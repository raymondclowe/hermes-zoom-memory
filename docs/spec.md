# Zoom Memory — Design Notes

## The problem

Hermes's built-in `ContextCompressor` replaces old messages with a flat
summary. That summary is opaque: the agent can read it but never recover the
original text. Over weeks, decisions, preferences, and the user's reasoning
sink into that summary and become unrecoverable.

## The idea

Keep a **verbatim append-only log** of every message. Compress it into a
**binary tree of short summary lines**. The agent sees the tree as its
historical layer, and can open any line back into the two lines it was made
from — all the way down to the original message.

This is the core borrowed from the X post about improving chatbot memory
systems. The tree-addressing scheme (`id+n`, binary merge, budget-driven
folding) is the part taken from that reference.

## What was built around it

The rest of the plugin is original work:

- **ZoomStore** — append-only JSONL with fsync durability, writer-lock /
  read-only-fallback model, HKT day sharding, fingerprint-based idempotent
  ingestion
- **Compactor** — background-ish node builder with backoff, size enforcement,
  priority-ordered compaction prompt
- **ZoomEngine** — lifecycle wiring, tool exposure, context injection,
  summary-layer unification
- **zoom / date tools** — the two tools exposed to the agent

## Node addressing

```
node(l, i) covers messages [i*2**l, (i+1)*2**l)
address: "id+n" where id = i*2**l, n = 2**l
children of node(l, i): node(l-1, 2i) and node(l-1, 2i+1)
```

## View folding

The view is a list of parts tiling `[0, T)`, oldest first, kept under a byte
budget. It changes by appending the new message, then merging the "most due"
adjacent pair until it fits again. It never splits.

The "due" metric for a pair `(a, b)` at level `l`:

```
due = (T - a.start) / 2**(l + 2)
```

Older messages are more due to merge — which is correct, because their
contents are already compressed and merging them again costs little.

## Durability contract

Every line is written with one write + fsync before the call returns, in
append order. A parent line only becomes durable after both children are.
A corrupt (torn) line is reported and the node is marked NOT built, so the
compactor retries it — "an absent item can never be found by zooming" must
not be violated by recovery.

## Ingestion idempotency

Messages are fingerprinted by `sha1(kind + "\x00" + text)[:16]`. A message is
logged at most once, ever. This makes ingestion idempotent across reloads and
compression rewrites.

## Compaction priority

The compaction prompt encodes this priority order for what to keep:

1. The user's own words matter most — orders, decisions, corrections,
   preferences, reasoning, explanations
2. Anything with lasting effect, done by anyone — what changed in the world,
   what was committed to, what failed and why
3. Findings and open questions, and the agent's own replies
4. Least of all: intermediate tool calls and outputs — mostly noise

The rule: "avoid dropping an item entirely. An absent item can never be found
by zooming, while a word or two keeps it findable."