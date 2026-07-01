"""Synthetic odds provider used for paper-trading demos, backtests, and tests
when no real API key is configured. Generates plausible two-way markets with
a small, randomly-varying vig so the rest of the pipeline has real signal to
react to without requiring network access or credentials.
"""

from __future__ import annotations

import random
import time

from arbbot.models import SportsbookQuote
from arbbot.odds.base import OddsProvider

_TEAMS = {
    "basketball_nba": [("Los Angeles Lakers", "Boston Celtics"), ("Golden State Warriors", "Miami Heat")],
    "americanfootball_nfl": [("Kansas City Chiefs", "Buffalo Bills"), ("San Francisco 49ers", "Dallas Cowboys")],
    "soccer_epl": [("Manchester City", "Liverpool"), ("Arsenal", "Chelsea")],
    "baseball_mlb": [("New York Yankees", "Los Angeles Dodgers"), ("Houston Astros", "Atlanta Braves")],
}

_BOOKMAKERS = ["pinnacle", "draftkings", "fanduel", "betmgm"]


class MockOddsProvider(OddsProvider):
    name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    async def fetch_quotes(self, sport_key: str) -> list[SportsbookQuote]:
        matchups = _TEAMS.get(sport_key, [])
        quotes: list[SportsbookQuote] = []
        for home, away in matchups:
            event_id = f"{sport_key}:{home}:{away}".replace(" ", "_")
            commence_time = time.time() + self._rng.uniform(3600, 3 * 24 * 3600)
            true_home_prob = self._rng.uniform(0.35, 0.65)
            for bookmaker in _BOOKMAKERS:
                vig = self._rng.uniform(0.02, 0.06)
                jitter = self._rng.uniform(-0.02, 0.02)
                home_prob = min(max(true_home_prob + jitter, 0.05), 0.95)
                away_prob = 1.0 - home_prob
                overround_scale = 1.0 + vig
                home_prob_vig = min(home_prob * overround_scale, 0.98)
                away_prob_vig = min(away_prob * overround_scale, 0.98)
                quotes.append(
                    SportsbookQuote(
                        bookmaker=bookmaker,
                        event_id=event_id,
                        sport_key=sport_key,
                        home_team=home,
                        away_team=away,
                        commence_time=commence_time,
                        outcome_name=home,
                        decimal_odds=round(1.0 / home_prob_vig, 3),
                    )
                )
                quotes.append(
                    SportsbookQuote(
                        bookmaker=bookmaker,
                        event_id=event_id,
                        sport_key=sport_key,
                        home_team=home,
                        away_team=away,
                        commence_time=commence_time,
                        outcome_name=away,
                        decimal_odds=round(1.0 / away_prob_vig, 3),
                    )
                )
        return quotes
