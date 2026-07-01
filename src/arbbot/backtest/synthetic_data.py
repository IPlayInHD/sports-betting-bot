"""Generates a synthetic historical dataset of paired sportsbook/Polymarket
quotes with a known ground-truth outcome per event, for backtesting the
strategy layers without needing a paid historical-odds data vendor.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from arbbot.models import MatchedMarket, PolymarketQuote, SportsbookQuote


@dataclass(slots=True)
class SyntheticEvent:
    event_id: str
    home_team: str
    away_team: str
    home_wins: bool
    matched_markets: list[MatchedMarket] = field(default_factory=list)


def generate_synthetic_events(
    n_events: int = 500, seed: int = 42, arb_probability: float = 0.15
) -> list[SyntheticEvent]:
    rng = random.Random(seed)
    events: list[SyntheticEvent] = []

    for i in range(n_events):
        home, away = f"Team_{i}_A", f"Team_{i}_B"
        event_id = f"synthetic:{i}"
        commence_time = time.time() + rng.uniform(3600, 48 * 3600)
        true_home_prob = rng.uniform(0.25, 0.75)
        home_wins = rng.random() < true_home_prob

        home_book_quotes, away_book_quotes = [], []
        for b in range(rng.randint(3, 6)):
            vig = rng.uniform(0.02, 0.06)
            jitter = rng.uniform(-0.015, 0.015)
            home_prob = min(max(true_home_prob + jitter, 0.03), 0.97)
            away_prob = 1.0 - home_prob
            scale = 1.0 + vig
            common = dict(
                bookmaker=f"book_{b}",
                event_id=event_id,
                sport_key="synthetic",
                home_team=home,
                away_team=away,
                commence_time=commence_time,
            )
            home_book_quotes.append(
                SportsbookQuote(outcome_name=home, decimal_odds=round(1.0 / min(home_prob * scale, 0.98), 3), **common)
            )
            away_book_quotes.append(
                SportsbookQuote(outcome_name=away, decimal_odds=round(1.0 / min(away_prob * scale, 0.98), 3), **common)
            )

        # Deliberately inject a mispricing gap on a configurable fraction of events.
        gap = rng.uniform(0.01, 0.035) if rng.random() < arb_probability else 0.0
        poly_home_mid = min(max(true_home_prob - gap, 0.02), 0.98)
        poly_away_mid = 1.0 - poly_home_mid
        half_spread = rng.uniform(0.004, 0.015)
        liquidity = round(rng.uniform(300, 6000), 2)

        home_poly = PolymarketQuote(
            market_id=event_id,
            token_id=f"{event_id}:{home}",
            question=f"Will {home} beat {away}?",
            outcome_name=home,
            best_bid=round(max(poly_home_mid - half_spread, 0.01), 4),
            best_ask=round(min(poly_home_mid + half_spread, 0.99), 4),
            liquidity_usd=liquidity,
            end_date=commence_time,
        )
        away_poly = PolymarketQuote(
            market_id=event_id,
            token_id=f"{event_id}:{away}",
            question=f"Will {home} beat {away}?",
            outcome_name=away,
            best_bid=round(max(poly_away_mid - half_spread, 0.01), 4),
            best_ask=round(min(poly_away_mid + half_spread, 0.99), 4),
            liquidity_usd=liquidity,
            end_date=commence_time,
        )

        matched_markets = [
            MatchedMarket(
                match_id=f"{event_id}:{home}", sportsbook_quotes=home_book_quotes, polymarket_quote=home_poly, match_score=1.0
            ),
            MatchedMarket(
                match_id=f"{event_id}:{away}", sportsbook_quotes=away_book_quotes, polymarket_quote=away_poly, match_score=1.0
            ),
        ]

        events.append(
            SyntheticEvent(
                event_id=event_id, home_team=home, away_team=away, home_wins=home_wins, matched_markets=matched_markets
            )
        )

    return events
