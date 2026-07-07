"""Data feed for Polymarket's crypto prediction markets.

Discovery goes through the public Gamma API (tag=crypto) and per-token
pricing through the public CLOB REST API -- both unauthenticated reads,
reusing the exact same clients as the sports pipeline. The feed abstraction
exists so paper trading without network access can swap in mock.py's
synthetic generator, mirroring every other market family.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from arbbot.models import PolymarketQuote
from arbbot.polymarket.clob_market_data import ClobMarketDataClient


class PolymarketCryptoFeed(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_crypto_markets(self) -> list[PolymarketQuote]:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class PolymarketCryptoDataClient(PolymarketCryptoFeed):
    name = "clob_polymarket"

    def __init__(
        self,
        clob: ClobMarketDataClient | None = None,
        market_limit: int = 200,
        scan_all_markets: bool = True,
        tags: list[str] | None = None,
    ) -> None:
        self._clob = clob or ClobMarketDataClient()
        self._market_limit = market_limit
        self._scan_all = scan_all_markets
        self._tags = tags or ["crypto"]

    async def fetch_crypto_markets(self) -> list[PolymarketQuote]:
        if self._scan_all:
            # All active markets -- the riskless complement arb is asset-agnostic.
            return await self._clob.fetch_markets_by_tag(None, limit=self._market_limit)
        # Restricted to specific tags: fetch each, dedupe by token.
        seen: set[str] = set()
        merged: list[PolymarketQuote] = []
        for tag in self._tags:
            for q in await self._clob.fetch_markets_by_tag(tag, limit=self._market_limit):
                if q.token_id not in seen:
                    seen.add(q.token_id)
                    merged.append(q)
        return merged

    async def close(self) -> None:
        await self._clob.close()
