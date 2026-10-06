"""eichi connectors.

A connector is a small Python module that knows how to enumerate documents
from some external data source and emit them as JSONL-shaped dicts compatible
with ``eichi index-stream``. Each dict has the keys ``source``, ``doc_id``,
``text``, ``mtime`` (optional), and ``metadata`` (optional).

The standard contract is a function::

    def iter_documents(state: dict | None = None,
                       config: dict | None = None) -> Iterator[dict]: ...

``state`` is an opaque per-connector cursor that the driver loads/saves so
incremental runs only re-emit changed content. ``config`` is per-connector
configuration loaded from ``eichi.toml`` (path overrides, etc.).

Connectors shipped with eichi:

- :mod:`eichi.connectors.claude_jsonl` — Anthropic CLI session JSONL files
  (``~/.claude/projects/*/*.jsonl``). One doc per user/assistant turn plus
  rolling conversation clusters per session.
- :mod:`eichi.connectors.claude_watch_queue` — claude-watch ``queue.json``
  task queue. One doc per finished queue item.
- :mod:`eichi.connectors.http_conversation` — generic, fully
  config-driven HTTP/JSON (or ``file://``) conversation source. One doc
  per message. Declare as many ``[[corpus]]`` blocks with
  ``connector = "http-conversation"`` as you like; each indexes under its
  own source name.

Writing an external connector
-----------------------------
A connector does not have to live in this repository. Any installed
package can register one through the ``eichi.connectors`` entry-point
group, so backend-specific adapters (custom auth, odd pagination,
clustering, attachment handling) ship in their own repos.

1. Write a module (or a plain function) that follows the contract::

       KIND = "conversation"          # or "document" (default)

       def iter_documents(state=None, config=None):
           ...                         # yield document dicts

   Each yielded dict has:

   ``source``    source tag the doc is indexed under (defaults to the
                 corpus name; set it per doc only if you need to).
   ``doc_id``    stable unique id within the source. Re-emitting the same
                 ``doc_id`` with the same ``text`` is a cheap no-op.
   ``text``      the content to embed. Docs with no id or text are skipped
                 and counted as errors.
   ``mtime``     optional float epoch seconds; drives recency ranking.
   ``metadata``  optional dict. Recognised keys: ``kind``
                 (``"msg"`` / ``"cluster"``), ``cluster_id``,
                 ``mtime_end``, ``library_added_at``; anything else is
                 stored as-is.

   ``state`` is a dict the driver loads before the run, passes in, and
   persists afterwards (per corpus name). It starts as ``None`` / empty.
   Mutate it in place to record a cursor (last id, last timestamp, ...)
   so later runs only emit new content. Keep it JSON-serialisable.
   ``config`` is the ``[[corpus]]`` table from ``eichi.toml`` for the
   corpus being indexed, plus ``source`` = the corpus name. Read secrets
   from the environment, not the file.

2. Declare the kind. ``KIND = "conversation"`` marks the connector as a
   live conversation source: ``eichi query --conversations`` then searches
   every configured corpus backed by it, with the recency boost and path
   collapsing the preset provides. Anything else (or no ``KIND``) means a
   plain document source. The declaration is read from the module, or,
   for a bare function, from a ``KIND`` attribute on the function.

3. Register it in the package's ``pyproject.toml``::

       [project.entry-points."eichi.connectors"]
       my-backend = "my_pkg.eichi_connector"          # a module
       # my-backend = "my_pkg.connector:iter_documents"   # or a function

   The entry-point name is the connector name used in ``eichi.toml``::

       [[corpus]]
       name = "support-chat"
       connector = "my-backend"
       base_url = "https://chat.example.test/api"

Collisions: built-in connectors always win. A third-party entry point
whose name matches a built-in (or an earlier-loaded third party) is
ignored and a warning is printed to stderr, so a shadowing attempt is
never silent. Entry points that fail to import are skipped with a
warning; they never break indexing of other corpora.

All connectors no-op gracefully when their underlying source is absent
(path missing / API unreachable), so it's safe to enable them in
``eichi.toml`` even on a fresh machine.
"""

from __future__ import annotations

import sys
from importlib import metadata as _metadata
from typing import Callable, Dict, Optional

from . import claude_jsonl, claude_watch_queue, http_conversation

ENTRY_POINT_GROUP = "eichi.connectors"

KIND_CONVERSATION = "conversation"
KIND_DOCUMENT = "document"

_BUILTIN_MODULES = {
    "claude-jsonl": claude_jsonl,
    "claude-watch-queue": claude_watch_queue,
    "http-conversation": http_conversation,
}


def _kind_of(obj) -> str:
    kind = getattr(obj, "KIND", KIND_DOCUMENT)
    return str(kind).strip().lower() or KIND_DOCUMENT


def _resolve(obj) -> Optional[Callable]:
    """Accept a module exposing ``iter_documents`` or the callable itself."""
    if callable(obj):
        return obj
    fn = getattr(obj, "iter_documents", None)
    return fn if callable(fn) else None


def _discover_third_party(registry: Dict[str, Callable], kinds: Dict[str, str]):
    try:
        eps = _metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover — defensive
        print(f"eichi: cannot list {ENTRY_POINT_GROUP} entry points: {exc}",
              file=sys.stderr)
        return
    for ep in sorted(eps, key=lambda e: e.name):
        if ep.name in registry:
            print(
                f"eichi: ignoring entry point {ep.name!r} ({ep.value}): "
                "name already registered (built-ins and earlier entry "
                "points win)",
                file=sys.stderr,
            )
            continue
        try:
            obj = ep.load()
        except Exception as exc:
            print(f"eichi: entry point {ep.name!r} ({ep.value}) failed to "
                  f"load: {exc}", file=sys.stderr)
            continue
        fn = _resolve(obj)
        if fn is None:
            print(f"eichi: entry point {ep.name!r} ({ep.value}) has no "
                  "iter_documents; skipped", file=sys.stderr)
            continue
        registry[ep.name] = fn
        kinds[ep.name] = _kind_of(obj)


def build_registry():
    """Return ``(registry, kinds)``: built-ins first, then entry points."""
    registry: Dict[str, Callable] = {
        n: m.iter_documents for n, m in _BUILTIN_MODULES.items()
    }
    kinds: Dict[str, str] = {n: _kind_of(m) for n, m in _BUILTIN_MODULES.items()}
    _discover_third_party(registry, kinds)
    return registry, kinds


# Public registry — connector name -> ``iter_documents``. The CLI looks up
# ``eichi index --corpus <name>`` here. KINDS maps the same names to their
# declared kind ("conversation" / "document").
REGISTRY, KINDS = build_registry()


def kind_of(name: str) -> str:
    """Declared kind of the connector registered as ``name``."""
    return KINDS.get(name, KIND_DOCUMENT)


__all__ = [
    "REGISTRY", "KINDS", "ENTRY_POINT_GROUP", "KIND_CONVERSATION",
    "build_registry", "kind_of",
    "claude_jsonl", "claude_watch_queue", "http_conversation",
]
