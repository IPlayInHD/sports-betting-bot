from __future__ import annotations

import time

from arbbot.matching.market_matcher import match_markets
from arbbot.models import PolymarketQuote, SportsbookQuote


def _sportsbook_quote(commence_time: float) -> SportsbookQuote:
    return SportsbookQuote(
        bookmaker="pinnacle",
        event_id="evt-1",
        sport_key="basketball_nba",
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
        commence_time=commence_time,
        outcome_name="Los Angeles Lakers",
        decimal_odds=1.91,
    )


def _polymarket_quote(end_date: float, outcome_name: str = "Los Angeles Lakers") -> PolymarketQuote:
    return PolymarketQuote(
        market_id="poly-evt-1",
        token_id="poly-evt-1:lakers",
        question="Will the Los Angeles Lakers beat the Boston Celtics?",
        outcome_name=outcome_name,
        best_bid=0.50,
        best_ask=0.52,
        liquidity_usd=2000.0,
        end_date=end_date,
    )


def test_match_markets_finds_confident_match():
    commence_time = time.time() + 3600
    matches = match_markets(
        [_sportsbook_quote(commence_time)],
        [_polymarket_quote(commence_time)],
        min_match_score=0.82,
        max_date_skew_hours=6.0,
    )
    assert len(matches) == 1
    assert matches[0].polymarket_quote.outcome_name == "Los Angeles Lakers"
    assert matches[0].match_score >= 0.82


def test_match_markets_rejects_date_skew():
    commence_time = time.time() + 3600
    far_end_date = commence_time + 48 * 3600  # 48h away, well beyond the 6h skew window
    matches = match_markets(
        [_sportsbook_quote(commence_time)],
        [_polymarket_quote(far_end_date)],
        min_match_score=0.82,
        max_date_skew_hours=6.0,
    )
    assert matches == []


def test_match_markets_rejects_low_similarity():
    commence_time = time.time() + 3600
    unrelated_poly = PolymarketQuote(
        market_id="poly-evt-2",
        token_id="poly-evt-2:x",
        question="Will it rain in Tokyo tomorrow?",
        outcome_name="Yes",
        best_bid=0.50,
        best_ask=0.52,
        liquidity_usd=2000.0,
        end_date=commence_time,
    )
    matches = match_markets(
        [_sportsbook_quote(commence_time)],
        [unrelated_poly],
        min_match_score=0.82,
        max_date_skew_hours=6.0,
    )
    assert matches == []
