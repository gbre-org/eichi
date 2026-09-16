---
name: eichi-search
description: |
  Use eichi as the default FIRST lookup for open-ended, fuzzy recall questions
  ("where is X", "what did we decide about Y", "find the note where I wrote
  about Z") — before grepping or asking the user for context. eichi is a
  local sqlite-vec + sentence-transformers semantic search index over your
  own notes, transcripts, and files. Trigger on: recall / "did we already
  discuss..." / "where did I write about..." / searching notes, transcripts,
  or logs for a concept rather than an exact string. NOT for exact-string
  lookups (function names, error codes, config keys — use grep) or
  structured data (metrics, timestamps, statuses — use a domain-specific
  tool).
---

# eichi search

[eichi](https://github.com/gbre-org/eichi) is a local-first semantic search
index: [`sqlite-vec`](https://github.com/asg017/sqlite-vec) for vector search
plus [`sentence-transformers`](https://www.sbert.net/) for embeddings, over
your own notes, chat transcripts, or files. Everything lives in one SQLite
file (`~/.local/share/eichi/index.db` by default); queries run fully offline
after the one-time embedding-model bootstrap.

## When to use it — decision tree

1. **Concept-level / fuzzy question** ("where is X", "what did we decide
   about Y") -> query eichi first.
2. **Exact-string question** (a function name, an error message, a config
   key) -> `grep -r` or code search instead.
3. **Structured data** (metrics, timestamps, statuses) -> the relevant
   domain-specific tool (a database, a metrics backend, etc.), not eichi.

Fall back to grep only if eichi returns nothing, or every hit scores
`[distant]` — not before.

## How to query

The CLI is the primary interface:

```bash
eichi query "<question>"                  # top-K hybrid (vec + BM25) hits
eichi query "<question>" -k 5             # cap result count
eichi query "<question>" --added-since 7d # filter by recency
eichi query "<question>" --sort added -k 10
eichi stats                               # doc count, last-indexed time, DB size
eichi ls                                  # indexed files + chunk counts
```

Where the index is served over HTTP instead of a local CLI (e.g. from
inside a container without the venv), use the web API:

```bash
curl -s "http://<host>/api/search?q=<query>&k=5" | jq .
```

Query params: `q` (required), `k` (top-K, default 20), `source` (filter by
source tag), `added_since` (duration, e.g. `1d` / `7d` / `30d`), `retrieval`
(`hybrid` | `vector` | `bm25`).

## Interpreting results

- **Score label**, most to least confident: `[strong]` > `[moderate]` >
  `[weak]` > `[distant]`. Treat `[distant]` as noise unless the query is
  highly specialized.
- **Source tag** (e.g. `[file]`, `[obsidian]`, `[transcripts]`) —
  provenance, useful for disambiguating similar hits.
- **Timestamp** — when the document was last modified or added to the
  index.

## Freshness

The index is delta-maintained: `eichi index <path>` only re-embeds files
whose content changed, so it is not automatically live. If a very recent
item doesn't turn up, run `eichi stats` and check `last indexed at` before
concluding eichi genuinely has no answer — it may just not be indexed yet.

## When NOT to use

- **Exact-string lookups.** grep is faster and exact; eichi's embeddings
  are tuned for meaning, not literal substring matches.
- **Structured or aggregated data.** eichi returns document chunks, not
  computed metrics or counts.

See [`AGENTS.md`](../../../AGENTS.md) in the repo root for the fuller
agent-integration writeup (invocation patterns, re-indexing guidance, and a
worked example of wiring this into a production agent loop).
