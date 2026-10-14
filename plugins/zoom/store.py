"""Verbatim log + summary-tree storage for the zoom context engine.

Two append-only JSONL streams under <hermes_home>/zoom/:

    main/YYYY-MM-DD.jsonl   {i, kind, text, size, date}   verbatim messages
    tree/YYYY-MM-DD.jsonl   {l, i, text, size, built}     summary nodes

Design notes (see zoom-spec-v2.md):
- ``i`` is a permanent, monotonically increasing message index. Messages are
  never renumbered, because main/ is only ever appended to.
- A node is addressed ``id+n`` where ``n = 2**l``. node(l, i) covers messages
  ``[i*2**l, (i+1)*2**l)``. The two children of node(l, i) are node(l-1, 2i)
  and node(l-1, 2i+1).
- Every line is written with one write + fsync before the call returns, in
  append order, so a parent line only becomes durable after both children are.
- A corrupt (torn) line is reported and the node is marked NOT built, so the
  compactor retries it — "an absent item can never be found by zooming" must
  not be violated by recovery.
- One writer per profile. A second process does not block and does not write:
  it opens read-only and serves zoom from the last durable state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

KINDS = ("user", "talk", "tool", "echo", "note")


def _day(epoch: Optional[float] = None) -> str:
    """Local calendar day (YYYY-MM-DD) for an epoch; HKT on op3."""
    epoch = time.time() if epoch is None else epoch
    # Derive HKT from UTC so a server set to UTC still shards by the user's day.
    t = time.gmtime(epoch + 8 * 3600)
    return "%04d-%02d-%02d" % (t.tm_year, t.tm_mon, t.tm_mday)


def _iso_hkt(epoch: Optional[float] = None) -> str:
    epoch = time.time() if epoch is None else epoch
    t = time.gmtime(epoch + 8 * 3600)
    return "%04d-%02d-%02dT%02d:%02d:%02d+08:00" % (
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _text_size(kind: str, text: str) -> int:
    return len((kind + ": " + text).encode("utf-8"))


class ZoomStore:
    """Append-only verbatim log + summary tree on disk."""

    def __init__(self, root: Path, *, writable: bool = True):
        self.root = Path(root)
        self.main_dir = self.root / "main"
        self.tree_dir = self.root / "tree"
        self.main_dir.mkdir(parents=True, exist_ok=True)
        self.tree_dir.mkdir(parents=True, exist_ok=True)
        self.writable = writable
        self._lock_fd: Optional[int] = None
        # In-memory indexes, populated by load().
        self.messages: Dict[int, Dict] = {}
        self.nodes: Dict[tuple, Dict] = {}
        self.next_index: int = 0
        if writable:
            self._acquire_writer_lock()

    # -- locking ---------------------------------------------------------
    def _acquire_writer_lock(self) -> bool:
        """Try to take the exclusive advisory lock. Returns True on success.
        On failure the store is read-only — defined, non-stalling behaviour."""
        if self.writable and self._lock_fd is not None:
            return True
        lock_path = self.root / ".lock"
        try:
            import fcntl
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                self.writable = False
                return False
            self._lock_fd = fd
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self.writable = True
            return True
        except Exception as exc:  # pragma: no cover - platform guard
            logger.debug("zoom: lock unavailable (%s)", exc)
            return False

    def try_acquire(self) -> bool:
        """Attempt to become the writer (used when a live session needs to write
        but a previous session's lock has only just been released)."""
        if self.writable and self._lock_fd is not None:
            return True
        if self._acquire_writer_lock():
            logger.info("zoom: acquired the writer lock")
            return True
        return False

    def release(self) -> None:
        if self._lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None

    # -- load ------------------------------------------------------------
    def load(self) -> None:
        """Read every line into memory; torn lines are skipped and reported."""
        self.messages.clear()
        self.nodes.clear()
        torn = 0
        for path in sorted(self.main_dir.glob("*.jsonl")):
            torn += self._load_jsonl(path, self._on_message)
        for path in sorted(self.tree_dir.glob("*.jsonl")):
            torn += self._load_jsonl(path, self._on_node)
        self.next_index = (max(self.messages) + 1) if self.messages else 0
        if torn:
            logger.warning("zoom: %d torn line(s) skipped on load", torn)

    def _load_jsonl(self, path: Path, sink) -> int:
        torn = 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        torn += 1
                        continue
                    if isinstance(rec, dict):
                        sink(rec)
        except OSError as exc:
            logger.warning("zoom: cannot read %s: %s", path, exc)
        return torn

    def _on_message(self, rec: Dict) -> None:
        i = rec.get("i")
        if isinstance(i, int):
            self.messages[i] = rec

    def _on_node(self, rec: Dict) -> None:
        l, i = rec.get("l"), rec.get("i")
        if isinstance(l, int) and isinstance(i, int):
            self.nodes[(l, i)] = rec

    # -- append ----------------------------------------------------------
    def _append_line(self, path: Path, rec: Dict) -> None:
        """One write + fsync per line (durability contract)."""
        blob = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())

    def append_message(self, kind: str, text: str, *,
                       epoch: Optional[float] = None) -> int:
        """Append one verbatim message; returns its permanent index."""
        if not self.writable:
            raise RuntimeError("zoom store is read-only (another writer holds the lock)")
        if kind not in KINDS:
            kind = "echo" if kind not in KINDS else kind
        epoch = time.time() if epoch is None else epoch
        i = self.next_index
        rec = {"i": i, "kind": kind, "text": text,
               "size": _text_size(kind, text), "date": _iso_hkt(epoch)}
        day = _day(epoch)
        self._append_line(self.main_dir / f"{day}.jsonl", rec)
        self.messages[i] = rec
        self.next_index = i + 1
        return i

    def write_node(self, l: int, i: int, text: str, *, built: bool = True) -> None:
        if not self.writable:
            raise RuntimeError("zoom store is read-only (another writer holds the lock)")
        rec = {"l": l, "i": i, "text": text,
               "size": len(text.encode("utf-8")), "built": built}
        day = _day()
        self._append_line(self.tree_dir / f"{day}.jsonl", rec)
        self.nodes[(l, i)] = rec

    # -- reads -----------------------------------------------------------
    def message(self, i: int) -> Optional[Dict]:
        return self.messages.get(i)

    def node(self, l: int, i: int) -> Optional[Dict]:
        return self.nodes.get((l, i))

    def is_built(self, l: int, i: int) -> bool:
        rec = self.nodes.get((l, i))
        return bool(rec and rec.get("built"))

    def count(self) -> int:
        return len(self.messages)
