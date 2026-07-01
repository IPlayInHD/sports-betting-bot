"""Abstract sportsbook odds provider interface (Layer 1: data ingestion).

Any real provider (The Odds API, a direct exchange feed, a scraped source,
etc.) implements this interface so the rest of the pipeline never depends on
a specific vendor's schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from arbbot.models import SportsbookQuote


class OddsProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_quotes(self, sport_key: str) -> list[SportsbookQuote]:
        """Return the latest quotes for every event/outcome/bookmaker for a sport."""
        raise NotImplementedError

    async def close(self) -> None:
        return None
