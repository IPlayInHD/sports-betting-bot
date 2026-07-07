"""Crypto price feeds: cross-exchange best bid/ask for a set of symbols.

All three real adapters below hit genuinely public, unauthenticated,
well-documented ticker endpoints -- no API key or account needed to read
prices (only live order placement needs credentials; see execution.py).
Verify against each exchange's current docs if you hit unexpected errors,
since public API schemas do occasionally change:
  - Coinbase Exchange: https://docs.cloud.coinbase.com/exchange/reference
  - Kraken:             https://docs.kraken.com/rest/
  - Binance:            https://binance-docs.github.io/apidocs/spot/en/

IMPORTANT REAL-WORLD CAVEAT: genuine cross-exchange arbitrage requires you
to already hold balances on BOTH exchanges before a gap appears. Moving
crypto between exchanges takes minutes and costs network fees, which is far
too slow to capture a gap that shows up right now -- so this bot (in both
paper and live mode) assumes you have pre-funded balances sitting on each
configured exchange and just buys on one / sells on the other
simultaneously. It does not attempt any cross-exchange transfer.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import aiohttp

from arbbot.models import CryptoQuote

logger = logging.getLogger(__name__)


class CryptoPriceFeed(ABC):
    exchange_name: str = "base"

    @abstractmethod
    async def fetch_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        """symbols use the normalized "BASE/QUOTE" form, e.g. "BTC/USD"."""
        raise NotImplementedError

    async def close(self) -> None:
        return None


class CoinbasePriceFeed(CryptoPriceFeed):
    exchange_name = "coinbase"
    BASE_URL = "https://api.exchange.coinbase.com"

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        session = await self._get_session()
        quotes: list[CryptoQuote] = []
        for symbol in symbols:
            product_id = symbol.replace("/", "-")
            try:
                async with session.get(
                    f"{self.BASE_URL}/products/{product_id}/ticker", timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                quotes.append(
                    CryptoQuote(
                        exchange=self.exchange_name, symbol=symbol, bid=float(data["bid"]), ask=float(data["ask"])
                    )
                )
            except (aiohttp.ClientError, KeyError, ValueError, TypeError) as exc:
                logger.debug("coinbase quote fetch failed for %s: %s", symbol, exc)
        return quotes

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()


class KrakenPriceFeed(CryptoPriceFeed):
    exchange_name = "kraken"
    BASE_URL = "https://api.kraken.com/0/public"

    # Kraken uses idiosyncratic pair codes rather than plain "BTC/USD".
    # PAXG (PAX Gold) and XAUT (Tether Gold) are ERC-20 tokens each redeemable
    # for one fine troy ounce of gold -- they track the spot gold price, so
    # they're the low-volatility "stable trend" leg of the cross-exchange book.
    _SYMBOL_MAP = {
        "BTC/USD": "XBTUSD",
        "ETH/USD": "ETHUSD",
        "SOL/USD": "SOLUSD",
        "LTC/USD": "LTCUSD",
        "PAXG/USD": "PAXGUSD",   # gold-backed token
    }

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        pairs = [self._SYMBOL_MAP[s] for s in symbols if s in self._SYMBOL_MAP]
        if not pairs:
            return []

        session = await self._get_session()
        quotes: list[CryptoQuote] = []
        try:
            async with session.get(
                f"{self.BASE_URL}/Ticker", params={"pair": ",".join(pairs)}, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            logger.debug("kraken quote fetch failed: %s", exc)
            return []

        if payload.get("error"):
            logger.debug("kraken API error: %s", payload["error"])
            return []

        reverse_map = {v: k for k, v in self._SYMBOL_MAP.items()}
        for pair_code, ticker in payload.get("result", {}).items():
            symbol = reverse_map.get(pair_code)
            if symbol is None:
                continue
            try:
                quotes.append(
                    CryptoQuote(
                        exchange=self.exchange_name,
                        symbol=symbol,
                        bid=float(ticker["b"][0]),
                        ask=float(ticker["a"][0]),
                    )
                )
            except (KeyError, ValueError, IndexError):
                continue
        return quotes

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()


class BinancePriceFeed(CryptoPriceFeed):
    exchange_name = "binance"

    def __init__(self, session: aiohttp.ClientSession | None = None, base_url: str = "https://api.binance.us") -> None:
        # Defaults to Binance.US since binance.com blocks US IP addresses.
        # Pass base_url="https://api.binance.com" if you're trading from
        # somewhere Binance.US isn't available instead.
        self._session = session
        self._owns_session = session is None
        self._base_url = base_url
        # ccxt treats binance.com and Binance.US as distinct exchange ids
        # ("binance" vs "binanceus"); label ourselves accordingly so live
        # order routing (execution.py) picks the matching ccxt exchange.
        self.exchange_name = "binanceus" if "binance.us" in base_url else "binance"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch_quotes(self, symbols: list[str]) -> list[CryptoQuote]:
        session = await self._get_session()
        quotes: list[CryptoQuote] = []
        for symbol in symbols:
            base, _, quote = symbol.partition("/")
            # Binance.US lists USD pairs as e.g. "BTCUSD" (plain concatenation);
            # binance.com would need "BTCUSDT" instead for most USD-quoted pairs.
            binance_symbol = f"{base}{quote}"
            try:
                async with session.get(
                    f"{self._base_url}/api/v3/ticker/bookTicker",
                    params={"symbol": binance_symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                quotes.append(
                    CryptoQuote(
                        exchange=self.exchange_name,
                        symbol=symbol,
                        bid=float(data["bidPrice"]),
                        ask=float(data["askPrice"]),
                    )
                )
            except (aiohttp.ClientError, KeyError, ValueError, TypeError) as exc:
                logger.debug("binance quote fetch failed for %s: %s", symbol, exc)
        return quotes

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
