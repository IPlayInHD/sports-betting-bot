"""Synthetic Polymarket crypto markets for offline paper trading / tests.

Generates binary threshold markets whose questions parse cleanly with
parser.py and whose implied probabilities hover around what pricing.py
would compute from the mock spot prices (markets/crypto/mock.py uses the
same _BASE_PRICES), so the spot-anchor layer sees a coherent world. Two
kinds of gap are injected occasionally, mirroring how the other mock feeds
seed detectable opportunities for demos/tests:
  * a complement gap: YES ask + NO ask drops below $1
  * a model gap: one side's quote drifts well away from the model price
"""

from __future__ import annotations

import random
import time

from arbbot.markets.crypto.mock import _BASE_PRICES
from arbbot.markets.polycrypto.data import PolymarketCryptoFeed
from arbbot.markets.polycrypto.pricing import SECONDS_PER_YEAR, terminal_above_probability
from arbbot.models import PolymarketQuote

_ASSET_NAMES = {"BTC/USD": "Bitcoin", "ETH/USD": "Ethereum", "SOL/USD": "Solana"}
_MOCK_VOL = 0.6


class MockPolymarketCryptoFeed(PolymarketCryptoFeed):
    name = "mock_crypto"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    async def fetch_crypto_markets(self) -> list[PolymarketQuote]:
        quotes: list[PolymarketQuote] = []
        now = time.time()
        for symbol, spot in _BASE_PRICES.items():
            asset = _ASSET_NAMES.get(symbol, symbol.split("/")[0])
            # A few strikes bracketing spot, expiring days to weeks out.
            for strike_mult in (0.92, 1.0, 1.08, 1.2):
                threshold = round(spot * strike_mult, -1)
                days_out = self._rng.uniform(1, 14)
                end_date = now + days_out * 86400.0
                t_years = (end_date - now) / SECONDS_PER_YEAR
                fair_p = terminal_above_probability(spot, threshold, t_years, _MOCK_VOL)

                # Market makers quote around fair value most of the time...
                noise = self._rng.uniform(-0.015, 0.015)
                mid = min(max(fair_p + noise, 0.02), 0.98)
                half_spread = self._rng.uniform(0.004, 0.015)
                # ...but occasionally one of the two injectable gaps appears.
                gap_kind = self._rng.choice(["none"] * 4 + ["complement", "model"])
                complement_discount = self._rng.uniform(0.01, 0.03) if gap_kind == "complement" else 0.0
                model_drift = self._rng.uniform(0.10, 0.20) * self._rng.choice([-1, 1]) if gap_kind == "model" else 0.0
                mid = min(max(mid + model_drift, 0.02), 0.98)

                market_id = f"mock_poly_crypto:{symbol}:{threshold:.0f}"
                question = f"Will {asset} be above ${threshold:,.0f} on the expiry date?"
                liquidity = self._rng.uniform(300, 8000)

                yes_ask = min(mid + half_spread - complement_discount / 2, 0.99)
                no_ask = min((1.0 - mid) + half_spread - complement_discount / 2, 0.99)
                quotes.append(
                    PolymarketQuote(
                        market_id=market_id,
                        token_id=f"{market_id}:yes",
                        question=question,
                        outcome_name="Yes",
                        best_bid=round(max(mid - half_spread, 0.01), 4),
                        best_ask=round(max(yes_ask, 0.01), 4),
                        liquidity_usd=round(liquidity, 2),
                        end_date=end_date,
                    )
                )
                quotes.append(
                    PolymarketQuote(
                        market_id=market_id,
                        token_id=f"{market_id}:no",
                        question=question,
                        outcome_name="No",
                        best_bid=round(max((1.0 - mid) - half_spread, 0.01), 4),
                        best_ask=round(max(no_ask, 0.01), 4),
                        liquidity_usd=round(liquidity, 2),
                        end_date=end_date,
                    )
                )
        return quotes
