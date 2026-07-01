from __future__ import annotations

import pytest

from arbbot.models import GapSignal, MarketFamily, MarketOpportunity, SignalType
from arbbot.risk.position_sizing import (
    kelly_fraction,
    size_arbitrage_signal,
    size_crypto_opportunity,
    size_value_edge_signal,
)


def test_kelly_fraction_positive_edge():
    # win_prob=0.6, decimal payout 2.0 (b=1) -> f = (1*0.6 - 0.4) / 1 = 0.2
    assert kelly_fraction(0.6, 2.0) == pytest.approx(0.2)


def test_kelly_fraction_no_edge_clamped_to_zero():
    # win_prob=0.4, decimal payout 2.0 -> f = (0.4 - 0.6) / 1 = -0.2 -> clamped to 0
    assert kelly_fraction(0.4, 2.0) == 0.0


def test_kelly_fraction_invalid_payout():
    assert kelly_fraction(0.6, 1.0) == 0.0


def test_size_arbitrage_signal_confidence_and_caps(matched_market_factory):
    matched_market = matched_market_factory(polymarket_quote=matched_market_factory().polymarket_quote)
    matched_market.polymarket_quote.liquidity_usd = 1000.0
    signal = GapSignal(signal_type=SignalType.ARBITRAGE, matched_market=matched_market, confidence=0.8)

    stake = size_arbitrage_signal(
        signal, bankroll_usd=1000.0, max_stake_per_trade_pct=2.0, max_stake_per_trade_usd=50.0
    )
    # base_cap = min(1000*0.02, 50) = 20; confidence_scaled = 20*0.8 = 16;
    # liquidity_cap = 1000*0.10 = 100 -> min(16, 100) = 16
    assert stake == pytest.approx(16.0)


def test_size_arbitrage_signal_liquidity_constrained(matched_market_factory):
    matched_market = matched_market_factory()
    matched_market.polymarket_quote.liquidity_usd = 50.0  # liquidity_cap = 5.0
    signal = GapSignal(signal_type=SignalType.ARBITRAGE, matched_market=matched_market, confidence=1.0)

    stake = size_arbitrage_signal(
        signal, bankroll_usd=1000.0, max_stake_per_trade_pct=2.0, max_stake_per_trade_usd=50.0
    )
    assert stake == pytest.approx(5.0)


def test_size_value_edge_signal(matched_market_factory):
    matched_market = matched_market_factory()
    signal = GapSignal(
        signal_type=SignalType.VALUE_EDGE,
        matched_market=matched_market,
        confidence=0.5,
        metadata={"consensus_prob": 0.6, "poly_ask": 0.5},
    )

    stake = size_value_edge_signal(
        signal,
        bankroll_usd=1000.0,
        kelly_fraction_cap=0.15,
        max_stake_per_trade_pct=2.0,
        max_stake_per_trade_usd=50.0,
    )
    # full_kelly = 0.2; fractional = 0.2*0.15*0.5 = 0.015; pct_cap = 0.02
    # stake_fraction = min(0.015, 0.02) = 0.015 -> stake = 15.0
    assert stake == pytest.approx(15.0)


def test_size_value_edge_signal_missing_metadata_returns_zero(matched_market_factory):
    matched_market = matched_market_factory()
    signal = GapSignal(signal_type=SignalType.VALUE_EDGE, matched_market=matched_market, confidence=0.5)
    stake = size_value_edge_signal(
        signal, bankroll_usd=1000.0, kelly_fraction_cap=0.15, max_stake_per_trade_pct=2.0, max_stake_per_trade_usd=50.0
    )
    assert stake == 0.0


def test_size_crypto_opportunity_confidence_and_caps():
    opportunity = MarketOpportunity(family=MarketFamily.CRYPTO, symbol="BTC/USD", confidence=0.8)
    stake = size_crypto_opportunity(
        opportunity, bankroll_usd=1000.0, max_stake_per_trade_pct=2.0, max_stake_per_trade_usd=50.0
    )
    # base_cap = min(1000*0.02, 50) = 20; confidence_scaled = 20*0.8 = 16
    assert stake == pytest.approx(16.0)


def test_size_crypto_opportunity_never_zeroes_out_low_confidence():
    opportunity = MarketOpportunity(family=MarketFamily.CRYPTO, symbol="BTC/USD", confidence=0.0)
    stake = size_crypto_opportunity(
        opportunity, bankroll_usd=1000.0, max_stake_per_trade_pct=2.0, max_stake_per_trade_usd=50.0
    )
    # floor of 0.25x applies even at zero confidence, same convention as sports arbitrage sizing
    assert stake == pytest.approx(20.0 * 0.25)
