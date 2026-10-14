# hermes-zoom-memory

A **zoomable, verbatim-backed memory layer** for Hermes Agent.

Instead of replacing compacted history with an opaque flat summary, this
plugin keeps an append-only verbatim log of every message and compresses it
into a **binary tree of short summary lines** that the agent can *open back
into the original text* with the `zoom(id, n)` and `date(id)` tools.

It subclasses Hermes's built-in `ContextCompressor` and swaps the flat summary
layer for the rendered tree view — so the agent sees **one** historical layer,
not a summary it can't inspect.

## What it does

```
recent messages  →  verbatim, in full
older history     →  one-line summaries, older = more messages per line
                  →  zoom(id, n) opens a line into the two lines under it
                  →  zoom(id, 1) opens a line into the original message
```

The tree is a **binary merge tree** addressed as `id+n` where `n = 2**l`:

- `node(l, i)` covers messages `[i*2**l, (i+1)*2**l)`
- The two children of `node(l, i)` are `node(l-1, 2i)` and `node(l-1, 2i+1)`
- A level-0 node is a single message; level `l` merges two level `l-1` nodes

The view is a list of parts tiling `[0, T)`, kept under a byte budget. When a
new message is appended, the "most due" adjacent pair is merged until it fits.

## Install

```bash
# Option A — copy the plugin into a profile's plugins dir
cp -r plugins/zoom ~/.hermes/profiles/<profile>/plugins/zoom

# Option B — install via the hub (when published)
hermes skills install <this-repo>

# Then enable it in config.yaml:
#   context:
#     engine: zoom
```

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `context.engine` | `auto` | Set to `zoom` to activate |
| `context.engine: zoom` | — | Reads `plugins/context_engine/zoom/` |

The view budget is derived automatically from the model's context window:
`min(128_000, usable_tokens * 4 * 0.40)` bytes.

## Tools

| Tool | Description |
|------|-------------|
| `zoom(id, n)` | Open line `id+n` into the two lines of `n/2` under it. `n=1` gives the message whole. `n` must be a power of 2; `id` must be a multiple of `n`. |
| `date(id)` | The date and time of message `id`. |

## Storage

Two append-only JSONL streams under `<hermes_home>/zoom/`:

```
main/YYYY-MM-DD.jsonl   {i, kind, text, size, date}   verbatim messages
tree/YYYY-MM-DD.jsonl   {l, i, text, size, built}     summary nodes
```

- `i` is a permanent, monotonically increasing message index. Messages are
  never renumbered.
- Every line is written with one write + fsync before the call returns, in
  append order — so a parent line only becomes durable after both children are.
- One writer per profile. A second process opens read-only and serves zoom
  from the last durable state.

## Compaction

The `Compactor` builds tree nodes from raw messages in order, up to 8 at a
time. A failed node waits 10 seconds and retries (up to 5 tries) — never
exponential backoff, because the next turn may wait on it.

Each node is built by calling an auxiliary LLM with a prompt that encodes the
priority order for what to keep:

1. The user's own words matter most (orders, decisions, corrections, reasoning)
2. Anything with lasting effect, done by anyone
3. Findings and open questions
4. Least of all: intermediate tool calls and outputs

## Layout

```
hermes-zoom-memory/
├── README.md
├── pyproject.toml
├── plugins/zoom/
│   ├── __init__.py      # ZoomEngine: lifecycle, ingestion, tools, context injection
│   ├── tree.py          # ZoomTree: addressing, view rendering, fold, zoom, date
│   ├── store.py         # ZoomStore: append-only JSONL + summary tree on disk
│   ├── compactor.py     # Compactor: builds tree nodes from raw messages
│   └── plugin.yaml      # Plugin metadata
├── tests/
│   └── test_zoom.py     # Unit tests for tree, store, and compactor
└── docs/
    └── spec.md           # Design notes
```

## License

MIT