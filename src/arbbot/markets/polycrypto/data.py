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
    name = "clob_crypto"

    def __init__(self, clob: ClobMarketDataClient | None = None, market_limit: int = 150) -> None:
        self._clob = clob or ClobMarketDataClient()
        self._market_limit = market_limit

    async def fetch_crypto_markets(self) -> list[PolymarketQuote]:
        return await self._clob.fetch_markets_by_tag("crypto", limit=self._market_limit)

    async def close(self) -> None:
        await self._clob.close()
