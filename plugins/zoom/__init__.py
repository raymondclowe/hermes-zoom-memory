"""Zoom context engine — a zoomable, verbatim-backed memory layer for Hermes.

This is a ContextEngine: the plugin's ``register(ctx)`` calls
``ctx.register_context_engine(...)`` (the discovery scanner looks for these
tokens in the first 8 KB of this file).

Instead of replacing compacted history with an opaque flat summary, this engine
keeps a verbatim append-only log and compresses it into a binary tree of short
summary lines the agent can *open back into the original text*. It subclasses
Hermes's built-in ``ContextCompressor`` so all the battle-tested compaction
logic is inherited; only the historical layer and two tools change.

Select it with ``context.engine: zoom`` in config.yaml. Until then it is inert.

See the spec: zoom-spec-v2.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    ContextCompressor,
)
from agent.model_metadata import estimate_messages_tokens_rough

from .compactor import COMPACT_PROMPT, Compactor
from .store import ZoomStore
from .tree import ZoomTree, view_budget_bytes

logger = logging.getLogger(__name__)

_VIEW_HEADER = (
    "[ZOOM MEMORY — reference only] The chat below is a summary tree of older "
    "messages, oldest first, one line per node:\n"
    "  id+n|text   = the n messages from id on, summarized\n"
    "Open a line with zoom(id, n) before you rely on it (zoom(id, 1) = the "
    "message whole); date(id) gives its time. Recent messages appear verbatim "
    "later in this context — the tree is only the older history."
)


class ZoomEngine(ContextCompressor):
    """Verbatim log + zoomable summary tree, on top of the built-in compressor."""

    @property
    def name(self) -> str:
        return "zoom"

    def __init__(self) -> None:
        # The host builds plugin engines with NO args, then calls update_model().
        super().__init__(model="")
        self._store: Optional[ZoomStore] = None
        self._tree: Optional[ZoomTree] = None
        self._compactor: Optional[Compactor] = None
        self._logged_counts: Dict[tuple, int] = {}
        self._view_budget = 0
        self._hermes_home: Optional[Path] = None

    # -- cloning ---------------------------------------------------------
    def clone_for_agent(self) -> "ZoomEngine":
        """Per-agent instance. The base impl deepcopies, which cannot copy a
        ThreadPoolExecutor or an fd. Return a clean engine carrying only the
        model/budget state; ``on_session_start`` then builds its OWN store (and
        competes for the profile's single writer lock). Only one clone wins the
        lock and writes — the others run read-only, per the spec."""
        clone = ZoomEngine()
        for k in ("model", "base_url", "api_key", "provider", "api_mode",
                  "context_length", "threshold_tokens", "threshold_percent",
                  "_base_threshold_percent", "_config_threshold_percent",
                  "protect_first_n", "protect_last_n", "summary_target_ratio",
                  "quiet_mode", "max_tokens", "model_thresholds"):
            if k in self.__dict__:
                clone.__dict__[k] = self.__dict__[k]
        clone._view_budget = self._view_budget
        clone._hermes_home = self._hermes_home
        return clone

    # -- lifecycle -------------------------------------------------------
    def on_session_start(self, session_id: str, **kwargs) -> None:
        home = kwargs.get("hermes_home")
        self._hermes_home = Path(home) if home else _default_home()
        root = self._hermes_home / "zoom"
        try:
            self._store = ZoomStore(root, writable=True)
        except Exception as exc:
            logger.warning("zoom: store init failed (%s); engine degraded to plain compressor", exc)
            self._store = None
            return
        # One-time repair of the log (drops empty/duplicate rows the earlier
        # non-idempotent ingestion left behind), THEN load. Must run under the
        # write lock, before load(), so the in-memory store sees the clean data.
        if self._store.writable:
            self._sanitize_logs(root)
        try:
            self._store.load()
        except Exception as exc:
            logger.warning("zoom: store load failed (%s); engine degraded to plain compressor", exc)
            self._store = None
            return
        # Authoritative fingerprint set: every message already on disk. This makes
        # ingestion idempotent across reloads/compression rewrites — a message is
        # logged at most once, ever (see the duplicate-log fix).
        self._logged = {self._fp(r.get("kind", ""), r.get("text", ""))
                        for r in self._store.messages.values()}
        self._tree = ZoomTree(self._store, budget_bytes=self._view_budget or 128_000)
        self._tree.rebuild()
        self._compactor = Compactor(self._store, self._tree, call_fn=self._compact_call)
        logger.info("zoom: loaded %d messages (%d fingerprints) from %s",
                    self._store.count(), len(self._logged), root)

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        self._ingest(messages)
        if self._compactor:
            self._compactor.close()
        # Release the writer lock so a live session's engine can take over — a dead
        # session must never keep the profile's single writer slot (see the stall fix).
        if self._store:
            self._store.release()

    # -- model wiring ----------------------------------------------------
    def update_model(self, model: str, context_length: int, base_url: str = "",
                     api_key: str = "", provider: str = "", api_mode: str = "") -> None:
        super().update_model(model=model, context_length=context_length, base_url=base_url,
                             api_key=api_key, provider=provider, api_mode=api_mode)
        self._view_budget = view_budget_bytes(context_length, self.max_tokens or 0)
        if self._tree is not None and self._view_budget:
            self._tree.budget = self._view_budget

    # -- ingestion (LIVE, per turn) --------------------------------------
    def on_turn_complete(self, messages: List[Dict[str, Any]], usage: Dict[str, Any] = None,
                         **kwargs: Any) -> None:
        self._ingest(messages)

    @staticmethod
    def _fp(role_or_kind: str, text: str) -> str:
        """Stable content fingerprint — the identity of a logged message."""
        return hashlib.sha1((role_or_kind + "\x00" + (text or "")).encode("utf-8")).hexdigest()[:16]

    def _sanitize_logs(self, root: Path) -> None:
        """One-time repair: drop empty rows and exact duplicates. The first
        ingestion design re-logged the whole transcript on every gateway reload
        (and logged empty tool/talk bodies), which polluted the log. Runs under
        the write lock; if anything changed the tree is wiped so it rebuilds."""
        changed = False
        for f in sorted((root / "main").glob("*.jsonl")):
            rows, seen = [], set()
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    txt = r.get("text") or ""
                    if not txt.strip():          # empty body = no signal
                        changed = True
                        continue
                    h = self._fp(r.get("kind", ""), txt)
                    if h in seen:                # exact duplicate from a re-ingest
                        changed = True
                        continue
                    seen.add(h)
                    rows.append(r)
            except Exception as exc:
                logger.debug("zoom: sanitize skipped %s: %s", f.name, exc)
                continue
            if changed:
                tmp = f.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                               encoding="utf-8")
                tmp.replace(f)
        if changed:
            for t in (root / "tree").glob("*.jsonl"):
                t.unlink()
            logger.info("zoom: sanitized log (removed empty/duplicate rows); tree will rebuild")

    def _ingest(self, messages: List[Dict[str, Any]]) -> None:
        """Append messages not already logged (idempotent by content fingerprint)."""
        if not self._store or not messages:
            return
        if not self._tree:
            return
        # A previous (now-dead) session may still hold the lock; the live session
        # must be able to take it over, or the tree would freeze forever.
        if not self._store.writable:
            self._store.try_acquire()
        if not self._store.writable:
            return
        if not hasattr(self, "_logged"):
            self._logged = set()
        added = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or ""
            if role == "system":
                continue
            content = msg.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if not text or not text.strip():     # never log empty bodies
                continue
            kind = _kind_for(role, msg)
            h = self._fp(kind, text)             # fingerprint the STORED kind, not the role,
            if h in self._logged:                # so it matches the on-disk seed set exactly
                continue
            try:
                self._store.append_message(kind, text)
            except Exception as exc:
                logger.debug("zoom: append failed: %s", exc)
                break
            self._logged.add(h)
            self._tree.append(self._store.next_index - 1)
            added += 1
        if added and self._compactor:
            self._compactor.kick()

    # -- tools -----------------------------------------------------------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"name": "zoom", "description":
                "Open the line id+n of the chat-memory view into the two lines of "
                "n/2 under it; n = 1 gives the message whole. Use it whenever a "
                "summary line only mentions something you need, before you act, "
                "guess or ask.",
             "parameters": {"type": "object", "properties": {
                 "id": {"type": "integer", "description": "first message index of the line"},
                 "n": {"type": "integer", "description": "how many messages the line covers (a power of 2)"}},
                 "required": ["id", "n"]}},
            {"name": "date", "description": "The date and time of message id.",
             "parameters": {"type": "object", "properties": {
                 "id": {"type": "integer", "description": "message index"}},
                 "required": ["id"]}},
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._tree:
            return json.dumps({"error": "zoom memory is not initialised for this session"})
        try:
            if name == "zoom":
                return json.dumps({"result": self._tree.zoom(int(args["id"]), int(args["n"]))})
            if name == "date":
                return json.dumps({"result": self._tree.date(int(args["id"]))})
        except (KeyError, TypeError, ValueError) as exc:
            return json.dumps({"error": f"bad arguments: {exc}"})
        return json.dumps({"error": f"Unknown zoom tool: {name}"})

    # -- context injection ----------------------------------------------
    def select_context(self, request_messages: List[Dict[str, Any]], *,
                       conversation_messages: List[Dict[str, Any]] = None,
                       incoming_message: Dict[str, Any] = None,
                       budget_tokens: int = 0) -> Optional[List[Dict[str, Any]]]:
        """Insert the bounded view as a system block; None leaves the request as-is."""
        if not self._tree or not self._store or not self._store.count():
            return None
        view = self._tree.render()
        block = {"role": "system", "content": _VIEW_HEADER + "\n\n" + view}
        out = list(request_messages)
        insert_at = 0
        for idx, m in enumerate(out):
            if isinstance(m, dict) and m.get("role") == "system":
                insert_at = idx + 1
            else:
                break
        out.insert(insert_at, block)
        return out

    # -- compaction: unify the historical layer --------------------------
    def compress(self, messages: List[Dict[str, Any]], current_tokens: Optional[int] = None,
                 focus_topic: Optional[str] = None, force: bool = False,
                 memory_context: str = "") -> List[Dict[str, Any]]:
        """Run the inherited compressor, then swap its flat summary for the view."""
        out = super().compress(messages, current_tokens=current_tokens, focus_topic=focus_topic,
                               force=force, memory_context=memory_context)
        if out is messages or not self._tree:
            return out
        try:
            self._unify_summary_layer(out)
        except Exception as exc:
            logger.debug("zoom: summary-layer unify skipped: %s", exc)
        return out

    def _unify_summary_layer(self, out: List[Dict[str, Any]]) -> None:
        """Replace the flat compaction summary's body with the rendered view, so the
        agent sees ONE historical layer. Fail-safe: keep the flat summary if the view
        would be larger or the store is empty."""
        if not self._store or not self._store.count():
            return
        for msg in out:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant" and not msg.get(COMPRESSED_SUMMARY_METADATA_KEY):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.startswith(SUMMARY_PREFIX[:40]):
                continue
            view = self._tree.render()
            if len(view.encode("utf-8")) > len(content.encode("utf-8")):
                return  # never grow the layer
            msg["content"] = f"{SUMMARY_PREFIX}\n\n{_VIEW_HEADER}\n\n{view}"
            return

    # -- compactor model call -------------------------------------------
    def _compact_call(self, system: str, user: str) -> str:
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
        resp = call_llm(task="compression", messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return extract_content_or_reasoning(resp) or ""


def _kind_for(role: str, msg: Dict[str, Any]) -> str:
    if role == "user":
        return "user"
    if role == "assistant":
        return "talk" if not msg.get("tool_calls") else "tool"
    if role == "tool":
        return "echo"
    return "note"


def _default_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def register(ctx) -> None:
    """Context-engine plugin entry point."""
    ctx.register_context_engine(ZoomEngine())
