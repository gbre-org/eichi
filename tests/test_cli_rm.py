"""Tests for `eichi rm` — path form, --doc-id form, and dry-run counting.

No embedding model is needed: rows are inserted with `store.add_chunks` and
stub vectors, and `cmd_rm` never embeds. sqlite-vec is loaded by `open_db`
(the `chunks` table is a vec0 virtual table), same as the store tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from eichi import EMBEDDING_DIM
from eichi.cli import build_parser
from eichi.store import add_chunks, list_files, open_db, stats

STREAM_ID = "repo-md:notes:memory/feedback_a.md"
SIBLING_ID = "repo-md:notes:memory/feedbackXa.md"


def _stub_embeddings(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    arr = rng.standard_normal((n, EMBEDDING_DIM)).astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
    return (arr / norms).astype(np.float32)


@pytest.fixture
def db_path():
    tmp = tempfile.mkdtemp()
    p = Path(tmp) / "rm.db"
    yield p
    for f in p.parent.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        p.parent.rmdir()
    except OSError:
        pass


def _seed(db_path, paths):
    conn = open_db(db_path)
    try:
        for i, path in enumerate(paths):
            add_chunks(
                conn,
                source="test",
                path=path,
                mtime=1.0,
                file_hash=f"h{i}",
                chunks=[(0, 0, f"body {i}"), (1, 10, f"more {i}")],
                embeddings=_stub_embeddings(2, seed=i),
            )
    finally:
        conn.close()


def _paths(db_path):
    conn = open_db(db_path)
    try:
        return sorted(row[0] for row in list_files(conn))
    finally:
        conn.close()


def _chunk_count(db_path):
    conn = open_db(db_path)
    try:
        return stats(conn)["chunk_count"]
    finally:
        conn.close()


def _run(db_path, argv):
    args = build_parser().parse_args(["--db", str(db_path)] + argv)
    return args.func(args)


def test_rm_doc_id_removes_exactly_that_doc(db_path, capsys):
    _seed(db_path, [STREAM_ID, SIBLING_ID])

    rc = _run(db_path, ["rm", "--doc-id", STREAM_ID, "--json"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out == {"removed_chunks": 2, "path": STREAM_ID}
    assert _paths(db_path) == [SIBLING_ID]
    assert _chunk_count(db_path) == 2


def test_rm_positional_path_still_resolves(db_path, tmp_path, capsys):
    doc = tmp_path / "note.md"
    doc.write_text("hello")
    other = tmp_path / "other.md"
    other.write_text("hello")
    _seed(db_path, [str(doc), str(other)])

    rc = _run(db_path, ["rm", str(doc)])
    assert rc == 0
    assert "removed 2 chunks" in capsys.readouterr().out
    assert _paths(db_path) == [str(other)]


def test_rm_relative_path_is_resolved_against_cwd(db_path, tmp_path, monkeypatch):
    doc = tmp_path / "note.md"
    doc.write_text("hello")
    _seed(db_path, [str(doc)])

    monkeypatch.chdir(tmp_path)
    assert _run(db_path, ["rm", "note.md"]) == 0
    assert _paths(db_path) == []


def test_rm_without_path_or_doc_id_exits_2(db_path, capsys):
    _seed(db_path, [STREAM_ID])

    assert _run(db_path, ["rm"]) == 2
    assert "--doc-id" in capsys.readouterr().err
    # Nothing was touched.
    assert _paths(db_path) == [STREAM_ID]


def test_rm_path_and_doc_id_are_mutually_exclusive(db_path):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["rm", "/tmp/x", "--doc-id", STREAM_ID])
    assert exc.value.code == 2


def test_rm_doc_id_dry_run_counts_without_deleting(db_path, capsys):
    _seed(db_path, [STREAM_ID, SIBLING_ID])

    assert _run(db_path, ["rm", "--doc-id", STREAM_ID, "-n", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"would_remove_chunks": 2, "path": STREAM_ID}

    assert _paths(db_path) == [SIBLING_ID, STREAM_ID]
    assert _chunk_count(db_path) == 4


def test_rm_dry_run_count_escapes_like_wildcards(db_path, capsys):
    """`_` in a doc id must not make the dry-run count siblings."""
    _seed(db_path, ["a_b.md", "axb.md/child"])

    assert _run(db_path, ["rm", "--doc-id", "a_b.md", "-n", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["would_remove_chunks"] == 2  # only a_b.md's own two chunks


def test_rm_doc_id_leaves_wildcard_siblings_alone(db_path):
    _seed(db_path, ["a_b.md", "axb.md/child", "a_b.md/child"])

    assert _run(db_path, ["rm", "--doc-id", "a_b.md"]) == 0
    # The exact doc and its real "/"-prefixed child go; the wildcard
    # look-alike stays.
    assert _paths(db_path) == ["axb.md/child"]
