"""Real sportsbook odds adapter for https://the-odds-api.com

Requires ODDS_API_KEY (see .env.example). This is a thin, read-only REST
client -- it never places bets. The Odds API aggregates quotes from many
bookmakers, which is exactly what the multi-book value-edge layer needs for
consensus, and what the arbitrage layer needs to find the single best price
to trade against.

NOTE: endpoint paths/response schema are per the vendor's public docs as of
this writing. Verify against https://the-odds-api.com/liveapi/guides/v4/
if you hit unexpected 4xx errors, since third-party APIs evolve.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from arbbot.models import SportsbookQuote
from arbbot.odds.base import OddsProvider

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"


class TheOddsApiProvider(OddsProvider):
    name = "the_odds_api"

    def __init__(self, api_key: str, regions: str = "us,uk,eu", markets: str = "h2h", session: aiohttp.ClientSession | None = None) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY is required for TheOddsApiProvider")
        self._api_key = api_key
        self._regions = regions
        self._markets = markets
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
    async def fetch_quotes(self, sport_key: str) -> list[SportsbookQuote]:
        session = await self._get_session()
        url = f"{BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey": self._api_key,
            "regions": self._regions,
            "markets": self._markets,
            "oddsFormat": "decimal",
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 401:
                raise PermissionError("Invalid ODDS_API_KEY")
            resp.raise_for_status()
            payload = await resp.json()

        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            logger.debug("the_odds_api quota remaining=%s", remaining)

        return self._parse(payload, sport_key)

    @staticmethod
    def _parse(payload: list[dict], sport_key: str) -> list[SportsbookQuote]:
        quotes: list[SportsbookQuote] = []
        for event in payload:
            event_id = event.get("id", "")
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")
            commence_iso = event.get("commence_time")
            commence_time = (
                datetime.fromisoformat(commence_iso.replace("Z", "+00:00")).timestamp()
                if commence_iso
                else 0.0
            )
            for bookmaker in event.get("bookmakers", []):
                book_key = bookmaker.get("key", "unknown")
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for outcome in market.get("outcomes", []):
                        try:
                            quotes.append(
                                SportsbookQuote(
                                    bookmaker=book_key,
                                    event_id=event_id,
                                    sport_key=sport_key,
                                    home_team=home_team,
                                    away_team=away_team,
                                    commence_time=commence_time,
                                    outcome_name=outcome["name"],
                                    decimal_odds=float(outcome["price"]),
                                )
                            )
                        except (KeyError, ValueError, ZeroDivisionError):
                            continue
        return quotes

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
