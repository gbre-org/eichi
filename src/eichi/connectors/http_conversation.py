"""Generic HTTP/JSON conversation connector.

Indexes a conversational corpus (chat messages, forum posts, an exported
message log, ...) from any source that serves JSON (or JSON Lines) over
HTTP or from a local ``file://`` URL. The connector knows nothing about
any particular backend: the endpoint, the field mapping, pagination and
auth all come from the ``[[corpus]]`` block in ``eichi.toml``. One
Document is emitted per message.

Configuration (all keys live in the corpus block)
-------------------------------------------------
``name``          Source tag for the indexed docs (any string you like).
``connector``     Must be ``"http-conversation"`` to select this module.
``url``           Required. Endpoint or ``file://`` URL to read.
``format``        ``"json"`` (default) or ``"jsonl"`` (one object/line).
``items_path``    Dotted path to the message list inside the response.
                  ``""`` (default) means the response itself is the list.
``headers``       Table of extra request headers. ``$VAR`` / ``${VAR}``
                  are expanded from the environment, so secrets stay out
                  of the config file.
``timeout``       Per-request timeout seconds (default 10).
``page_size``     Messages per request (default 500).
``order``         Hint for ``before`` paging; see below.

Field mapping (``[corpus.fields]``) — dotted paths into each message.
The values below are only generic defaults/examples::

    id         = "id"          # unique message id (int or string)
    body       = "body"        # message text
    author     = "author"      # who wrote it
    timestamp  = "timestamp"   # when it was sent
    thread     = "thread"      # topic / channel / conversation name
    reply_to   = "reply_to"    # id of the parent message
    attachments = "attachments"  # list; rendered as a count only

``timestamp_format``  ``"epoch"`` (seconds, default), ``"epoch_ms"`` or
``"iso8601"``.

Pagination (``[corpus.pagination]``)::

    mode = "none"     # default: a single request
    mode = "before"   # walk backwards: ?<limit_param>=N&<before_param>=<oldest id>
    mode = "page"     # ?<page_param>=1,2,...  (first_page = 1)
    mode = "offset"   # ?<offset_param>=0,N,2N,...
    limit_param, before_param, page_param, offset_param, first_page
    since_param       # optional: send the stored high-water id here so the
                      # server returns only newer messages

Incremental behaviour: the highest numeric message id seen is stored as
the cursor (``{"max_id": N}``). In ``before`` mode the walk stops as soon
as a page reaches an already-indexed id; in other modes every page is
fetched but unchanged messages are skipped by the driver's content-hash
dedup. Messages with non-numeric ids are always re-offered and deduped
the same way.

The connector yields nothing (never raises) when the source is
unreachable or returns something unparsable, so a down service cannot
fail the whole index run.

doc_id format: ``<source>:msg:<id>``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT = 10.0
MAX_PAGES = 10_000

# Generic example defaults for the field mapping; override per corpus.
DEFAULT_FIELDS: Dict[str, str] = {
    "id": "id",
    "body": "body",
    "author": "author",
    "timestamp": "timestamp",
    "thread": "thread",
    "reply_to": "reply_to",
    "attachments": "attachments",
}

DEFAULT_PAGINATION: Dict[str, Any] = {
    "mode": "none",
    "limit_param": "limit",
    "before_param": "before",
    "page_param": "page",
    "offset_param": "offset",
    "first_page": 1,
}


def _dig(obj: Any, path: str) -> Any:
    """Follow a dotted path through nested dicts (and list indexes)."""
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _read(url: str, headers: Dict[str, str], timeout: float) -> Optional[bytes]:
    """GET ``url``; return the body, or None on any transport error."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def _parse_items(raw: bytes, fmt: str, items_path: str) -> List[Dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
        if fmt == "jsonl":
            data: Any = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        else:
            data = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return []
    items = _dig(data, items_path)
    if not isinstance(items, list):
        return []
    return [m for m in items if isinstance(m, dict)]


def _with_query(url: str, params: Dict[str, Any]) -> str:
    if not params:
        return url
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return url + sep + urllib.parse.urlencode(params)


def _fetch_page(cfg: Dict[str, Any], params: Dict[str, Any]) -> List[Dict[str, Any]]:
    headers = {"Accept": "application/json"}
    for k, v in (cfg.get("headers") or {}).items():
        headers[str(k)] = os.path.expandvars(str(v))
    raw = _read(
        _with_query(str(cfg["url"]), params),
        headers,
        _float(cfg.get("timeout"), DEFAULT_TIMEOUT),
    )
    if raw is None:
        return []
    return _parse_items(raw, str(cfg.get("format") or "json"), str(cfg.get("items_path") or ""))


