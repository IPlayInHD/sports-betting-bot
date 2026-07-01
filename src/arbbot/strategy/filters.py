"""Layer 6: multi-stage signal filter pipeline.

Every detected signal (arbitrage or value-edge) passes through this ordered
pipeline before it's allowed to reach position sizing / execution. Filters
are ordered cheap-to-expensive so obviously-bad signals are rejected fast --
this matters for reaction speed when many signals arrive per polling cycle.
This is one of the main levers for "enhancing win rate": most of the raw
detected gaps are noise (stale quotes, thin books, wide spreads) and get
discarded here before any capital is risked on them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from arbbot.models import GapSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FilterConfig:
    max_quote_age_sec: float = 3.0
    min_polymarket_liquidity_usd: float = 500.0
    max_spread_pct: float = 3.0
    max_signal_latency_ms: float = 800.0
    max_concurrent_positions_per_market_group: int = 1


class FilterResult:
    __slots__ = ("passed", "reason")

    def __init__(self, passed: bool, reason: str = ""):
        self.passed = passed
        self.reason = reason


def _filter_staleness(signal: GapSignal, cfg: FilterConfig) -> FilterResult:
    poly_quote = signal.matched_market.polymarket_quote
    book_quote = signal.matched_market.best_sportsbook_quote
    now = time.time()
    poly_age = now - poly_quote.observed_at
    book_age = now - book_quote.observed_at
    if poly_age > cfg.max_quote_age_sec or book_age > cfg.max_quote_age_sec:
        return FilterResult(False, f"stale quote (poly_age={poly_age:.2f}s book_age={book_age:.2f}s)")
    return FilterResult(True)


def _filter_liquidity(signal: GapSignal, cfg: FilterConfig) -> FilterResult:
    liquidity = signal.matched_market.polymarket_quote.liquidity_usd
    if liquidity < cfg.min_polymarket_liquidity_usd:
        return FilterResult(False, f"insufficient liquidity (${liquidity:.0f})")
    return FilterResult(True)


def _filter_spread(signal: GapSignal, cfg: FilterConfig) -> FilterResult:
    spread_pct = signal.matched_market.polymarket_quote.spread_pct
    if spread_pct > cfg.max_spread_pct:
        return FilterResult(False, f"spread too wide ({spread_pct:.2f}%)")
    return FilterResult(True)


def _filter_pipeline_latency(signal: GapSignal, cfg: FilterConfig) -> FilterResult:
    latency_ms = signal.age_sec * 1000.0
    if latency_ms > cfg.max_signal_latency_ms:
        return FilterResult(False, f"signal aged out ({latency_ms:.0f}ms since detection)")
    return FilterResult(True)


_PIPELINE = [
    _filter_staleness,
    _filter_liquidity,
    _filter_spread,
    _filter_pipeline_latency,
]


class ExposureTracker:
    """Layer 6 correlation guard: caps how many concurrent open positions can
    exist on the same underlying market group (e.g. the same game, across its
    moneyline/spread/total variants) to avoid stacking correlated risk.
    """

    def __init__(self) -> None:
        self._open_by_group: dict[str, int] = {}

    def market_group_key(self, signal: GapSignal) -> str:
        return signal.metadata.get("event_id", signal.matched_market.match_id)

    def check(self, signal: GapSignal, cfg: FilterConfig) -> FilterResult:
        key = self.market_group_key(signal)
        current = self._open_by_group.get(key, 0)
        if current >= cfg.max_concurrent_positions_per_market_group:
            return FilterResult(False, f"exposure cap reached for market group {key} ({current} open)")
        return FilterResult(True)

    def on_open(self, signal: GapSignal) -> None:
        key = self.market_group_key(signal)
        self._open_by_group[key] = self._open_by_group.get(key, 0) + 1

    def on_close(self, signal: GapSignal) -> None:
        key = self.market_group_key(signal)
        if key in self._open_by_group:
            self._open_by_group[key] = max(0, self._open_by_group[key] - 1)


def run_filter_pipeline(
    signals: list[GapSignal], cfg: FilterConfig, exposure_tracker: ExposureTracker
) -> list[GapSignal]:
    accepted = []
    for signal in signals:
        rejected_reason = None
        for stage in _PIPELINE:
            result = stage(signal, cfg)
            if not result.passed:
                rejected_reason = result.reason
                break
        if rejected_reason is None:
            exposure_result = exposure_tracker.check(signal, cfg)
            if not exposure_result.passed:
                rejected_reason = exposure_result.reason

        if rejected_reason:
            logger.debug("signal %s rejected: %s", signal.signal_id, rejected_reason)
            continue
        accepted.append(signal)
    return accepted
