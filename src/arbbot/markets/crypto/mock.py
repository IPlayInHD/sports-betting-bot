"""Synthetic crypto price feed for offline paper trading / tests, mirroring
the same pattern as the sports mock providers: mostly-agreeing prices across
exchanges with an occasionally injected cross-exchange gap.
"""

from __future__ import annotations

import random

from arbbot.markets.crypto.exchanges import CryptoPriceFeed
from arbbot.models import CryptoQuote

_BASE_PRICES = {"BTC/USD": 65000.0, "ETH/USD": 3400.0, "SOL/USD": 150.0}
_EXCHANGES = ["mock_exchange_a", "mock_exchange_b", "mock_exchange_c"]


class MockCryptoPriceFeed(CryptoPriceFeed):
    exchange_name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    async def fetch_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        quotes: list[CryptoQuote] = []
        for symbol in symbols:
            base_price = _BASE_PRICES.get(symbol, 100.0)
            for exchange in _EXCHANGES:
                jitter = self._rng.uniform(-0.0015, 0.0015)
                gap = self._rng.choice([0.0, 0.0, 0.0, self._rng.uniform(0.002, 0.008)])
                mid = base_price * (1 + jitter - gap)
                half_spread = base_price * self._rng.uniform(0.0005, 0.0015)
                quotes.append(
                    CryptoQuote(
                        exchange=exchange,
                        symbol=symbol,
                        bid=round(mid - half_spread, 2),
                        ask=round(mid + half_spread, 2),
                    )
                )
        return quotes
