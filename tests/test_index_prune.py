"""Tests for the reconcile pass on `eichi index <dir>`.

`index` is delta-only for content: it adds and updates. Before this pass
existed it never removed anything, so a renamed or deleted document kept
answering queries under its old name and with its old body.

The dangerous half of the feature is the prune predicate. "Not seen in
this indexing pass" is NOT the same as "deleted from disk" — the walk
skips unsupported extensions, oversized files and ignored directories.
Pruning on "not seen" would delete live entries, which is a far worse
bug than the ghost entry it fixes. The tests below pin the predicate to
disk state.

The embedder is stubbed; retrieval assertions use BM25 (fts5), which is
exact and needs no model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from eichi import EMBEDDING_DIM
from eichi.cli import build_parser
from eichi.store import list_files, open_db, search_bm25


def _stub_encode(texts, batch_size=32):
    n = len(texts)
    if n == 0:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    rng = np.random.default_rng(7)
    arr = rng.standard_normal((n, EMBEDDING_DIM)).astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
    return (arr / norms).astype(np.float32)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "prune.db"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    d = tmp_path / "corpus"
    d.mkdir()
    return d


def _run(argv, db_path: Path) -> int:
    parser = build_parser()
    args = parser.parse_args(["--db", str(db_path)] + argv)
    with patch("eichi.embed.encode", side_effect=_stub_encode):
        return args.func(args)


def _run_json(argv, db_path: Path, capsys) -> dict:
    payload, _err = _run_json_err(argv, db_path, capsys)
    return payload


def _run_json_err(argv, db_path: Path, capsys) -> tuple[dict, str]:
    rc = _run(argv + ["--json"], db_path)
    assert rc == 0
    captured = capsys.readouterr()
    return json.loads(captured.out.strip().splitlines()[-1]), captured.err


def _indexed_paths(db_path: Path) -> set[str]:
    return {p for p, _s, _c in list_files(open_db(db_path))}


# --- positive: a deleted file's entry goes away ----------------------------


def test_deleted_file_is_pruned_and_no_longer_returned(db_path, corpus, capsys):
    keep = corpus / "keep.md"
    doomed = corpus / "doomed.md"
    keep.write_text("notes about the persistent widget subsystem\n")
    doomed.write_text("notes about the doomed zygomorphic pipeline\n")

    payload = _run_json(["index", str(corpus)], db_path, capsys)
    assert payload["files_indexed"] == 2
    assert payload["files_pruned"] == 0

    conn = open_db(db_path)
    assert [h.path for h in search_bm25(conn, "zygomorphic")] == [str(doomed)]

    doomed.unlink()
    payload = _run_json(["index", str(corpus)], db_path, capsys)

    assert payload["files_pruned"] == 1
    assert payload["chunks_pruned"] >= 1
    assert payload["pruned_paths"] == [str(doomed)]
    assert _indexed_paths(db_path) == {str(keep)}

    conn = open_db(db_path)
    assert search_bm25(conn, "zygomorphic") == []
    # The surviving file is untouched and still findable.
    assert [h.path for h in search_bm25(conn, "widget")] == [str(keep)]


# --- the dangerous one: files the walker never visits must survive ---------


def test_entries_the_walk_skips_are_not_pruned(db_path, corpus, capsys):
    """Live files that this pass does not visit must keep their entries.

    Three deliberately-constructed skip classes, all indexed by an
    earlier pass that could see them, none visited by the second pass:

    * ignored directory — a file inside `.git/`, which the walk drops
      from its descent;
    * oversized file — indexed while small, then grown past
      MAX_FILE_BYTES;
    * unsupported extension — a `.weird` file, which `_eligible`
      rejects even when named directly. `_eligible` gates the
      single-file path too, so this entry is written through the store
      API — exactly how it arrives in reality when the extension list
      changes under an existing index, or when a connector stamps a
      real filesystem path.

    All three files are on disk when the directory pass runs, so all
    three entries must survive it.
    """
    from eichi import cli
    from eichi.store import add_chunks

    normal = corpus / "normal.md"
    normal.write_text("ordinary body text about the visible document\n")

    # 1. Ignored directory: .git is dropped from the walk's descent.
    gitdir = corpus / ".git"
    gitdir.mkdir()
    in_git = gitdir / "COMMIT_EDITMSG.md"
    in_git.write_text("commit message body mentioning octopodal merges\n")

    # 2. Oversized: indexed while small, grown past the cap afterwards.
    big = corpus / "big.md"
    big.write_text("small for now, mentions the brobdingnagian ledger\n")

    # Seed: the directory pass picks up normal + big; the .git file needs
    # an explicit single-file index because the walk never descends there.
    _run_json(["index", str(corpus)], db_path, capsys)
    _run_json(["index", str(in_git)], db_path, capsys)

    big.write_text("x" * (cli.MAX_FILE_BYTES + 1))

    # 3. Unsupported extension.
    odd = corpus / "odd.weird"
    odd.write_text("body text mentioning the quixotic sidecar format\n")
    add_chunks(
        open_db(db_path),
        source="file",
        path=str(odd),
        mtime=odd.stat().st_mtime,
        file_hash="deadbeef",
        chunks=[(0, 0, "body text mentioning the quixotic sidecar format")],
        embeddings=_stub_encode(["x"]),
    )

    seeded = _indexed_paths(db_path)
    assert seeded == {str(normal), str(in_git), str(big), str(odd)}

    # Sanity check that the three skip classes really are invisible to
    # this pass — otherwise the test proves nothing.
    visited = {str(p) for p in cli._walk(corpus) if cli._eligible(p)}
    assert visited == {str(normal)}, visited

    payload = _run_json(["index", str(corpus)], db_path, capsys)

    assert payload["files_pruned"] == 0, payload["pruned_paths"]
    assert _indexed_paths(db_path) == seeded

    conn = open_db(db_path)
    for term in ("octopodal", "brobdingnagian", "quixotic"):
        assert search_bm25(conn, term), f"{term} entry was pruned"


# --- single-file index must not prune siblings -----------------------------


def test_single_file_index_does_not_prune_siblings(db_path, corpus, capsys):
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("alpha document about tessellation\n")
    b.write_text("beta document about tessellation\n")
    _run_json(["index", str(corpus)], db_path, capsys)

    b.unlink()
    payload = _run_json(["index", str(a)], db_path, capsys)

    assert payload["files_pruned"] == 0
    # b is gone from disk but its entry survives: a single-file index
    # reconciles nothing but itself.
    assert _indexed_paths(db_path) == {str(a), str(b)}


def test_directory_index_does_not_touch_paths_outside_the_prefix(
    db_path, corpus, capsys
):
    inside = corpus / "inside.md"
    inside.write_text("inside the indexed tree\n")
    outside_dir = corpus.parent / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "outside.md"
    outside.write_text("outside the indexed tree\n")

    _run_json(["index", str(corpus)], db_path, capsys)
    _run_json(["index", str(outside_dir)], db_path, capsys)

    outside.unlink()
    payload = _run_json(["index", str(corpus)], db_path, capsys)

    assert payload["files_pruned"] == 0
    assert str(outside) in _indexed_paths(db_path)


# --- rename: the motivating case -------------------------------------------


def test_rename_leaves_only_the_new_path(db_path, corpus, capsys):
    old = corpus / "old-name.md"
    old.write_text("the document body mentions perspicacious findings\n")
    _run_json(["index", str(corpus)], db_path, capsys)

    new = corpus / "new-name.md"
    old.rename(new)
    payload = _run_json(["index", str(corpus)], db_path, capsys)

    assert payload["files_indexed"] == 1
    assert payload["files_pruned"] == 1
    assert _indexed_paths(db_path) == {str(new)}

    conn = open_db(db_path)
    hits = search_bm25(conn, "perspicacious")
    assert [h.path for h in hits] == [str(new)]


# --- opt-out ---------------------------------------------------------------


def test_no_prune_keeps_missing_entries(db_path, corpus, capsys):
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("alpha\n")
    b.write_text("beta\n")
    _run_json(["index", str(corpus)], db_path, capsys)

    b.unlink()
    payload = _run_json(["index", str(corpus), "--no-prune"], db_path, capsys)

    assert payload["files_pruned"] == 0
    assert _indexed_paths(db_path) == {str(a), str(b)}


def test_dry_run_reports_but_does_not_prune(db_path, corpus, capsys):
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("alpha\n")
    b.write_text("beta\n")
    _run_json(["index", str(corpus)], db_path, capsys)

    b.unlink()
    payload = _run_json(["index", str(corpus), "-n"], db_path, capsys)

    assert payload["dry_run"] is True
    assert payload["pruned_paths"] == [str(b)]
    assert payload["chunks_pruned"] == 0
    assert _indexed_paths(db_path) == {str(a), str(b)}


# --- safety guard: an empty walk is not evidence of deletion ---------------


def test_empty_walk_refuses_to_prune(db_path, corpus, capsys):
    """A tree that suddenly yields no indexable files is suspicious.

    That is what an unmounted volume looks like from the walker's side,
    and pruning would wipe a live corpus. We refuse and say why.
    """
    a = corpus / "a.md"
    b = corpus / "b.md"
    a.write_text("alpha\n")
    b.write_text("beta\n")
    _run_json(["index", str(corpus)], db_path, capsys)

    for f in (a, b):
        f.unlink()
    # Something must exist under the root or `index` bails on the root
    # itself; an unindexable file reproduces the "walk sees nothing"
    # shape without removing the directory.
    (corpus / "placeholder.bin").write_bytes(b"\x00")

    payload, err = _run_json_err(["index", str(corpus)], db_path, capsys)

    assert payload["prune_skipped"] == "no-eligible-files-found-in-walk"
    assert payload["files_pruned"] == 0
    assert _indexed_paths(db_path) == {str(a), str(b)}
    assert "NOT pruning" in err


# --- the predicate itself --------------------------------------------------


def test_path_is_gone_only_on_enoent(tmp_path):
    from eichi.cli import _path_is_gone

    real = tmp_path / "real.md"
    real.write_text("x")
    assert _path_is_gone(str(real)) is False
    assert _path_is_gone(str(tmp_path / "nope.md")) is True

    # A dangling symlink is PRESENT: the name is there, and the target
    # may be an unmounted volume that comes back.
    link = tmp_path / "link.md"
    os.symlink(str(tmp_path / "nowhere.md"), str(link))
    assert _path_is_gone(str(link)) is False

    # Any other OSError is UNKNOWN, not "deleted".
    with patch("os.lstat", side_effect=PermissionError):
        assert _path_is_gone(str(tmp_path / "whatever.md")) is False
    with patch("os.lstat", side_effect=OSError(5, "EIO")):
        assert _path_is_gone(str(tmp_path / "whatever.md")) is False


def test_prefix_matching_escapes_like_wildcards(db_path, tmp_path, capsys):
    """`_` is a LIKE wildcard — a sibling dir must not be swept in."""
    from eichi.store import paths_under

    under = tmp_path / "a_b"
    other = tmp_path / "axb"
    under.mkdir()
    other.mkdir()
    (under / "one.md").write_text("first\n")
    (other / "two.md").write_text("second\n")

    _run_json(["index", str(under)], db_path, capsys)
    _run_json(["index", str(other)], db_path, capsys)

    conn = open_db(db_path)
    assert paths_under(conn, str(under)) == [str(under / "one.md")]
