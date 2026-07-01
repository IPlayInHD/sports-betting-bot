"""Synthetic Polymarket client for paper trading / backtests / tests without
network access. Mirrors the same synthetic matchups the mock odds provider
generates, with a deliberately injected pricing gap so the arbitrage layer
has something real to catch in demos.
"""

from __future__ import annotations

import random
import time

from arbbot.models import PolymarketQuote
from arbbot.polymarket.base import PolymarketDataClient

_TEAMS = {
    "basketball_nba": [("Los Angeles Lakers", "Boston Celtics"), ("Golden State Warriors", "Miami Heat")],
    "americanfootball_nfl": [("Kansas City Chiefs", "Buffalo Bills"), ("San Francisco 49ers", "Dallas Cowboys")],
    "soccer_epl": [("Manchester City", "Liverpool"), ("Arsenal", "Chelsea")],
    "baseball_mlb": [("New York Yankees", "Los Angeles Dodgers"), ("Houston Astros", "Atlanta Braves")],
}


class MockPolymarketClient(PolymarketDataClient):
    name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    async def fetch_sports_markets(self) -> list[PolymarketQuote]:
        quotes: list[PolymarketQuote] = []
        for sport_key, matchups in _TEAMS.items():
            for home, away in matchups:
                event_id = f"{sport_key}:{home}:{away}".replace(" ", "_")
                end_date = time.time() + self._rng.uniform(3600, 3 * 24 * 3600)
                true_home_prob = self._rng.uniform(0.35, 0.65)
                # Occasionally inject a deliberate mispricing gap for demos/tests.
                gap = self._rng.choice([0.0, 0.0, 0.0, self._rng.uniform(0.01, 0.04)])
                home_mid = min(max(true_home_prob - gap, 0.02), 0.98)
                away_mid = 1.0 - home_mid
                half_spread = self._rng.uniform(0.005, 0.02)
                liquidity = self._rng.uniform(200, 5000)

                quotes.append(
                    PolymarketQuote(
                        market_id=event_id,
                        token_id=f"{event_id}:{home}",
                        question=f"Will the {home} beat the {away}?",
                        outcome_name=home,
                        best_bid=round(max(home_mid - half_spread, 0.01), 4),
                        best_ask=round(min(home_mid + half_spread, 0.99), 4),
                        liquidity_usd=round(liquidity, 2),
                        end_date=end_date,
                    )
                )
                quotes.append(
                    PolymarketQuote(
                        market_id=event_id,
                        token_id=f"{event_id}:{away}",
                        question=f"Will the {home} beat the {away}?",
                        outcome_name=away,
                        best_bid=round(max(away_mid - half_spread, 0.01), 4),
                        best_ask=round(min(away_mid + half_spread, 0.99), 4),
                        liquidity_usd=round(liquidity, 2),
                        end_date=end_date,
                    )
                )
        return quotes
