"""Tests for the zoom memory plugin (tree, store, compactor)."""
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from plugins.zoom.tree import Part, ZoomTree, view_budget_bytes
from plugins.zoom.store import ZoomStore, KINDS
from plugins.zoom.compactor import Compactor, NODE


# ---------------------------------------------------------------------------
# tree.py
# ---------------------------------------------------------------------------
class TestPart:
    def test_address(self):
        p = Part(0, 5)
        assert p.n == 1
        assert p.start == 5
        assert p.address == "5+1"

    def test_level_one(self):
        p = Part(1, 3)
        assert p.n == 2
        assert p.start == 6
        assert p.address == "6+2"

    def test_level_two(self):
        p = Part(2, 1)
        assert p.n == 4
        assert p.start == 4
        assert p.address == "4+4"


class TestViewBudget:
    def test_zero_usable(self):
        assert view_budget_bytes(0) == 0

    def test_cap(self):
        # 100k tokens * 4 bytes * 0.40 = 160k, capped at 128k
        assert view_budget_bytes(100_000) == 128_000

    def test_uncapped(self):
        # 10k tokens * 4 * 0.40 = 16k
        assert view_budget_bytes(10_000) == 16_000

    def test_reserves_output(self):
        # 20k tokens * 4 * 0.40 = 32k; reserving 10k leaves 22k
        a = view_budget_bytes(20_000, output_reserve_tokens=10_000)
        b = view_budget_bytes(20_000, output_reserve_tokens=0)
        assert a < b


class FakeStore:
    def __init__(self, messages, nodes):
        self.messages = messages
        self.nodes = nodes

    def count(self):
        return len(self.messages)

    def message(self, i):
        return self.messages.get(i)

    def node(self, l, i):
        return self.nodes.get((l, i))

    def is_built(self, l, i):
        rec = self.nodes.get((l, i))
        return bool(rec and rec.get("built"))


def _msg(i, text="x"):
    return {"i": i, "kind": "user", "text": text, "size": len(text), "date": "d"}


def _node(l, i, text="y", built=True):
    return {"l": l, "i": i, "text": text, "size": len(text), "built": built}


class TestZoomTree:
    def test_zoom_message(self):
        store = FakeStore({0: _msg(0, "hello")}, {})
        t = ZoomTree(store)
        assert t.zoom(0, 1) == "0+1|user: hello"

    def test_zoom_missing_message(self):
        store = FakeStore({}, {})
        t = ZoomTree(store)
        assert "No line 0+1" in t.zoom(0, 1)

    def test_zoom_bad_n(self):
        store = FakeStore({0: _msg(0, "hi")}, {})
        t = ZoomTree(store)
        assert "power of 2" in t.zoom(0, 3)

    def test_zoom_id_must_be_multiple(self):
        store = FakeStore({0: _msg(0, "hi"), 1: _msg(1, "ho")}, {})
        t = ZoomTree(store)
        assert "multiple of n" in t.zoom(1, 2)

    def test_zoom_past_end(self):
        store = FakeStore({0: _msg(0, "hi")}, {})
        t = ZoomTree(store)
        assert "past the end" in t.zoom(0, 4)

    def test_zoom_node(self):
        store = FakeStore(
            {0: _msg(0, "a"), 1: _msg(1, "b")},
            {(0, 0): _node(0, 0, "child-a"), (0, 1): _node(0, 1, "child-b")},
        )
        t = ZoomTree(store)
        out = t.zoom(0, 2)
        assert "0+1|child-a" in out
        assert "1+1|child-b" in out

    def test_zoom_child_not_built(self):
        store = FakeStore(
            {0: _msg(0, "a"), 1: _msg(1, "b")},
            {(0, 0): _node(0, 0, "child-a")},  # (0,1) missing
        )
        t = ZoomTree(store)
        assert "not summarized yet" in t.zoom(0, 2)

    def test_date(self):
        store = FakeStore({0: _msg(0, "x")}, {})
        t = ZoomTree(store)
        assert t.date(0) == "d"

    def test_date_missing(self):
        store = FakeStore({}, {})
        t = ZoomTree(store)
        assert "No message 0" in t.date(0)

    def test_render_empty(self):
        store = FakeStore({}, {})
        t = ZoomTree(store)
        # render() joins the (empty) view list with newlines inside <chat>
        assert t.render().startswith("<chat>")
        assert t.render().endswith("</chat>")

    def test_append_and_fit(self):
        # 4 messages of 1 byte each + addresses = ~52 bytes, well under 200
        msgs = {i: _msg(i, "x") for i in range(4)}
        store = FakeStore(msgs, {})
        t = ZoomTree(store, budget_bytes=200)
        for i in range(4):
            t.append(i)
        # Stays under budget
        assert t.size() <= 200
        # View tiles [0,4) with one part per message
        assert len(t.view) == 4


# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------
class TestZoomStore:
    def test_append_and_load(self, tmp_path):
        s = ZoomStore(tmp_path)
        i = s.append_message("user", "hello")
        assert i == 0
        assert s.count() == 1
        s.release()

        s2 = ZoomStore(tmp_path, writable=False)
        s2.load()
        assert s2.count() == 1
        assert s2.message(0)["text"] == "hello"
        assert s2.message(0)["kind"] == "user"

    def test_append_dupe_kind_falls_back(self, tmp_path):
        s = ZoomStore(tmp_path)
        i = s.append_message("bogus", "x")
        assert i == 0
        assert s.message(0)["kind"] == "echo"
        s.release()

    def test_write_node_and_is_built(self, tmp_path):
        s = ZoomStore(tmp_path)
        s.write_node(0, 0, "summary", built=True)
        assert s.is_built(0, 0) is True
        assert s.node(0, 0)["text"] == "summary"
        s.release()

    def test_write_node_not_built(self, tmp_path):
        s = ZoomStore(tmp_path)
        s.write_node(0, 0, "summary", built=False)
        assert s.is_built(0, 0) is False
        s.release()

    def test_read_only_cannot_append(self, tmp_path):
        s = ZoomStore(tmp_path)
        s.append_message("user", "x")
        s.release()

        s2 = ZoomStore(tmp_path, writable=False)
        with pytest.raises(RuntimeError):
            s2.append_message("user", "y")

    def test_lock_is_exclusive(self, tmp_path):
        s = ZoomStore(tmp_path)
        assert s.writable is True
        # A second writable store on the same dir must lose the lock
        s2 = ZoomStore(tmp_path, writable=True)
        assert s2.writable is False
        s.release()

    def test_torn_line_skipped(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        (main / "2026-01-01.jsonl").write_text(
            '{"i": 0, "kind": "user", "text": "ok", "size": 2, "date": "d"}\n'
            "not json\n"
            '{"i": 1, "kind": "user", "text": "ok2", "size": 3, "date": "d"}\n',
            encoding="utf-8",
        )
        s = ZoomStore(tmp_path, writable=False)
        s.load()
        assert s.count() == 2

    def test_kinds(self):
        assert KINDS == ("user", "talk", "tool", "echo", "note")


# ---------------------------------------------------------------------------
# compactor.py
# ---------------------------------------------------------------------------
class FakeCall:
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return "compressed-line"


def _make_tree(store):
    """A ZoomTree over an existing store, with the view rebuilt from disk."""
    from plugins.zoom.tree import ZoomTree
    t = ZoomTree(store, budget_bytes=128_000)
    t.rebuild()
    return t


class TestCompactor:
    def _writable_store(self, tmp_path, n):
        s = ZoomStore(tmp_path)
        for i in range(n):
            s.append_message("user", f"message {i}")
        return s

    def _read_store(self, tmp_path, n):
        s = self._writable_store(tmp_path, n)
        s.release()
        return ZoomStore(tmp_path, writable=False)

    def test_ready_level0(self, tmp_path):
        s = self._writable_store(tmp_path, 1)
        c = Compactor(s, None, call_fn=FakeCall())
        assert c._ready(0, 0) is True
        assert c._ready(0, 1) is False
        s.release()

    def test_ready_level1_requires_children(self, tmp_path):
        s = self._writable_store(tmp_path, 2)
        c = Compactor(s, None, call_fn=FakeCall())
        assert c._ready(1, 0) is False
        s.write_node(0, 0, "a", built=True)
        s.write_node(0, 1, "b", built=True)
        assert c._ready(1, 0) is True
        s.release()

    def test_build_level0(self, tmp_path):
        s = self._writable_store(tmp_path, 1)
        c = Compactor(s, _make_tree(s), call_fn=FakeCall())
        text = c._build(0, 0)
        assert text is not None
        assert len(text.encode("utf-8")) <= NODE
        s.release()

    def test_enforce_size_truncates(self, tmp_path):
        s = self._writable_store(tmp_path, 1)
        calls = []

        def call(system, user):
            calls.append(1)
            # First call returns oversized; the trim request must shrink it
            return "x" * (NODE + 100) if len(calls) == 1 else "x" * NODE

        c = Compactor(s, _make_tree(s), call_fn=call)
        result = c._enforce_size("user prompt")
        assert len(result.encode("utf-8")) <= NODE
        # At least two calls: the original, then the trim
        assert len(calls) >= 2
        s.release()

    def test_build_level1(self, tmp_path):
        s = self._writable_store(tmp_path, 2)
        s.write_node(0, 0, "child-a", built=True)
        s.write_node(0, 1, "child-b", built=True)
        c = Compactor(s, _make_tree(s), call_fn=FakeCall())
        text = c._build(1, 0)
        assert text is not None
        assert len(text.encode("utf-8")) <= NODE
        s.release()

    def test_backoff_retry(self, tmp_path):
        s = self._writable_store(tmp_path, 1)

        def call(system, user):
            raise ValueError("boom")

        c = Compactor(s, _make_tree(s), call_fn=call)
        # _build_guarded catches the exception and records the failure
        c._build_guarded(0, 0)
        assert (0, 0) in c._failed_at
        assert c._in_backoff((0, 0)) is True
        # Wait out the backoff
        c._failed_at[(0, 0)] = time.time() - 11
        assert c._in_backoff((0, 0)) is False
        s.release()