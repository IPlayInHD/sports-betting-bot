from __future__ import annotations

import pytest

from arbbot.markets.crypto.detector import detect_crypto_arbitrage
from arbbot.models import CryptoQuote, MarketFamily


def _quote(exchange: str, symbol: str, bid: float, ask: float) -> CryptoQuote:
    return CryptoQuote(exchange=exchange, symbol=symbol, bid=bid, ask=ask)


def test_detects_cross_exchange_gap():
    quotes = [
        _quote("coinbase", "BTC/USD", bid=64950, ask=65000),
        _quote("kraken", "BTC/USD", bid=65300, ask=65350),  # sell here (higher bid)
    ]
    opportunities = detect_crypto_arbitrage(quotes, min_edge_pct=0.1, max_edge_pct=5.0, fee_pct_per_leg=0.05)

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.family == MarketFamily.CRYPTO
    assert opp.symbol == "BTC/USD"
    # gross edge = (65300 - 65000) / 65000 * 100 = 0.4615%; minus 2*0.05% fees
    assert opp.edge_pct == pytest.approx(0.4615 - 0.10, abs=0.01)
    legs_by_action = {leg["action"]: leg for leg in opp.legs}
    assert legs_by_action["buy"]["venue"] == "coinbase"
    assert legs_by_action["sell"]["venue"] == "kraken"


def test_no_opportunity_when_prices_agree():
    quotes = [
        _quote("coinbase", "BTC/USD", bid=64990, ask=65000),
        _quote("kraken", "BTC/USD", bid=64995, ask=65005),
    ]
    opportunities = detect_crypto_arbitrage(quotes, min_edge_pct=0.1, max_edge_pct=5.0, fee_pct_per_leg=0.05)
    assert opportunities == []


def test_fees_can_eat_a_small_gap():
    quotes = [
        _quote("coinbase", "BTC/USD", bid=64980, ask=65000),
        _quote("kraken", "BTC/USD", bid=65020, ask=65040),
    ]
    # gross edge here is tiny; a high per-leg fee should wipe it out entirely.
    opportunities = detect_crypto_arbitrage(quotes, min_edge_pct=0.1, max_edge_pct=5.0, fee_pct_per_leg=1.0)
    assert opportunities == []


def test_requires_at_least_two_exchanges():
    quotes = [_quote("coinbase", "BTC/USD", bid=64990, ask=65000)]
    opportunities = detect_crypto_arbitrage(quotes)
    assert opportunities == []


def test_skips_when_cheapest_and_priciest_are_same_exchange():
    # Only one exchange quotes this symbol -- min(ask) and max(bid) both
    # resolve to it, so there's no real cross-venue trade available.
    quotes = [_quote("coinbase", "ETH/USD", bid=3390, ask=3400)]
    opportunities = detect_crypto_arbitrage(quotes)
    assert opportunities == []


def test_confidence_scales_with_safety_margin():
    # A gap that only just clears the minimum edge scores lower confidence
    # than one with a comfortable margin above it.
    thin = [
        _quote("coinbase", "BTC/USD", bid=64950, ask=65000),
        _quote("kraken", "BTC/USD", bid=65100, ask=65150),
    ]
    thick = [
        _quote("coinbase", "BTC/USD", bid=64950, ask=65000),
        _quote("kraken", "BTC/USD", bid=65600, ask=65650),
    ]
    thin_opp = detect_crypto_arbitrage(thin, min_edge_pct=0.1, fee_pct_per_leg=0.02)[0]
    thick_opp = detect_crypto_arbitrage(thick, min_edge_pct=0.1, fee_pct_per_leg=0.02)[0]
    assert 0.0 <= thin_opp.confidence <= 1.0
    assert thick_opp.confidence > thin_opp.confidence
    assert thick_opp.confidence == pytest.approx(1.0)  # wide margin saturates


def test_gross_edge_above_max_is_treated_as_bad_data():
    quotes = [
        _quote("coinbase", "BTC/USD", bid=64990, ask=1000),  # implausible ask -- bad data, not a real gap
        _quote("kraken", "BTC/USD", bid=65350, ask=65400),
    ]
    opportunities = detect_crypto_arbitrage(quotes, min_edge_pct=0.1, max_edge_pct=5.0, fee_pct_per_leg=0.05)
    assert opportunities == []
