# Writing an external connector

Backend-specific adapters should live in their own package, not in eichi.
eichi discovers them through the `eichi.connectors` entry-point group; the
full contract is documented in the `eichi.connectors` module docstring and
summarised here.

## Contract

```python
KIND = "conversation"   # or "document" (the default)

def iter_documents(state=None, config=None):
    # yield dicts: source, doc_id, text, mtime (optional), metadata (optional)
    ...
```

- `doc_id` is stable and unique within the source; `text` is what gets
  embedded; `mtime` is epoch seconds and drives recency ranking.
- `metadata` may carry `kind` (`"msg"` / `"cluster"`), `cluster_id`,
  `mtime_end`, `library_added_at`.
- `state` is a per-corpus dict persisted between runs. Mutate it in place
  to store a cursor (JSON-serialisable values only).
- `config` is the corpus table from `eichi.toml` plus `source` (the corpus
  name). Take secrets from environment variables.
- `KIND = "conversation"` makes `eichi query --conversations` include every
  corpus backed by the connector.

## Optional: progress estimates

```python
def estimate_pending(state=None, config=None):
    # return a cheap estimate of how many docs the next iter_documents()
    # run will emit, or None when unknown
    ...
```

Large backfills print a progress line every ~30s (`eichi index --progress`,
or automatically once a run proves large; `--no-progress` silences it).
If the module (or function attribute) defines `estimate_pending`, eichi
uses it for `~N% of ~total est.`, remaining count and ETA. It is optional:
without it the line degrades to "N docs, rate". The hook runs in a
background thread, receives a snapshot of `state`, must not mutate shared
state, and its result is always shown as an estimate. Cursor-based HTTP
sources typically cannot answer and should omit it.

## Registering

```toml
# the adapter package's pyproject.toml
[project.entry-points."eichi.connectors"]
my-backend = "my_pkg.eichi_connector"
```

```toml
# eichi.toml
[[corpus]]
name = "support-chat"
connector = "my-backend"
base_url = "https://chat.example.test/api"
```

Built-in connectors win name collisions; a shadowing entry point is ignored
with a warning on stderr. Entry points that fail to import are skipped with
a warning.

## Minimal example

```python
# my_pkg/eichi_connector.py
import json, os, urllib.request

KIND = "conversation"

def iter_documents(state=None, config=None):
    state = state if state is not None else {}
    cfg = config or {}
    req = urllib.request.Request(
        f"{cfg['base_url']}/messages?after={state.get('last_id', 0)}",
        headers={"Authorization": "Bearer " + os.environ.get("MY_TOKEN", "")},
    )
    try:
        msgs = json.load(urllib.request.urlopen(req, timeout=10))
    except OSError:
        return  # source unreachable: no-op
    for m in msgs:
        yield {
            "source": cfg.get("source", "my-backend"),
            "doc_id": f"msg:{m['id']}",
            "text": m["text"],
            "mtime": float(m["ts"]),
            "metadata": {"author": m.get("user")},
        }
        state["last_id"] = max(state.get("last_id", 0), m["id"])
