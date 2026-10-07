"""Backfill progress output."""

from __future__ import annotations

import io
import time

from eichi import connectors
from eichi.connectors import claude_jsonl
from eichi.progress import Progress, _fmt_eta


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _p(mode="on", **kw):
    c = Clock()
    out = io.StringIO()
    return Progress("c", mode=mode, stream=out, clock=c, **kw), c, out


def test_quiet_before_interval():
    p, c, out = _p()
    p.tick()
    assert out.getvalue() == ""


def test_exact_total_line():
    p, c, out = _p(exact_total=100)
    for _ in range(10):
        p.tick()
    c.t = 31
    p.tick()
    line = out.getvalue()
    assert "11 docs" in line and "11% of 100" in line and "~" not in line.split("ETA")[0]
    assert "docs/s" in line and "ETA" in line


def test_estimate_marked_and_capped():
    p, c, out = _p(estimate=lambda: 10)
    time.sleep(0.2)  # let estimator thread land
    p.tick(50)
    c.t = 31
    p.tick()
    line = out.getvalue()
    assert "~99% of ~10 est." in line and "~0 remaining" in line


def test_unknown_total_degrades():
    p, c, out = _p(estimate=lambda: None)
    p.tick(5)
    c.t = 31
    p.tick()
    line = out.getvalue()
    assert "6 docs, " in line and "docs/s" in line and "%" not in line


def test_estimator_exception_ignored():
    def boom():
        raise RuntimeError

    p, c, out = _p(estimate=boom)
    time.sleep(0.1)
    c.t = 31
    p.tick()
    assert "progress:" in out.getvalue()


def test_off_never_emits():
    p, c, out = _p(mode="off", exact_total=10)
    c.t = 99
    p.tick(5)
    assert out.getvalue() == ""


def test_auto_silent_for_small_runs_loud_for_big():
    p, c, out = _p(mode="auto")
    c.t = 31
    p.tick(10)
    assert out.getvalue() == ""
    p2, c2, out2 = _p(mode="auto")
    c2.t = 31
    p2.tick(600)
    assert "progress:" in out2.getvalue()
    p3, c3, out3 = _p(mode="auto", exact_total=9000)
    c3.t = 31
    p3.tick(3)
    assert "progress:" in out3.getvalue()


def test_fmt_eta():
    assert _fmt_eta(30) == "30s"
    assert _fmt_eta(600) == "10m"
    assert _fmt_eta(3 * 3600 + 60) == "3h01m"


def test_claude_jsonl_estimate(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.jsonl").write_text("{}\n" * 400)
    (proj / "b.jsonl").write_text("{}\n" * 300)
    monkeypatch.setenv("EICHI_CLAUDE_JSONL_ROOT", str(tmp_path))
    monkeypatch.setattr(claude_jsonl, "MIN_FILE_BYTES", 0)
    mt = (proj / "b.jsonl").stat().st_mtime
    assert claude_jsonl.estimate_pending({}, {}) == 700
    state = {"files": {"b": mt}}
    assert claude_jsonl.estimate_pending(state, {}) == 400
    assert state == {"files": {"b": mt}}


def test_builtin_hook_registered_and_optional():
    assert connectors.estimator_of("claude-jsonl") is not None
    assert connectors.estimator_of("http-conversation") is None
