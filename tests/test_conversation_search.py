"""Cross-source conversation search: source scoping, per-source RRF,
--since on event time, recency boost, path collapse, --conversations."""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from unittest.mock import patch

import numpy as np
import pytest

from eichi import EMBEDDING_DIM
from eichi.cli import build_parser
from eichi.store import (
    SearchHit,
    add_chunks,
    collapse_paths,
    fuse_groups,
    open_db,
    recency_factor,
)

DAY = 86400.0


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


QVEC = _vec(1)


def _near(i: int) -> np.ndarray:
    """Vector close to QVEC; larger i = slightly farther."""
    v = QVEC + _vec(1000 + i) * (0.05 + 0.01 * i)
    return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture
def db(tmp_path):
    now = time.time()
    conn = open_db(tmp_path / "c.db")
    n = 0

    def put(source, path, text, age_days, chunk=0):
        nonlocal n
        n += 1
        add_chunks(
            conn, source=source, path=path, mtime=now - age_days * DAY,
            file_hash=f"h{n}", chunks=[(chunk, 0, text)],
            embeddings=_near(n)[None, :],
        )

    # Stale noisy source: many near-duplicate chunks, close to the query.
    for i in range(8):
        put("transcripts", f"/t/old{i}.md", f"rc prep old {i}", 40 + i)
    # Live sources, slightly farther in vector space but recent.
    put("botchat", "botchat:1", "rc prep yesterday", 1)
    put("claude-jsonl", "jsonl:1", "rc prep session", 2)
    conn.close()
    return tmp_path / "c.db"


def run(argv, db):
    args = build_parser().parse_args(["--db", str(db), "query"] + argv)
    buf = io.StringIO()
    with patch("eichi.embed.encode_one", return_value=QVEC), patch.dict(
        "os.environ", {"EICHI_NO_QUERY_LOG": "1"}
    ):
        with redirect_stdout(buf):
            assert args.func(args) == 0
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]


def test_baseline_stale_source_crowds(db):
    rows = run(["rc prep", "-k", "5", "--json", "--retrieval", "vector"], db)
    assert {r["source"] for r in rows} == {"transcripts"}


def test_comma_source_scopes(db):
    rows = run(["rc prep", "--source", "botchat,claude-jsonl", "--json",
                "--retrieval", "vector"], db)
    assert {r["source"] for r in rows} == {"botchat", "claude-jsonl"}


def test_exclude_source(db):
    rows = run(["rc prep", "--exclude-source", "transcripts", "--json",
                "--retrieval", "vector"], db)
    assert {r["source"] for r in rows} == {"botchat", "claude-jsonl"}


def test_per_source_rrf_interleaves(db):
    rows = run(["rc prep", "-k", "3", "--per-source", "3", "--json",
                "--retrieval", "vector"], db)
    srcs = [r["source"] for r in rows]
    assert "botchat" in srcs and "claude-jsonl" in srcs
    assert all(r["fused_score"] is not None for r in rows)


def test_since_filters_on_event_time(db):
    rows = run(["rc prep", "--since", "3d", "--json", "--retrieval", "vector"], db)
    assert {r["source"] for r in rows} == {"botchat", "claude-jsonl"}
    rows = run(["rc prep", "--since", "3d", "--json", "--retrieval", "hybrid"], db)
    assert {r["source"] for r in rows} <= {"botchat", "claude-jsonl"}


def test_added_since_still_needs_library_added_at(db):
    rows = run(["rc prep", "--added-since", "3d", "--json"], db)
    assert rows == []


def test_conversations_preset(db):
    rows = run(["rc prep", "--conversations", "--json"], db)
    assert rows
    assert {r["source"] for r in rows} <= {"botchat", "claude-jsonl",
                                           "claude-watch-queue"}
    assert rows[0]["source"] == "botchat"


def test_conversations_source_narrows(db):
    rows = run(["rc prep", "--conversations", "--source", "claude-jsonl",
                "--json"], db)
    assert {r["source"] for r in rows} == {"claude-jsonl"}


def test_recency_boost_promotes_recent(db):
    rows = run(["rc prep", "--recency-boost", "3d", "-k", "3", "--json",
                "--retrieval", "vector"], db)
    assert rows[0]["source"] in ("botchat", "claude-jsonl")


def test_bad_args(db):
    for argv in (["x", "--since", "bogus"], ["x", "--per-source", "0"],
                 ["x", "--recency-boost", "0d"]):
        args = build_parser().parse_args(["--db", str(db), "query"] + argv)
        with patch("eichi.embed.encode_one", return_value=QVEC), patch.dict(
            "os.environ", {"EICHI_NO_QUERY_LOG": "1"}
        ):
            assert args.func(args) == 2


def _h(rowid, path, mtime=0.0):
    return SearchHit(rowid=rowid, score=0.5, source="s", path=path,
                     chunk_idx=0, offset=0, text="t", mtime=mtime)


def test_collapse_paths_keeps_first():
    out = collapse_paths([_h(1, "a"), _h(2, "a"), _h(3, "b")])
    assert [h.rowid for h in out] == [1, 3]


def test_collapse_cli_duplicate_path(tmp_path):
    conn = open_db(tmp_path / "d.db")
    for i in range(2):
        add_chunks(conn, source="botchat", path="botchat:dup", mtime=time.time(),
                   file_hash="h", chunks=[(i, 0, f"same msg {i}")],
                   embeddings=_near(i)[None, :])
    conn.close()
    rows = run(["same", "--collapse-paths", "--json", "--retrieval", "vector"],
               tmp_path / "d.db")
    assert len(rows) == 1


def test_fuse_groups_rrf_and_dedup_rowid():
    a = [_h(1, "a"), _h(2, "b")]
    b = [_h(1, "a"), _h(3, "c")]
    out = fuse_groups([a, b])
    assert out[0].rowid == 1
    assert len(out) == 3


def test_fuse_groups_recency():
    now = 1_000_000.0
    old = _h(1, "o", mtime=now - 100 * DAY)
    new = _h(2, "n", mtime=now - 1 * DAY)
    out = fuse_groups([[old, new]], halflife_s=7 * DAY, now=now)
    assert out[0].rowid == 2


def test_recency_factor_bounds():
    assert recency_factor(0, 100, 10) == pytest.approx(0.2)
    assert recency_factor(100, 100, 10) == pytest.approx(1.0)
    assert 0.2 < recency_factor(90, 100, 10) < 1.0
