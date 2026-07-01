"""Abstract Polymarket market-data interface (Layer 1: data ingestion)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from arbbot.models import PolymarketQuote


class PolymarketDataClient(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_sports_markets(self) -> list[PolymarketQuote]:
        """Return current best bid/ask for every open sports-related market."""
        raise NotImplementedError

    async def close(self) -> None:
        return None
