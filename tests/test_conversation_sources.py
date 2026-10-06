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
    monkeypatch.setattr("eichi.cli.DEFAULT_DB_PATH", tmp_path / "default.db")
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


def _cursor(db, name):
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT v FROM meta WHERE k = ?", (f"connector_state:{name}",)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def test_sources_keep_separate_cursors(env):
    _run(env, "index", "--corpus", "alpha-chat", "--json")
    assert _cursor(env, "alpha-chat")["max_id"] == 2
    assert _cursor(env, "beta-inbox") is None
    (again,) = _run(env, "index", "--corpus", "alpha-chat", "--json")
    assert again["docs_indexed"] == 0


def test_cursor_follows_the_db(env, tmp_path):
    scratch = tmp_path / "scratch.db"
    _run(env, "index", "--corpus", "alpha-chat", "--json")
    before = _cursor(env, "alpha-chat")
    # A run against another DB must not touch A's cursor and starts empty.
    (res,) = _run(scratch, "index", "--corpus", "alpha-chat", "--json")
    assert res["docs_indexed"] == 2
    assert _cursor(env, "alpha-chat") == before
    # The shared legacy file is no longer written.
    assert not (tmp_path / "state.json").exists()
    # A fresh DB never inherits a cursor.
    fresh = tmp_path / "fresh.db"
    (res,) = _run(fresh, "index", "--corpus", "alpha-chat", "--json")
    assert res["docs_indexed"] == 2


def test_default_db_adopts_legacy_cursor_once(env, tmp_path, monkeypatch):
    default = tmp_path / "default.db"
    _run(default, "index", "--corpus", "alpha-chat", "--json")
    import sqlite3

    c = sqlite3.connect(str(default))
    c.execute("DELETE FROM meta WHERE k LIKE 'connector_state:%'")
    c.commit()
    c.close()
    (tmp_path / "state.json").write_text(
        json.dumps({"alpha-chat": {"max_id": 2}})
    )
    # Default DB with docs: continues from the legacy cursor, nothing re-run.
    (res,) = _run(default, "index", "--corpus", "alpha-chat", "--json")
    assert res["docs_indexed"] == 0
    assert _cursor(default, "alpha-chat")["max_id"] == 2
    # A non-default or doc-less DB never adopts it.
    (res,) = _run(tmp_path / "other.db", "index", "--corpus", "alpha-chat", "--json")
    assert res["docs_indexed"] == 2


def test_unknown_corpus_rejected(env, capsys):
    args = build_parser().parse_args(["--db", str(env), "index", "--corpus", "nope"])
    assert args.func(args) == 2
