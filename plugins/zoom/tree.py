"""Tree addressing, the bounded view, and zoom/date resolution.

Node addressing: node(l, i) covers messages [i*2**l, (i+1)*2**l).
Rendered address: "id+n" with id = i*2**l and n = 2**l.

The view is a list of parts tiling [0, T), oldest first, kept under a byte
budget. It changes by appending the new message, then merging the "most due"
adjacent pair until it fits again. It never splits (see the spec).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

NODE = 512            # target bytes for one summary line
VIEW_FRACTION = 0.40  # share of usable context given to the view
VIEW_MAX = 128_000    # hard byte ceiling


def view_budget_bytes(context_length_tokens: int, output_reserve_tokens: int = 0) -> int:
    """Adaptive view budget (bytes) for a model's context window.

    Uses ~4 bytes/token as a rough byte↔token bridge; the view is a *summary*
    layer so a conservative ratio keeps it well inside the window.
    """
    usable = max(0, int(context_length_tokens) - int(output_reserve_tokens))
    if usable <= 0:
        return 0
    return min(VIEW_MAX, int(usable * 4 * VIEW_FRACTION))


class Part:
    __slots__ = ("l", "i")

    def __init__(self, l: int, i: int):
        self.l = l
        self.i = i

    @property
    def n(self) -> int:
        return 1 << self.l

    @property
    def start(self) -> int:
        return self.i * self.n

    @property
    def address(self) -> str:
        return f"{self.start}+{self.n}"

    def __repr__(self) -> str:
        return f"Part(l={self.l}, i={self.i}, addr={self.address})"


class ZoomTree:
    def __init__(self, store, *, budget_bytes: int = VIEW_MAX):
        self.store = store
        self.budget = budget_bytes
        self.view: List[Part] = []

    # -- rendering -------------------------------------------------------
    def _part_text(self, part: Part) -> str:
        rec = self.store.node(part.l, part.i)
        if rec and rec.get("built"):
            return rec["text"]
        # Only level-0 parts can be unbuilt (a parent enters the view only once built).
        return "(not summarized yet: zoom it)"

    def render(self) -> str:
        lines = [f"{p.address}|{self._part_text(p)}" for p in self.view]
        return "<chat>\n" + "\n".join(lines) + "\n</chat>"

    def size(self) -> int:
        return sum(len(self._part_text(p).encode("utf-8")) + len(p.address) + 1
                   for p in self.view)

    # -- fold ------------------------------------------------------------
    def rebuild(self) -> None:
        """Fold the view from message 0 (used at load)."""
        self.view = []
        for i in range(self.store.count()):
            self.view.append(Part(0, i))
            self.fit()

    def append(self, index: int) -> None:
        self.view.append(Part(0, index))
        self.fit()

    def fit(self) -> None:
        """Merge the most-due adjacent pair until the view is under budget."""
        T = self.store.count()
        guard = 0
        while self.size() > self.budget and guard < 100_000:
            guard += 1
            best = None
            best_due = -1.0
            for a, b in zip(self.view, self.view[1:]):
                if a.l != b.l or (a.i % 2) != 0:
                    continue
                if b.i != a.i + 1:
                    continue
                if not self.store.is_built(a.l + 1, a.i // 2):
                    continue
                due = (T - a.start) / float(1 << (a.l + 2))
                if due > best_due:
                    best_due = due
                    best = (a, b)
            if best is None:
                break  # wait until a parent is built
            a, b = best
            idx = self.view.index(a)
            self.view[idx:idx + 2] = [Part(a.l + 1, a.i // 2)]

    # -- zoom / date -----------------------------------------------------
    def zoom(self, id_: int, n: int) -> str:
        """Open line id+n into the two lines of n/2; n == 1 gives the message."""
        if n < 1 or (n & (n - 1)) != 0:
            return f"No line {id_}+{n}: n must be a power of 2."
        if n == 1:
            rec = self.store.message(id_)
            if not rec:
                return f"No line {id_}+1."
            return f"{id_}+1|{rec['kind']}: {rec['text']}"
        if id_ % n != 0:
            return f"No line {id_}+{n}: id must be a multiple of n."
        T = self.store.count()
        if id_ + n > T:
            return f"No line {id_}+{n}: past the end."
        l = int(math.log2(n)) - 1
        half = n // 2
        i0 = (id_ // n) * 2
        out = []
        for li, ii in ((l, i0), (l, i0 + 1)):
            rec = self.store.node(li, ii)
            if not rec or not rec.get("built"):
                return f"No line {id_}+{n}: child not summarized yet."
            addr = f"{ii << li}+{1 << li}"
            # Summary lines render flat (newlines are only kept for a whole message).
            out.append(f"{addr}|{rec['text'].replace(chr(10), ' ')}")
        return "\n".join(out)

    def date(self, id_: int) -> str:
        rec = self.store.message(id_)
        return rec["date"] if rec else f"No message {id_}."