def _float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _int(value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _to_epoch(value: Any, fmt: str) -> float:
    if value is None or value == "":
        return 0.0
    try:
        if fmt == "iso8601":
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        num = float(value)
        return num / 1000.0 if fmt == "epoch_ms" else num
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _build_doc(
    msg: Dict[str, Any], source: str, fields: Dict[str, str], ts_format: str
) -> Optional[Dict[str, Any]]:
    mid = _dig(msg, fields["id"])
    if mid is None or _text(mid) == "":
        return None
    body = _text(_dig(msg, fields["body"]))
    if not body:
        return None  # nothing to embed
    author = _text(_dig(msg, fields["author"])) or "?"
    thread = _text(_dig(msg, fields["thread"]))
    reply_to = _dig(msg, fields["reply_to"])
    atts = _dig(msg, fields["attachments"])
    n_att = len(atts) if isinstance(atts, list) else 0
    ts = _to_epoch(_dig(msg, fields["timestamp"]), ts_format)

    header = f"# {source} message {mid} — {author}"
    if thread:
        header += f" [thread: {thread}]"
    lines = [header]
    if reply_to not in (None, "", 0):
        lines.append(f"_(in reply to message {reply_to})_")
    lines += ["", body]
    if n_att:
        lines += ["", f"_({n_att} attachment{'s' if n_att != 1 else ''})_"]
    return {
        "source": source,
        "doc_id": f"{source}:msg:{mid}",
        "text": "\n".join(lines),
        "mtime": ts,
        "md": True,
        "metadata": {
            "message_id": mid,
            "author": author if author != "?" else None,
            "thread": thread or None,
            "reply_to": reply_to if reply_to not in ("", 0) else None,
            "ts": ts or None,
            "library_added_at": ts or None,
        },
    }


def _page_params(
    pag: Dict[str, Any], page_size: int, before: Optional[Any], n: int, prev_max: int
) -> Dict[str, Any]:
    mode = pag["mode"]
    params: Dict[str, Any] = {}
    if mode != "none":
        params[pag["limit_param"]] = page_size
    if mode == "before" and before is not None:
        params[pag["before_param"]] = before
    elif mode == "page":
        params[pag["page_param"]] = _int(pag["first_page"], 1) + n
    elif mode == "offset":
        params[pag["offset_param"]] = n * page_size
    if pag.get("since_param") and prev_max:
        params[pag["since_param"]] = prev_max
    return params


# Declared connector kind: a live conversation source (see
# eichi.connectors for the contract).
KIND = "conversation"
# Corpora are user-named; there is no implicit corpus called after the
# connector itself, so --conversations only includes configured ones.
MULTI_CORPUS = True


def iter_documents(
    state: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield one doc dict per new message. See the module docstring."""
    cfg = config or {}
    if state is None:
        state = {}
    if not cfg.get("url"):
        return
    source = str(cfg.get("source") or cfg.get("name") or "conversation")
    fields = {**DEFAULT_FIELDS, **{k: str(v) for k, v in (cfg.get("fields") or {}).items()}}
    pag = {**DEFAULT_PAGINATION, **(cfg.get("pagination") or {})}
    mode = str(pag["mode"])
    if mode not in ("none", "before", "page", "offset"):
        return
    page_size = _int(cfg.get("page_size"), DEFAULT_PAGE_SIZE)
    ts_format = str(cfg.get("timestamp_format") or "epoch")
    prev_max = _as_int(state.get("max_id")) or 0
    new_max = prev_max

    before: Optional[Any] = None
    seen: set = set()
    for n in range(MAX_PAGES if mode != "none" else 1):
        page = _fetch_page(cfg, _page_params(pag, page_size, before, n, prev_max))
        if not page:
            break
        numeric: List[Tuple[int, Dict[str, Any]]] = []
        fresh = 0
        for msg in page:
            mid = _dig(msg, fields["id"])
            key = _text(mid)
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            num = _as_int(mid)
            if num is not None:
                numeric.append((num, msg))
                if num <= prev_max:
                    continue
                new_max = max(new_max, num)
            doc = _build_doc(msg, source, fields, ts_format)
            if doc is not None:
                yield doc
        if mode == "none" or len(page) < page_size or fresh == 0:
            break
        if mode == "before":
            if not numeric:
                break
            oldest = min(n_ for n_, _ in numeric)
            if oldest <= prev_max:
                break
            before = oldest
    state["max_id"] = new_max
