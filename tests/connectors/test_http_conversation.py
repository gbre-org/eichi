"""Tests for the generic http-conversation connector (synthetic data only)."""

from __future__ import annotations

import json

from eichi.connectors import http_conversation as hc


def _msg(mid, body, *, author="alice", ts=None, thread=None, reply_to=None,
         attachments=None):
    return {
        "id": mid, "body": body, "author": author,
        "timestamp": ts if ts is not None else 1_700_000_000.0 + mid,
        "thread": thread, "reply_to": reply_to,
        "attachments": attachments or [],
    }


def _stub(monkeypatch, store, *, mode="before"):
    """Replace the transport with an in-memory server over ``store``."""
    calls = []

    def fake(cfg, params):
        calls.append(dict(params))
        limit = int(params.get("limit", 10**9))
        msgs = sorted(store, key=lambda m: m["id"])
        if mode == "before":
            if "before" in params:
                msgs = [m for m in msgs if m["id"] < int(params["before"])]
            return msgs[-limit:]
        if mode == "page":
            start = (int(params["page"]) - 1) * limit
            return msgs[start:start + limit]
        return msgs

    monkeypatch.setattr(hc, "_fetch_page", fake)
    return calls


CFG = {"url": "http://chat.example.test/api", "source": "demo-chat",
       "pagination": {"mode": "before"}}


def test_doc_shape(monkeypatch):
    _stub(monkeypatch, [_msg(1, "hello", thread="intro", reply_to=None),
                        _msg(2, "reply", reply_to=1, attachments=[1, 2])])
    docs = {d["doc_id"]: d for d in hc.iter_documents(config=CFG)}
    assert set(docs) == {"demo-chat:msg:1", "demo-chat:msg:2"}
    d1, d2 = docs["demo-chat:msg:1"], docs["demo-chat:msg:2"]
    assert d1["source"] == "demo-chat"
    assert "[thread: intro]" in d1["text"] and "alice" in d1["text"]
    assert "in reply to message 1" in d2["text"]
    assert "2 attachments" in d2["text"]
    assert d2["metadata"]["reply_to"] == 1
    assert d1["mtime"] == 1_700_000_001.0


def test_field_mapping_nested_and_iso(monkeypatch):
    raw = [{"uid": "m-9", "content": {"text": "nested body"},
            "from": {"name": "bob"}, "created": "2024-01-02T03:04:05Z",
            "subject": "s1"}]
    monkeypatch.setattr(hc, "_fetch_page", lambda cfg, params: raw)
    cfg = {"url": "x://y", "source": "alt", "timestamp_format": "iso8601",
           "fields": {"id": "uid", "body": "content.text",
                      "author": "from.name", "timestamp": "created",
                      "thread": "subject"}}
    (d,) = hc.iter_documents(config=cfg)
    assert d["doc_id"] == "alt:msg:m-9"
    assert "nested body" in d["text"] and "bob" in d["text"]
    assert d["mtime"] == 1704164645.0
    assert d["metadata"]["thread"] == "s1"


def test_timestamp_epoch_ms():
    assert hc._to_epoch(1_700_000_000_000, "epoch_ms") == 1_700_000_000.0
    assert hc._to_epoch("garbage", "iso8601") == 0.0


def test_before_paging_walks_everything(monkeypatch):
    _stub(monkeypatch, [_msg(i, f"m{i}") for i in range(1, 7)])
    state = {}
    docs = list(hc.iter_documents(state=state, config={**CFG, "page_size": 2}))
    assert len(docs) == 6 and state["max_id"] == 6


def test_cursor_stops_early(monkeypatch):
    calls = _stub(monkeypatch, [_msg(i, f"m{i}") for i in range(1, 7)])
    state = {"max_id": 4}
    docs = list(hc.iter_documents(state=state, config={**CFG, "page_size": 2}))
    assert [d["doc_id"] for d in docs] == ["demo-chat:msg:5", "demo-chat:msg:6"]
    assert len(calls) == 2 and state["max_id"] == 6


def test_page_mode_and_since_param(monkeypatch):
    calls = _stub(monkeypatch, [_msg(i, f"m{i}") for i in range(1, 6)], mode="page")
    cfg = {**CFG, "page_size": 2,
           "pagination": {"mode": "page", "since_param": "after_id"}}
    docs = list(hc.iter_documents(state={"max_id": 2}, config=cfg))
    assert [d["doc_id"] for d in docs] == [f"demo-chat:msg:{i}" for i in (3, 4, 5)]
    assert all(c.get("after_id") == 2 for c in calls)
    assert [c["page"] for c in calls] == [1, 2, 3]


def test_empty_and_attachment_only_skipped_but_advance_cursor(monkeypatch):
    _stub(monkeypatch, [_msg(1, "ok"), _msg(2, "   "), _msg(3, "")])
    state = {}
    docs = list(hc.iter_documents(state=state, config=CFG))
    assert len(docs) == 1 and state["max_id"] == 3


def test_unreachable_and_misconfigured_noop(monkeypatch):
    monkeypatch.setattr(hc, "_read", lambda *a, **k: None)
    assert list(hc.iter_documents(config=CFG)) == []
    assert list(hc.iter_documents(config={})) == []
    assert list(hc.iter_documents(config={**CFG, "pagination": {"mode": "bogus"}})) == []


def test_file_url_json_and_jsonl(tmp_path):
    msgs = [_msg(1, "one"), _msg(2, "two")]
    j = tmp_path / "export.json"
    j.write_text(json.dumps({"data": {"items": msgs}}))
    cfg = {"url": j.as_uri(), "source": "export", "items_path": "data.items"}
    assert len(list(hc.iter_documents(config=cfg))) == 2
    jl = tmp_path / "export.jsonl"
    jl.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
    cfg = {"url": jl.as_uri(), "source": "export", "format": "jsonl"}
    assert len(list(hc.iter_documents(config=cfg))) == 2


def test_header_env_expansion(monkeypatch):
    seen = {}

    def fake_read(url, headers, timeout):
        seen.update(headers)
        return b"[]"

    monkeypatch.setenv("DEMO_TOKEN", "s3cret")
    monkeypatch.setattr(hc, "_read", fake_read)
    list(hc.iter_documents(config={
        "url": "http://h.example.test/", "headers": {"Authorization": "Bearer ${DEMO_TOKEN}"}}))
    assert seen["Authorization"] == "Bearer s3cret"
