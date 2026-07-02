from __future__ import annotations

import time

import pytest

from arbbot.markets.polycrypto.detector import (
    SpotAnchorConfig,
    detect_complement_arbitrage,
    detect_spot_anchored_gaps,
)
from arbbot.models import MarketFamily, PolymarketQuote


def make_pair(
    market_id: str = "mkt-1",
    question: str = "Will Bitcoin be above $70,000 on July 31?",
    yes_ask: float = 0.50,
    no_ask: float = 0.50,
    spread: float = 0.01,
    liquidity: float = 2000.0,
    end_in_days: float = 10.0,
) -> list[PolymarketQuote]:
    end = time.time() + end_in_days * 86400.0
    return [
        PolymarketQuote(
            market_id=market_id,
            token_id=f"{market_id}:yes",
            question=question,
            outcome_name="Yes",
            best_bid=max(yes_ask - spread, 0.001),
            best_ask=yes_ask,
            liquidity_usd=liquidity,
            end_date=end,
        ),
        PolymarketQuote(
            market_id=market_id,
            token_id=f"{market_id}:no",
            question=question,
            outcome_name="No",
            best_bid=max(no_ask - spread, 0.001),
            best_ask=no_ask,
            liquidity_usd=liquidity,
            end_date=end,
        ),
    ]


class TestComplementArbitrage:
    def test_detects_when_combined_ask_below_one(self):
        quotes = make_pair(yes_ask=0.48, no_ask=0.49)  # 0.97 combined -> ~3.1% gross
        opps = detect_complement_arbitrage(quotes, min_edge_pct=0.4, fee_buffer_pct=0.2)
        assert len(opps) == 1
        opp = opps[0]
        assert opp.family == MarketFamily.POLYCRYPTO
        assert opp.metadata["strategy"] == "complement"
        assert opp.edge_pct == pytest.approx((1 - 0.97) / 0.97 * 100 - 0.2)
        assert len(opp.legs) == 2
        assert {leg["symbol"] for leg in opp.legs} == {"Yes", "No"}

    def test_no_opportunity_when_combined_ask_at_or_above_one(self):
        assert detect_complement_arbitrage(make_pair(yes_ask=0.52, no_ask=0.50)) == []

    def test_fee_buffer_can_kill_a_marginal_gap(self):
        quotes = make_pair(yes_ask=0.497, no_ask=0.50)  # ~0.3% gross
        assert detect_complement_arbitrage(quotes, min_edge_pct=0.4, fee_buffer_pct=0.2) == []

    def test_liquidity_filter(self):
        quotes = make_pair(yes_ask=0.48, no_ask=0.49, liquidity=50.0)
        assert detect_complement_arbitrage(quotes, min_liquidity_usd=250.0) == []

    def test_absolute_spread_filter(self):
        quotes = make_pair(yes_ask=0.48, no_ask=0.49, spread=0.06)  # 6 cents wide
        assert detect_complement_arbitrage(quotes, max_spread_cents=3.0) == []

    def test_implausibly_large_gap_treated_as_bad_data(self):
        quotes = make_pair(yes_ask=0.40, no_ask=0.40)  # 25% gross "gap"
        assert detect_complement_arbitrage(quotes, max_edge_pct=5.0) == []

    def test_confidence_capped_at_one_even_on_crossed_books(self):
        # A crossed book (ask below bid) must not push confidence above 1.
        quotes = make_pair(yes_ask=0.48, no_ask=0.49)
        quotes[0].best_bid = 0.50  # cross the YES book
        opps = detect_complement_arbitrage(quotes)
        assert len(opps) == 1
        assert 0.0 <= opps[0].confidence <= 1.0

    def test_ignores_non_binary_markets(self):
        quotes = make_pair(yes_ask=0.30, no_ask=0.30)
        extra = PolymarketQuote(
            market_id="mkt-1",
            token_id="mkt-1:maybe",
            question=quotes[0].question,
            outcome_name="Maybe",
            best_bid=0.29,
            best_ask=0.30,
            liquidity_usd=2000.0,
            end_date=quotes[0].end_date,
        )
        assert detect_complement_arbitrage(quotes + [extra]) == []


class TestSpotAnchoredGaps:
    CFG = SpotAnchorConfig(min_edge_pct=6.0, max_edge_pct=90.0, min_confidence=0.0, max_days_to_expiry=45.0)

    def test_detects_underpriced_yes_when_spot_far_above_threshold(self):
        # Spot $90k vs a $70k strike 5 days out: model says YES is ~certain,
        # but the market only charges 60c for it.
        quotes = make_pair(yes_ask=0.60, no_ask=0.41, end_in_days=5)
        opps = detect_spot_anchored_gaps(quotes, {"BTC/USD": 90_000.0}, self.CFG)
        assert len(opps) == 1
        opp = opps[0]
        assert opp.metadata["strategy"] == "spot_anchor"
        assert opp.metadata["side"] == "yes"
        assert opp.symbol == "BTC/USD"
        assert opp.edge_pct > 30
        assert len(opp.legs) == 1
        assert opp.legs[0]["token_id"].endswith(":yes")

    def test_detects_underpriced_no_when_spot_far_below_threshold(self):
        quotes = make_pair(yes_ask=0.40, no_ask=0.61, end_in_days=5)
        opps = detect_spot_anchored_gaps(quotes, {"BTC/USD": 50_000.0}, self.CFG)
        assert len(opps) == 1
        assert opps[0].metadata["side"] == "no"

    def test_fairly_priced_market_produces_nothing(self):
        # Near-the-money strike priced around 50/50: no material model gap.
        quotes = make_pair(yes_ask=0.50, no_ask=0.52, end_in_days=5)
        assert detect_spot_anchored_gaps(quotes, {"BTC/USD": 70_000.0}, self.CFG) == []

    def test_min_confidence_gate(self):
        strict = SpotAnchorConfig(min_edge_pct=6.0, max_edge_pct=90.0, min_confidence=0.99, max_days_to_expiry=45.0)
        quotes = make_pair(yes_ask=0.60, no_ask=0.41, end_in_days=5)
        assert detect_spot_anchored_gaps(quotes, {"BTC/USD": 90_000.0}, strict) == []

    def test_skips_markets_without_spot_data_or_parse(self):
        quotes = make_pair(yes_ask=0.60, no_ask=0.41, end_in_days=5)
        assert detect_spot_anchored_gaps(quotes, {"ETH/USD": 3_400.0}, self.CFG) == []  # no BTC spot
        unparseable = make_pair(question="Will a spot ETF be approved?", yes_ask=0.60, no_ask=0.41)
        assert detect_spot_anchored_gaps(unparseable, {"BTC/USD": 90_000.0}, self.CFG) == []

    def test_skips_expired_and_too_distant_markets(self):
        expired = make_pair(yes_ask=0.60, no_ask=0.41, end_in_days=-1)
        assert detect_spot_anchored_gaps(expired, {"BTC/USD": 90_000.0}, self.CFG) == []
        distant = make_pair(yes_ask=0.60, no_ask=0.41, end_in_days=200)
        assert detect_spot_anchored_gaps(distant, {"BTC/USD": 90_000.0}, self.CFG) == []

    def test_absurd_edge_treated_as_broken_model_or_quote(self):
        tight = SpotAnchorConfig(min_edge_pct=6.0, max_edge_pct=30.0, min_confidence=0.0, max_days_to_expiry=45.0)
        quotes = make_pair(yes_ask=0.05, no_ask=0.96, end_in_days=5)  # 90+ point "gap"
        assert detect_spot_anchored_gaps(quotes, {"BTC/USD": 90_000.0}, tight) == []
