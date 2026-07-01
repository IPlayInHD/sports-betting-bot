from __future__ import annotations

import time

import pytest

from arbbot.models import MatchedMarket, PolymarketQuote, SportsbookQuote
from arbbot.strategy.value_edge import detect_value_edges


def _build_consensus_event():
    commence_time = time.time() + 3600
    # 3 identical bookmakers, true fair prob home=0.6 / away=0.4, uniform 5% vig
    # so multiplicative de-vig recovers exactly 0.6 / 0.4 for every book.
    home_decimal = 1.0 / 0.63
    away_decimal = 1.0 / 0.42

    home_books, away_books = [], []
    for i in range(3):
        common = dict(
            bookmaker=f"book_{i}",
            event_id="evt-1",
            sport_key="basketball_nba",
            home_team="Home Team",
            away_team="Away Team",
            commence_time=commence_time,
        )
        home_books.append(SportsbookQuote(outcome_name="Home Team", decimal_odds=home_decimal, **common))
        away_books.append(SportsbookQuote(outcome_name="Away Team", decimal_odds=away_decimal, **common))

    home_poly = PolymarketQuote(
        market_id="evt-1",
        token_id="evt-1:home",
        question="Will Home Team beat Away Team?",
        outcome_name="Home Team",
        best_bid=0.48,
        best_ask=0.50,  # 10 points cheaper than the 0.6 consensus -> should trigger
        liquidity_usd=2000.0,
        end_date=commence_time,
    )
    away_poly = PolymarketQuote(
        market_id="evt-1",
        token_id="evt-1:away",
        question="Will Home Team beat Away Team?",
        outcome_name="Away Team",
        best_bid=0.39,
        best_ask=0.41,  # priced above its 0.4 fair value -> should NOT trigger
        liquidity_usd=2000.0,
        end_date=commence_time,
    )

    home_match = MatchedMarket(
        match_id="evt-1:Home Team", sportsbook_quotes=home_books, polymarket_quote=home_poly, match_score=1.0
    )
    away_match = MatchedMarket(
        match_id="evt-1:Away Team", sportsbook_quotes=away_books, polymarket_quote=away_poly, match_score=1.0
    )
    return [home_match, away_match]


def test_detect_value_edges_finds_underpriced_outcome():
    matches = _build_consensus_event()
    signals = detect_value_edges(matches, min_books_agreeing=3, min_edge_pct=3.0)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.matched_market.polymarket_quote.outcome_name == "Home Team"
    assert signal.edge_pct == pytest.approx(10.0, abs=0.05)
    assert signal.metadata["consensus_prob"] == pytest.approx(0.6, abs=1e-6)
    assert signal.confidence == pytest.approx(0.5, abs=1e-6)


def test_detect_value_edges_requires_min_books():
    matches = _build_consensus_event()
    signals = detect_value_edges(matches, min_books_agreeing=5, min_edge_pct=3.0)
    assert signals == []


def test_detect_value_edges_disagreement_blocks_signal():
    matches = _build_consensus_event()
    # Make one book wildly disagree -- consensus stdev should exceed the cap.
    matches[0].sportsbook_quotes[0].decimal_odds = 1.0 / 0.20
    signals = detect_value_edges(matches, min_books_agreeing=3, min_edge_pct=3.0, max_consensus_stdev_pct=4.0)
    assert signals == []
