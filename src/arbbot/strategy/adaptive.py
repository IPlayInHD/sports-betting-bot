"""Frequency layer: adaptive burst polling and per-market cooldown.

Two small state machines used by every market family's polling loop:

AdaptivePoller -- professional market-making/arb systems don't poll at a
fixed cadence: they concentrate attention where activity is. Whenever a
cycle detects at least one opportunity, the poller drops to its burst
interval (gaps cluster in time -- one detected gap is strong evidence the
venue is temporarily mispriced or lagging, so re-check fast while it
lasts); quiet cycles decay the interval geometrically back up to the base
rate so idle markets don't hammer the public APIs.

SignalThrottle -- the flip side of polling faster: a persistent gap would
otherwise re-fire on every cycle, stacking many near-duplicate trades on
one underlying mispricing (which is one correlated position pretending to
be several independent ones). Each executed opportunity key goes on a
cooldown; repeat detections during the window are surfaced as skipped
rather than traded.
"""

from __future__ import annotations

import time


class AdaptivePoller:
    def __init__(self, base_interval_sec: float, burst_interval_sec: float, decay: float = 1.6) -> None:
        if burst_interval_sec > base_interval_sec:
            burst_interval_sec = base_interval_sec
        self._base = base_interval_sec
        self._burst = burst_interval_sec
        self._decay = max(1.01, decay)
        self._current = base_interval_sec

    @property
    def current_interval_sec(self) -> float:
        return self._current

    def on_cycle(self, opportunities_found: int) -> float:
        """Record the outcome of a polling cycle and return the interval to
        sleep before the next one.
        """
        if opportunities_found > 0:
            self._current = self._burst
        else:
            self._current = min(self._base, self._current * self._decay)
        return self._current


class SignalThrottle:
    def __init__(self, cooldown_sec: float) -> None:
        self._cooldown = cooldown_sec
        self._last_fired: dict[str, float] = {}

    def ready(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        last = self._last_fired.get(key)
        return last is None or (now - last) >= self._cooldown

    def fire(self, key: str, now: float | None = None) -> None:
        self._last_fired[key] = time.time() if now is None else now

    def prune(self, now: float | None = None) -> None:
        """Drop expired entries so long-running processes don't accumulate
        one dict entry per market ever seen.
        """
        now = time.time() if now is None else now
        self._last_fired = {k: t for k, t in self._last_fired.items() if (now - t) < self._cooldown}
