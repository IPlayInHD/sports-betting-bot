from __future__ import annotations

import time

from arbbot.models import GapSignal, SignalType
from arbbot.strategy.filters import ExposureTracker, FilterConfig, run_filter_pipeline


def _make_signal(matched_market_factory, **overrides):
    matched_market = overrides.pop("matched_market", None) or matched_market_factory()
    defaults = dict(
        signal_type=SignalType.ARBITRAGE,
        matched_market=matched_market,
        edge_pct=2.0,
        confidence=0.8,
        metadata={"event_id": matched_market.sportsbook_quotes[0].event_id},
    )
    defaults.update(overrides)
    return GapSignal(**defaults)


def test_pipeline_accepts_healthy_signal(matched_market_factory):
    signal = _make_signal(matched_market_factory)
    cfg = FilterConfig()
    accepted = run_filter_pipeline([signal], cfg, ExposureTracker())
    assert accepted == [signal]


def test_pipeline_rejects_stale_quote(matched_market_factory, polymarket_quote_factory):
    stale_poly = polymarket_quote_factory(observed_at=time.time() - 10)
    matched_market = matched_market_factory(polymarket_quote=stale_poly)
    signal = _make_signal(matched_market_factory, matched_market=matched_market)
    cfg = FilterConfig(max_quote_age_sec=3.0)
    accepted = run_filter_pipeline([signal], cfg, ExposureTracker())
    assert accepted == []


def test_pipeline_rejects_thin_liquidity(matched_market_factory, polymarket_quote_factory):
    thin_poly = polymarket_quote_factory(liquidity_usd=10.0)
    matched_market = matched_market_factory(polymarket_quote=thin_poly)
    signal = _make_signal(matched_market_factory, matched_market=matched_market)
    cfg = FilterConfig(min_polymarket_liquidity_usd=500.0)
    accepted = run_filter_pipeline([signal], cfg, ExposureTracker())
    assert accepted == []


def test_pipeline_rejects_wide_spread(matched_market_factory, polymarket_quote_factory):
    wide_poly = polymarket_quote_factory(best_bid=0.30, best_ask=0.70)
    matched_market = matched_market_factory(polymarket_quote=wide_poly)
    signal = _make_signal(matched_market_factory, matched_market=matched_market)
    cfg = FilterConfig(max_spread_pct=3.0)
    accepted = run_filter_pipeline([signal], cfg, ExposureTracker())
    assert accepted == []


def test_pipeline_rejects_aged_signal(matched_market_factory):
    signal = _make_signal(matched_market_factory, created_at=time.time() - 5)
    cfg = FilterConfig(max_signal_latency_ms=800.0)
    accepted = run_filter_pipeline([signal], cfg, ExposureTracker())
    assert accepted == []


def test_exposure_tracker_caps_concurrent_positions(matched_market_factory):
    signal = _make_signal(matched_market_factory)
    cfg = FilterConfig(max_concurrent_positions_per_market_group=1)
    tracker = ExposureTracker()

    assert run_filter_pipeline([signal], cfg, tracker) == [signal]
    tracker.on_open(signal)

    signal2 = _make_signal(matched_market_factory)
    assert run_filter_pipeline([signal2], cfg, tracker) == []

    tracker.on_close(signal)
    assert run_filter_pipeline([signal2], cfg, tracker) == [signal2]
