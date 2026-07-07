"""Synthetic crypto price feed for offline paper trading / tests, mirroring
the same pattern as the sports mock providers: mostly-agreeing prices across
exchanges with an occasionally injected cross-exchange gap.
"""

from __future__ import annotations

import random

from arbbot.markets.crypto.exchanges import CryptoPriceFeed
from arbbot.models import CryptoQuote

# PAXG is a gold-backed token (~1 troy oz); it gets a tighter synthetic spread
# below to mimic gold's lower volatility versus the crypto majors.
_BASE_PRICES = {"BTC/USD": 65000.0, "ETH/USD": 3400.0, "SOL/USD": 150.0, "PAXG/USD": 2650.0}
_STABLE_SYMBOLS = {"PAXG/USD"}
_EXCHANGES = ["mock_exchange_a", "mock_exchange_b", "mock_exchange_c"]


class MockCryptoPriceFeed(CryptoPriceFeed):
    exchange_name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    async def fetch_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        quotes: list[CryptoQuote] = []
        for symbol in symbols:
            base_price = _BASE_PRICES.get(symbol, 100.0)
            is_stable = symbol in _STABLE_SYMBOLS
            for exchange in _EXCHANGES:
                # Gold-backed tokens jitter less and quote tighter than the
                # volatile majors -- a calmer, higher-win-rate book.
                jitter_amp = 0.0004 if is_stable else 0.0015
                jitter = self._rng.uniform(-jitter_amp, jitter_amp)
                gap = self._rng.choice([0.0, 0.0, 0.0, self._rng.uniform(0.002, 0.008)])
                mid = base_price * (1 + jitter - gap)
                spread_lo, spread_hi = (0.0003, 0.0008) if is_stable else (0.0005, 0.0015)
                half_spread = base_price * self._rng.uniform(spread_lo, spread_hi)
                quotes.append(
                    CryptoQuote(
                        exchange=exchange,
                        symbol=symbol,
                        bid=round(mid - half_spread, 2),
                        ask=round(mid + half_spread, 2),
                    )
                )
        return quotes
