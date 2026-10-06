"""Two http-conversation sources configured at once: each indexes under its
own source name and is separately addressable by the cross-source flags."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import numpy as np
import pytest

from eichi import EMBEDDING_DIM
from eichi.cli import build_parser


def _vecs(texts, **_):
    out = np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float32)
    out[:, 0] = 1.0
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    a = tmp_path / "alpha.json"
    a.write_text(json.dumps({"messages": [
        {"id": 1, "body": "deploy plan for the widget", "sender": "alice",
         "ts": 1_700_000_000, "topic": "ops"},
        {"id": 2, "body": "widget rollout done", "sender": "bob", "ts": 1_700_000_100}]}))
    b = tmp_path / "beta.jsonl"
    b.write_text("\n".join(json.dumps(m) for m in [
        {"uid": "x1", "content": {"text": "widget complaint from customer"},
         "from": "carol", "at": "2024-01-02T03:04:05Z"},
        {"uid": "x2", "content": {"text": "widget refund issued"},
         "from": "dave", "at": "2024-01-03T03:04:05Z"}]) + "\n")
    cfg = tmp_path / "eichi.toml"
    cfg.write_text(f'''
[[corpus]]
name = "alpha-chat"
connector = "http-conversation"
url = "{a.as_uri()}"
items_path = "messages"
[corpus.fields]
author = "sender"
timestamp = "ts"
thread = "topic"

[[corpus]]
name = "beta-inbox"
connector = "http-conversation"
url = "{b.as_uri()}"
format = "jsonl"
timestamp_format = "iso8601"
[corpus.fields]
id = "uid"
body = "content.text"
author = "from"
timestamp = "at"
''')
    monkeypatch.setenv("EICHI_CONFIG", str(cfg))
    monkeypatch.setenv("EICHI_CONNECTOR_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("EICHI_NO_QUERY_LOG", "1")
    return tmp_path / "idx.db"


def _run(db, *argv):
    args = build_parser().parse_args(["--db", str(db), *argv])
    buf = io.StringIO()
    with patch("eichi.embed.encode", side_effect=_vecs), patch(
        "eichi.embed.encode_one", return_value=_vecs(["q"])[0]
    ), redirect_stdout(buf):
        assert args.func(args) == 0
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]


def test_two_sources_index_and_filter(env):
    for name in ("alpha-chat", "beta-inbox"):
        (res,) = _run(env, "index", "--corpus", name, "--json")
        assert res["docs_indexed"] == 2 and res["errors"] == 0

    q = ["query", "widget", "--json", "--retrieval", "bm25", "-k", "10"]
    both = _run(env, *q)
    assert {r["source"] for r in both} == {"alpha-chat", "beta-inbox"}
    assert {r["source"] for r in _run(env, *q, "--source", "alpha-chat")} == {"alpha-chat"}
    assert {r["source"] for r in _run(env, *q, "--source", "beta-inbox")} == {"beta-inbox"}
    assert {r["source"] for r in _run(env, *q, "--source", "alpha-chat,beta-inbox")} == {
        "alpha-chat", "beta-inbox"}
    assert {r["source"] for r in _run(env, *q, "--exclude-source", "alpha-chat")} == {"beta-inbox"}
    fused = _run(env, *q, "--source", "alpha-chat,beta-inbox", "--per-source", "1")
    assert {r["source"] for r in fused} == {"alpha-chat", "beta-inbox"}
    assert all(r["fused_score"] is not None for r in fused)
    conv = _run(env, *q, "--conversations")
    assert {r["source"] for r in conv} == {"alpha-chat", "beta-inbox"}


def test_sources_keep_separate_cursors(env):
    _run(env, "index", "--corpus", "alpha-chat", "--json")
    state = json.loads(open(env.parent / "state.json").read())
    assert state["alpha-chat"]["max_id"] == 2 and "beta-inbox" not in state
    (again,) = _run(env, "index", "--corpus", "alpha-chat", "--json")
    assert again["docs_indexed"] == 0


def test_unknown_corpus_rejected(env, capsys):
    args = build_parser().parse_args(["--db", str(env), "index", "--corpus", "nope"])
    assert args.func(args) == 2
