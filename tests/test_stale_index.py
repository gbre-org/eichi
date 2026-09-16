"""Tests for the loud stale-index warning.

Covers the shared ``store.index_staleness`` helper and the CLI's
``_warn_if_stale`` stderr banner (fired on every read invocation).
"""
from __future__ import annotations

import datetime
import sqlite3
import time

from eichi import cli
from eichi.store import index_staleness


def _conn(indexed_at_values):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE files(path TEXT, indexed_at TEXT)")
    for i, val in enumerate(indexed_at_values):
        conn.execute("INSERT INTO files VALUES (?, ?)", (f"p{i}", val))
    conn.commit()
    return conn


def _utc(days_ago, ref):
    return datetime.datetime.utcfromtimestamp(ref - days_ago * 86400).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_index_staleness_stale():
    ref = time.time()
    info = index_staleness(_conn([_utc(80, ref)]), now=ref)
    assert info["stale_index"] is True
    assert info["warning"] and "EICHI INDEX STALE" in info["warning"]
    assert 79.5 <= info["index_age_days"] <= 80.5
    assert info["threshold_days"] == 7.0


def test_index_staleness_fresh():
    ref = time.time()
    info = index_staleness(_conn([_utc(1, ref)]), now=ref)
    assert info["stale_index"] is False
    assert info["warning"] is None
    assert info["last_indexed"] is not None


def test_index_staleness_empty_index_is_not_stale():
    info = index_staleness(_conn([]))
    assert info["stale_index"] is False
    assert info["last_indexed"] is None
    assert info["index_age_days"] is None


def test_index_staleness_threshold_env_override(monkeypatch):
    ref = time.time()
    monkeypatch.setenv("EICHI_STALE_DAYS", "0.5")
    info = index_staleness(_conn([_utc(1, ref)]), now=ref)
    assert info["stale_index"] is True  # 1d old vs 0.5d threshold


def test_warn_if_stale_prints_loud_banner(monkeypatch, capsys):
    monkeypatch.setattr(cli, "open_db", lambda db: _conn([]))
    monkeypatch.setattr(
        cli,
        "index_staleness",
        lambda conn: {"stale_index": True, "warning": "EICHI INDEX STALE: xyz"},
    )
    cli._warn_if_stale(None)
    err = capsys.readouterr().err
    assert "EICHI INDEX STALE" in err
    assert "⚠" in err  # warning sign
    assert "!!!" in err  # the loud bar


def test_warn_if_stale_silent_when_fresh(monkeypatch, capsys):
    monkeypatch.setattr(cli, "open_db", lambda db: _conn([]))
    monkeypatch.setattr(
        cli,
        "index_staleness",
        lambda conn: {"stale_index": False, "warning": None},
    )
    cli._warn_if_stale(None)
    assert capsys.readouterr().err == ""


def test_warn_if_stale_never_raises(monkeypatch):
    def boom(db):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(cli, "open_db", boom)
    cli._warn_if_stale(None)  # must swallow all errors
