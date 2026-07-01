"""Polymarket Gamma API client -- read-only market discovery/metadata.

Gamma (https://gamma-api.polymarket.com) is Polymarket's public metadata API:
it lists markets/events with their question text, outcomes, CLOB token ids,
end dates, and volume/liquidity. No API key or wallet is required for reads.

Verify field names against https://docs.polymarket.com if the vendor changes
their schema -- this adapter targets the documented `/markets` endpoint.
"""

from __future__ import annotations

import json
import logging

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

BASE_URL = "https://gamma-api.polymarket.com"


class GammaClient:
    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
    async def fetch_active_sports_markets(self, tag: str = "sports", limit: int = 200) -> list[dict]:
        """Return raw Gamma market dicts for currently active, non-closed markets
        tagged as sports. Caller is responsible for team/date matching.
        """
        session = await self._get_session()
        params = {
            "active": "true",
            "closed": "false",
            "tag": tag,
            "limit": str(limit),
        }
        async with session.get(f"{BASE_URL}/markets", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()

        markets = []
        for m in data:
            try:
                outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                token_ids = (
                    json.loads(m["clobTokenIds"])
                    if isinstance(m.get("clobTokenIds"), str)
                    else m.get("clobTokenIds", [])
                )
                markets.append(
                    {
                        "condition_id": m.get("conditionId"),
                        "question": m.get("question", ""),
                        "outcomes": outcomes,
                        "token_ids": token_ids,
                        "end_date_iso": m.get("endDate"),
                        "liquidity_usd": float(m.get("liquidityNum") or m.get("liquidity") or 0.0),
                        "volume_usd": float(m.get("volumeNum") or m.get("volume") or 0.0),
                    }
                )
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return markets

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
