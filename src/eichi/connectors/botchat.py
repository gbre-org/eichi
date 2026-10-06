"""botchat conversation connector.

Indexes messages from a running ``botchat`` instance — the lightweight
operator <-> workbot chat service (Postgres-backed, fronted by an HTTP
API). One Document is emitted per chat message so the conversation
becomes semantically searchable alongside transcripts, queue items, and
files.

Source of messages
------------------
The connector reads the botchat HTTP API rather than touching Postgres
directly — the API is already the canonical read surface and avoids a
``psycopg`` dependency in eichi. The default endpoint is
``http://localhost:8111/api/messages`` (the host-published port of the
``botchat`` container). Override via ``config["api_base"]`` or
``$EICHI_BOTCHAT_API_BASE`` (point it at e.g.
``http://localhost:8111`` — the ``/api/messages`` path is appended).

The API returns the newest ``limit`` messages (ascending by id within
the page); the connector pages backwards via ``?limit=&before=<id>``
until it has walked every message (or until it reaches messages it has
already indexed, per the cursor). ``offset`` is ignored by the API.

Message shape (one element of the ``messages`` array)::

    {
      "id": 1015,                       # monotonically increasing int id
      "ts": 1782532504.6295133,         # epoch seconds (send time)
      "sender": "andrew" | "<workbot>", # author
      "body": "free-text message body",
      "topic": "optional thread topic or null",
      "reply_to": <id or null>,         # id of the message this replies to
      "reply_parent": ...,              # (unused here)
      "attachments": [...],             # (rendered as a count, not bodies)
      "reactions": {...}
    }

doc_id format::

    botchat:msg:<id>

Cursor: the highest message ``id`` seen so far. botchat ids are
monotonically increasing, so a re-run only fetches messages newer than
the cursor. State::

    {"max_id": <int>}

Edited messages are NOT re-indexed (botchat is append-mostly and the
API exposes no edit timestamp); the ``index-stream`` content-hash dedup
in the driver still skips unchanged bodies, so a ``--force`` reindex
picks up any edits cheaply.

Configurable:
  - ``config["api_base"]`` / ``$EICHI_BOTCHAT_API_BASE`` — base URL of
    the botchat HTTP API (default ``http://localhost:8111``).
  - ``config["page_size"]`` — messages per API page (default 500).
  - ``config["timeout"]`` — per-request HTTP timeout seconds (default 10).

No-op (yields nothing) when the API is unreachable — a connector must
never hard-fail the index run just because botchat happens to be down.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional


DEFAULT_API_BASE = "http://localhost:8111"
DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT = 10.0


def _resolve_api_base(config: Optional[Dict[str, Any]]) -> str:
    cfg = config or {}
    base = (
        os.environ.get("EICHI_BOTCHAT_API_BASE")
        or cfg.get("api_base")
        or DEFAULT_API_BASE
    )
    return str(base).rstrip("/")


def _fetch_page(
    api_base: str, *, limit: int, before: Optional[int], timeout: float
) -> List[Dict[str, Any]]:
    """Fetch one page of messages. Returns [] on any error (connector
    must no-op rather than crash the whole index run)."""
    url = f"{api_base}/api/messages?limit={limit}"
    if before is not None:
        url += f"&before={before}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return [m for m in msgs if isinstance(m, dict)]
        return []
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return []


def _render_text(msg: Dict[str, Any]) -> str:
    """Build the indexed body for one chat message.

    Markdown headers so the embedder picks up structure; the sender +
    topic + reply context are surfaced so a query can match on "who
    said what about X".
    """
    mid = msg.get("id")
    sender = str(msg.get("sender") or "?").strip() or "?"
    topic = str(msg.get("topic") or "").strip()
    body = str(msg.get("body") or "").strip()
    reply_to = msg.get("reply_to")
    attachments = msg.get("attachments")
    n_att = len(attachments) if isinstance(attachments, list) else 0

    header = f"# botchat message {mid} — {sender}"
    if topic:
        header += f" [topic: {topic}]"

    lines: List[str] = [header]
    if reply_to:
        lines.append(f"_(in reply to message {reply_to})_")
    lines.append("")
    lines.append(body or "_(empty message)_")
    if n_att:
        lines.append("")
        lines.append(f"_({n_att} attachment{'s' if n_att != 1 else ''})_")
    return "\n".join(lines)


def _build_doc(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mid = msg.get("id")
    if mid is None:
        return None
    try:
        mid_int = int(mid)
    except (TypeError, ValueError):
        return None
    body = str(msg.get("body") or "").strip()
    if not body:
        # Skip empty / attachment-only messages — nothing to embed.
        return None
    try:
        ts = float(msg.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    text = _render_text(msg)
    return {
        "source": "botchat",
        "doc_id": f"botchat:msg:{mid_int}",
        "text": text,
        "mtime": ts,
        "md": True,
        "metadata": {
            "message_id": mid_int,
            "sender": msg.get("sender") or None,
            "topic": msg.get("topic") or None,
            "reply_to": msg.get("reply_to"),
            "ts": ts or None,
            "library_added_at": ts or None,
        },
    }


def iter_documents(
    state: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield JSONL-shaped doc dicts for each new botchat message.

    Walks the botchat API newest-first, paging backwards, and yields
    docs for messages with ``id`` greater than the cursor's recorded
    ``max_id``. Stops early once a page is fully below the cursor (older
    than anything new), so steady-state runs fetch a single page.

    State shape::

        {"max_id": <int>}
    """
    cfg = config or {}
    if state is None:
        state = {}

    try:
        prev_max_id = int(state.get("max_id", 0) or 0)
    except (TypeError, ValueError):
        prev_max_id = 0

    api_base = _resolve_api_base(cfg)
    try:
        page_size = int(cfg.get("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    if page_size < 1:
        page_size = DEFAULT_PAGE_SIZE
    try:
        timeout = float(cfg.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    new_max_id = prev_max_id
    before: Optional[int] = None
    # Bound the walk so a runaway / misbehaving API can't loop forever.
    max_pages = 10_000

    for _ in range(max_pages):
        page = _fetch_page(api_base, limit=page_size, before=before, timeout=timeout)
        if not page:
            break

        page_ids: List[int] = []
        for msg in page:
            try:
                mid_int = int(msg.get("id"))
            except (TypeError, ValueError):
                continue
            page_ids.append(mid_int)
            if mid_int <= prev_max_id:
                # Already indexed on a previous run — skip.
                continue
            if mid_int > new_max_id:
                new_max_id = mid_int
            doc = _build_doc(msg)
            # Empty messages still advance the high-water mark (above)
            # so they are not re-walked every run.
            if doc is not None:
                yield doc

        if not page_ids:
            break
        oldest = min(page_ids)
        # The page reaches back to an already-indexed id: every older
        # page is already covered, so steady-state runs fetch one page.
        if oldest <= prev_max_id:
            break
        # Short page -> history exhausted.
        if len(page) < page_size:
            break
        # Page backwards: the API returns the newest ``limit`` messages
        # with id < before (ascending within the page); it ignores any
        # ``offset`` parameter.
        before = oldest

    state["max_id"] = new_max_id
