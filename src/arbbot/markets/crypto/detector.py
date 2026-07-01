"""Cross-exchange crypto arbitrage detection.

For each symbol, find the cheapest ask (buy here) and the highest bid (sell
here) across all configured exchanges. If selling covers buying plus both
legs' taker fees with room to spare, that's a genuine buy-low/sell-high gap.

See exchanges.py's module docstring for the important real-world caveat:
this assumes you already hold pre-funded balances on every configured
exchange, since crypto transfers between exchanges are far too slow to
capture a gap that exists right now.
"""

from __future__ import annotations

from collections import defaultdict

from arbbot.models import CryptoQuote, MarketFamily, MarketOpportunity


def detect_crypto_arbitrage(
    quotes: list[CryptoQuote],
    min_edge_pct: float = 0.3,
    max_edge_pct: float = 5.0,
    fee_pct_per_leg: float = 0.1,
) -> list[MarketOpportunity]:
    by_symbol: dict[str, list[CryptoQuote]] = defaultdict(list)
    for q in quotes:
        by_symbol[q.symbol].append(q)

    opportunities: list[MarketOpportunity] = []
    for symbol, symbol_quotes in by_symbol.items():
        if len(symbol_quotes) < 2:
            continue

        cheapest = min(symbol_quotes, key=lambda q: q.ask)
        priciest = max(symbol_quotes, key=lambda q: q.bid)
        if cheapest.exchange == priciest.exchange:
            continue  # need two distinct venues to actually arbitrage

        gross_edge_pct = (priciest.bid - cheapest.ask) / cheapest.ask * 100.0
        edge_pct = gross_edge_pct - (2 * fee_pct_per_leg)

        if edge_pct < min_edge_pct or gross_edge_pct > max_edge_pct:
            continue

        opportunities.append(
            MarketOpportunity(
                family=MarketFamily.CRYPTO,
                symbol=symbol,
                edge_pct=edge_pct,
                confidence=1.0,
                legs=[
                    {"venue": cheapest.exchange, "action": "buy", "symbol": symbol, "price": cheapest.ask},
                    {"venue": priciest.exchange, "action": "sell", "symbol": symbol, "price": priciest.bid},
                ],
                metadata={
                    "gross_edge_pct": gross_edge_pct,
                    "buy_exchange": cheapest.exchange,
                    "sell_exchange": priciest.exchange,
                },
            )
        )

    return opportunities
