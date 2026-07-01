"""Polymarket CLOB market-data client -- public, read-only order book prices.

The CLOB REST API (https://clob.polymarket.com) exposes best bid/ask and
order book depth per token_id without authentication. This is the low-latency
read path used every polling cycle (Layer 1); the Gamma client above is only
used periodically to discover *which* markets/tokens exist.

This client implements PolymarketDataClient by combining Gamma discovery with
CLOB price reads, matched against our own team-name matcher (Layer 3) rather
than Polymarket's own event grouping, since we need to cross-reference against
sportsbook team names.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from dateutil import parser as dateutil_parser
from tenacity import retry, stop_after_attempt, wait_exponential

from arbbot.models import PolymarketQuote
from arbbot.polymarket.base import PolymarketDataClient
from arbbot.polymarket.gamma_client import GammaClient

logger = logging.getLogger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"


class ClobMarketDataClient(PolymarketDataClient):
    name = "clob"

    def __init__(self, session: aiohttp.ClientSession | None = None, gamma: GammaClient | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        self._gamma = gamma or GammaClient(session=session)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.3, max=2))
    async def _fetch_price(self, session: aiohttp.ClientSession, token_id: str, side: str) -> float | None:
        try:
            async with session.get(
                f"{CLOB_BASE_URL}/price",
                params={"token_id": token_id, "side": side},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data.get("price")) if data.get("price") is not None else None
        except (aiohttp.ClientError, ValueError, TypeError, asyncio.TimeoutError):
            return None

    async def fetch_sports_markets(self) -> list[PolymarketQuote]:
        markets = await self._gamma.fetch_active_sports_markets()
        session = await self._get_session()
        quotes: list[PolymarketQuote] = []

        for m in markets:
            outcomes = m["outcomes"]
            token_ids = m["token_ids"]
            if len(outcomes) != len(token_ids):
                continue
            try:
                end_ts = dateutil_parser.isoparse(m["end_date_iso"]).timestamp() if m.get("end_date_iso") else 0.0
            except (ValueError, TypeError):
                end_ts = 0.0

            for outcome_name, token_id in zip(outcomes, token_ids):
                bid_task = self._fetch_price(session, token_id, "buy")
                ask_task = self._fetch_price(session, token_id, "sell")
                bid, ask = await asyncio.gather(bid_task, ask_task)
                if bid is None or ask is None:
                    continue
                quotes.append(
                    PolymarketQuote(
                        market_id=m["condition_id"] or "",
                        token_id=token_id,
                        question=m["question"],
                        outcome_name=outcome_name,
                        best_bid=bid,
                        best_ask=ask,
                        liquidity_usd=m["liquidity_usd"],
                        end_date=end_ts,
                        observed_at=time.time(),
                    )
                )
        return quotes

    async def close(self) -> None:
        await self._gamma.close()
        if self._owns_session and self._session is not None:
            await self._session.close()
