"""Human-friendly progress lines for large indexing runs.

A multi-hour backfill that prints nothing looks exactly like a hung
process (and gets killed by supervisors). :class:`Progress` emits one
line to stderr every ``interval`` seconds::

    progress: claude-jsonl: 41,200 docs, ~25% of ~165,000 est., ~134,000 remaining, 38 docs/s, ETA ~55m

Modes:

``on``    always report.
``off``   never report.
``auto``  (default) stay silent until the run proves itself large:
          either the connector's estimate of pending work reaches
          ``AUTO_ESTIMATE_THRESHOLD`` or ``AUTO_DOCS_THRESHOLD`` docs have
          been processed. A steady-state scheduled tick therefore logs
          nothing.

Totals come from an optional connector hook (``estimate_pending``, see
``eichi.connectors``); eichi never inspects the backend itself. The hook
runs in a daemon thread so it can never delay indexing; until it answers
(or when it returns ``None``) the line degrades to "N docs, rate".
Estimates are always printed with ``~``; an exact total (known up front,
e.g. a filesystem walk) is printed without it.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional

DEFAULT_INTERVAL = 30.0
AUTO_ESTIMATE_THRESHOLD = 5000
AUTO_DOCS_THRESHOLD = 500


def _fmt_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    return f"{hours // 24}d{hours % 24}h"


class Progress:
    def __init__(
        self,
        label: str,
        mode: str = "auto",
        interval: float = DEFAULT_INTERVAL,
        estimate: Optional[Callable[[], Optional[int]]] = None,
        exact_total: Optional[int] = None,
        stream=None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.label = label
        self.mode = mode
        self.interval = interval
        self.exact = exact_total is not None
        self.total: Optional[int] = exact_total
        self.stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self.done = 0
        self.active = mode == "on"
        self.emitted = False
        self._t0 = clock()
        self._next = self._t0 + interval
        self._last_t = self._t0
        self._last_done = 0
        self._lock = threading.Lock()
        if mode != "off" and estimate is not None and exact_total is None:
            threading.Thread(
                target=self._run_estimate, args=(estimate,), daemon=True
            ).start()

    def _run_estimate(self, fn) -> None:
        try:
            value = fn()
        except Exception:
            return
        if isinstance(value, int) and value > 0:
            with self._lock:
                self.total = value

    def tick(self, n: int = 1) -> None:
        """Record ``n`` processed docs; emit a line if one is due."""
        if self.mode == "off":
            return
        self.done += n
        now = self._clock()
        if now < self._next:
            return
        self._next = now + self.interval
        if not self.active:
            total = self.total
            if (total or 0) >= AUTO_ESTIMATE_THRESHOLD or (
                self.done >= AUTO_DOCS_THRESHOLD
            ):
                self.active = True
            else:
                return
        self._emit(now)

    def _emit(self, now: float) -> None:
        window = max(1e-6, now - self._last_t)
        recent = (self.done - self._last_done) / window
        overall = self.done / max(1e-6, now - self._t0)
        # Blend toward the recent rate so ETA tracks current throughput
        # without jittering on every tick.
        rate = (recent + overall) / 2 if self.done > self._last_done else overall
        self._last_t, self._last_done = now, self.done
        with self._lock:
            total = self.total
        parts = [f"{self.done:,} docs"]
        if total:
            tilde = "" if self.exact else "~"
            # An estimate can undershoot; never claim 100% mid-run.
            pct = min(99.9 if not self.exact else 100.0, 100.0 * self.done / total)
            remaining = max(0, total - self.done)
            parts.append(f"{tilde}{int(pct)}% of {tilde}{total:,}"
                         + ("" if self.exact else " est."))
            parts.append(f"{tilde}{remaining:,} remaining")
        parts.append(f"{rate:.1f} docs/s")
        if total and rate > 0 and self.done < total:
            parts.append(f"ETA ~{_fmt_eta((total - self.done) / rate)}")
        print(f"progress: {self.label}: " + ", ".join(parts),
              file=self.stream, flush=True)
        self.emitted = True
