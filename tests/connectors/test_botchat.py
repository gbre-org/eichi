"""Tests for the botchat connector.

No network calls — ``_fetch_page`` is monkeypatched to serve canned
pages so the connector's paging / cursor / doc-shaping logic is
exercised against a deterministic fake API.
"""

from __future__ import annotations

from eichi.connectors import botchat


def _msg(mid, body, *, sender="andrew", ts=None, topic=None, reply_to=None,
         attachments=None):
    return {
        "id": mid,
        "body": body,
        "sender": sender,
        "ts": ts if ts is not None else 1_700_000_000.0 + mid,
        "topic": topic,
        "reply_to": reply_to,
        "attachments": attachments or [],
        "reactions": {},
    }


def _fake_api(pages):
    """Return a _fetch_page replacement that mimics the real API.

    The real API ignores ``offset``: it returns the newest ``limit``
    messages with ``id < before`` (all messages when ``before`` is
    omitted), ascending by id within the page. ``pages`` is only
    flattened into the message store.
    """
    store = sorted((m for page in pages for m in page), key=lambda m: m["id"])

    def _fetch(api_base, *, limit, before, timeout):
        eligible = [m for m in store if before is None or m["id"] < before]
        return eligible[-limit:]

    return _fetch


def test_emits_one_doc_per_message(monkeypatch):
    msgs = [_msg(3, "third"), _msg(2, "second"), _msg(1, "first")]  # newest-first
    monkeypatch.setattr(botchat, "_fetch_page", _fake_api([msgs]))
    docs = list(botchat.iter_documents(config={"page_size": 500}))
    assert len(docs) == 3
    assert {d["source"] for d in docs} == {"botchat"}
    assert {d["doc_id"] for d in docs} == {
        "botchat:msg:1",
        "botchat:msg:2",
        "botchat:msg:3",
    }
    # Body text + sender are present in the rendered doc.
    bodies = {d["doc_id"]: d["text"] for d in docs}
    assert "first" in bodies["botchat:msg:1"]
    assert "andrew" in bodies["botchat:msg:1"]


def test_cursor_skips_already_indexed(monkeypatch):
    msgs = [_msg(3, "third"), _msg(2, "second"), _msg(1, "first")]
    monkeypatch.setattr(botchat, "_fetch_page", _fake_api([msgs]))
    state = {"max_id": 2}
    docs = list(botchat.iter_documents(state=state, config={}))
    # Only message 3 is newer than the cursor.
    assert [d["doc_id"] for d in docs] == ["botchat:msg:3"]
    assert state["max_id"] == 3


def test_cursor_advances_to_high_water_mark(monkeypatch):
    msgs = [_msg(5, "e"), _msg(4, "d"), _msg(3, "c")]
    monkeypatch.setattr(botchat, "_fetch_page", _fake_api([msgs]))
    state = {}
    list(botchat.iter_documents(state=state, config={}))
    assert state["max_id"] == 5


def test_empty_body_messages_skipped_but_advance_cursor(monkeypatch):
    msgs = [_msg(2, ""), _msg(1, "real content")]  # msg 2 is empty
    monkeypatch.setattr(botchat, "_fetch_page", _fake_api([msgs]))
    state = {}
    docs = list(botchat.iter_documents(state=state, config={}))
    assert [d["doc_id"] for d in docs] == ["botchat:msg:1"]
    # Cursor still advances past the empty message so it isn't re-walked.
    assert state["max_id"] == 2


def test_paging_walks_multiple_pages(monkeypatch):
    # 3 pages of 2 messages each (newest-first overall).
    pages = [
        [_msg(6, "f"), _msg(5, "e")],
        [_msg(4, "d"), _msg(3, "c")],
        [_msg(2, "b"), _msg(1, "a")],
    ]
    monkeypatch.setattr(botchat, "_fetch_page", _fake_api(pages))
    docs = list(botchat.iter_documents(config={"page_size": 2}))
    assert len(docs) == 6
    assert {d["doc_id"] for d in docs} == {
        f"botchat:msg:{i}" for i in range(1, 7)
    }


def test_paging_stops_early_when_page_fully_below_cursor(monkeypatch):
    pages = [
        [_msg(6, "f"), _msg(5, "e")],
        [_msg(4, "d"), _msg(3, "c")],
        [_msg(2, "b"), _msg(1, "a")],
    ]
    calls = {"n": 0}
    base_fetch = _fake_api(pages)

    def _counting_fetch(api_base, *, limit, before, timeout):
        calls["n"] += 1
        return base_fetch(api_base, limit=limit, before=before, timeout=timeout)

    monkeypatch.setattr(botchat, "_fetch_page", _counting_fetch)
    state = {"max_id": 4}  # messages 5,6 are new; 3,4 and below are not
    docs = list(botchat.iter_documents(state=state, config={"page_size": 2}))
    assert [d["doc_id"] for d in docs] == ["botchat:msg:6", "botchat:msg:5"]
    # Page 1 (5,6) is new; page 2 (3,4) reaches the cursor -> stop.
    assert calls["n"] == 2
    assert state["max_id"] == 6


def test_unreachable_api_noops(monkeypatch):
    monkeypatch.setattr(
        botchat, "_fetch_page", lambda *a, **k: []
    )
    docs = list(botchat.iter_documents(config={}))
    assert docs == []


def test_metadata_carries_sender_topic_reply(monkeypatch):
    msgs = [_msg(1, "hello", sender="wb", topic="deploy", reply_to=99, ts=1234.5)]
    monkeypatch.setattr(botchat, "_fetch_page", _fake_api([msgs]))
    (doc,) = list(botchat.iter_documents(config={}))
    md = doc["metadata"]
    assert md["message_id"] == 1
    assert md["sender"] == "wb"
    assert md["topic"] == "deploy"
    assert md["reply_to"] == 99
    assert doc["mtime"] == 1234.5
    assert doc["md"] is True


def test_api_base_resolution_env_wins(monkeypatch):
    monkeypatch.setenv("EICHI_BOTCHAT_API_BASE", "http://env-host:9999/")
    assert botchat._resolve_api_base({"api_base": "http://cfg-host:1111"}) == (
        "http://env-host:9999"
    )
    monkeypatch.delenv("EICHI_BOTCHAT_API_BASE", raising=False)
    assert botchat._resolve_api_base({"api_base": "http://cfg-host:1111/"}) == (
        "http://cfg-host:1111"
    )
    assert botchat._resolve_api_base(None) == botchat.DEFAULT_API_BASE


def test_incremental_run_fetches_single_page(monkeypatch):
    msgs = [_msg(i, f"m{i}") for i in range(1, 11)]
    calls = {"n": 0}
    base_fetch = _fake_api([msgs])

    def _counting_fetch(api_base, *, limit, before, timeout):
        calls["n"] += 1
        return base_fetch(api_base, limit=limit, before=before, timeout=timeout)

    monkeypatch.setattr(botchat, "_fetch_page", _counting_fetch)
    state = {"max_id": 8}
    docs = list(botchat.iter_documents(state=state, config={"page_size": 5}))
    assert [d["doc_id"] for d in docs] == ["botchat:msg:9", "botchat:msg:10"]
    assert calls["n"] == 1
    assert state["max_id"] == 10
