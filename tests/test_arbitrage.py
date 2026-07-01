from __future__ import annotations

import time

import pytest

from arbbot.models import MatchedMarket, PolymarketQuote, SportsbookQuote
from arbbot.strategy.arbitrage import detect_arbitrage


def _build_two_way_event():
    commence_time = time.time() + 3600

    home_book = SportsbookQuote(
        bookmaker="book_a",
        event_id="evt-1",
        sport_key="basketball_nba",
        home_team="Home Team",
        away_team="Away Team",
        commence_time=commence_time,
        outcome_name="Home Team",
        decimal_odds=2.30,  # implied prob 0.4348 -- cheaper than Polymarket's 0.46
    )
    away_book = SportsbookQuote(
        bookmaker="book_a",
        event_id="evt-1",
        sport_key="basketball_nba",
        home_team="Home Team",
        away_team="Away Team",
        commence_time=commence_time,
        outcome_name="Away Team",
        decimal_odds=1.80,  # implied prob 0.5556 -- more expensive than Polymarket's 0.50
    )

    home_poly = PolymarketQuote(
        market_id="evt-1",
        token_id="evt-1:home",
        question="Will Home Team beat Away Team?",
        outcome_name="Home Team",
        best_bid=0.44,
        best_ask=0.46,
        liquidity_usd=2000.0,
        end_date=commence_time,
    )
    away_poly = PolymarketQuote(
        market_id="evt-1",
        token_id="evt-1:away",
        question="Will Home Team beat Away Team?",
        outcome_name="Away Team",
        best_bid=0.48,
        best_ask=0.50,
        liquidity_usd=2000.0,
        end_date=commence_time,
    )

    home_match = MatchedMarket(
        match_id="evt-1:Home Team", sportsbook_quotes=[home_book], polymarket_quote=home_poly, match_score=1.0
    )
    away_match = MatchedMarket(
        match_id="evt-1:Away Team", sportsbook_quotes=[away_book], polymarket_quote=away_poly, match_score=1.0
    )
    return [home_match, away_match]


def test_detect_arbitrage_finds_cross_venue_gap():
    matches = _build_two_way_event()
    signals = detect_arbitrage(matches, min_edge_pct=0.5, max_edge_pct=8.0, fee_buffer_pct=1.0)

    assert len(signals) == 1
    signal = signals[0]

    # total_implied_prob = 0.4348 (sportsbook home) + 0.50 (polymarket away) = 0.9348
    assert signal.metadata["total_implied_prob"] == pytest.approx(0.9348, abs=1e-3)
    # raw edge ~6.52%, minus 1% fee buffer ~5.52%
    assert signal.edge_pct == pytest.approx(5.52, abs=0.05)

    legs = {leg["venue"]: leg for leg in signal.metadata["legs"]}
    assert legs["sportsbook"]["outcome_name"] == "Home Team"
    assert legs["polymarket"]["outcome_name"] == "Away Team"

    assert signal.sportsbook_stake_fraction + signal.polymarket_stake_fraction == pytest.approx(1.0)
    assert signal.polymarket_stake_fraction == pytest.approx(0.50 / 0.9348, abs=1e-4)


def test_detect_arbitrage_rejects_below_min_edge():
    matches = _build_two_way_event()
    # Require a much bigger edge than actually exists.
    signals = detect_arbitrage(matches, min_edge_pct=20.0, max_edge_pct=50.0, fee_buffer_pct=1.0)
    assert signals == []


def test_detect_arbitrage_ignores_single_outcome_events():
    matches = _build_two_way_event()[:1]  # only one outcome -- can't arb across a single leg
    signals = detect_arbitrage(matches)
    assert signals == []


def test_detect_arbitrage_skips_single_venue_opportunities():
    # Make Polymarket cheaper on both legs -- no sportsbook leg involved, so
    # this should NOT be reported as a cross-market signal.
    matches = _build_two_way_event()
    matches[0].polymarket_quote.best_ask = 0.10
    matches[1].polymarket_quote.best_ask = 0.10
    signals = detect_arbitrage(matches, min_edge_pct=0.5, max_edge_pct=90.0, fee_buffer_pct=1.0)
    assert signals == []
