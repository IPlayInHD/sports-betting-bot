from __future__ import annotations

import time

import pytest

from arbbot.models import MatchedMarket, PolymarketQuote, SportsbookQuote


def make_sportsbook_quote(**overrides) -> SportsbookQuote:
    defaults = dict(
        bookmaker="pinnacle",
        event_id="evt-1",
        sport_key="basketball_nba",
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
        commence_time=time.time() + 3600,
        outcome_name="Los Angeles Lakers",
        decimal_odds=1.91,
    )
    defaults.update(overrides)
    return SportsbookQuote(**defaults)


def make_polymarket_quote(**overrides) -> PolymarketQuote:
    defaults = dict(
        market_id="evt-1",
        token_id="evt-1:lakers",
        question="Will the Los Angeles Lakers beat the Boston Celtics?",
        outcome_name="Los Angeles Lakers",
        best_bid=0.51,
        best_ask=0.52,
        liquidity_usd=2000.0,
        end_date=time.time() + 3600,
    )
    defaults.update(overrides)
    return PolymarketQuote(**defaults)


def make_matched_market(**overrides) -> MatchedMarket:
    sportsbook_quotes = overrides.pop("sportsbook_quotes", None) or [make_sportsbook_quote()]
    polymarket_quote = overrides.pop("polymarket_quote", None) or make_polymarket_quote()
    defaults = dict(
        match_id="evt-1:Los Angeles Lakers",
        sportsbook_quotes=sportsbook_quotes,
        polymarket_quote=polymarket_quote,
        match_score=0.95,
    )
    defaults.update(overrides)
    return MatchedMarket(**defaults)


@pytest.fixture
def sportsbook_quote_factory():
    return make_sportsbook_quote


@pytest.fixture
def polymarket_quote_factory():
    return make_polymarket_quote


@pytest.fixture
def matched_market_factory():
    return make_matched_market
