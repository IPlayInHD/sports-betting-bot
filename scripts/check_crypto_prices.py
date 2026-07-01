#!/usr/bin/env python3
"""Diagnostic tool: fetch current real prices from every configured crypto
exchange and print them side by side, plus the cross-exchange edge the bot
would compute right now. Read-only -- never places an order.

Use this to independently sanity-check what the live bot is reporting. Run
it a few times, a minute or so apart, and look at whether:
  - the "gap" is always the same exchange pair in the same direction
    (e.g. always "buy Kraken, sell Coinbase") -- a red flag for a data
    artifact (symbol mismatch, stale cache, systematic feed offset) rather
    than a genuine, closing arbitrage opportunity, or
  - it moves around / disappears between runs, which is more consistent
    with a real (if fleeting and probably too-thin-to-trade) gap.

Usage:
    python scripts/check_crypto_prices.py
    python scripts/check_crypto_prices.py --symbols BTC/USD ETH/USD
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arbbot.config import load_config  # noqa: E402
from arbbot.markets.crypto.detector import detect_crypto_arbitrage  # noqa: E402
from arbbot.markets.crypto.exchanges import BinancePriceFeed, CoinbasePriceFeed, KrakenPriceFeed  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and compare live crypto prices across exchanges")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    symbols = args.symbols or cfg.markets.crypto.symbols

    feeds = {
        "coinbase": CoinbasePriceFeed(),
        "kraken": KrakenPriceFeed(),
        "binanceus": BinancePriceFeed(base_url="https://api.binance.us"),
    }

    print(f"Fetching live quotes for {symbols} from {list(feeds)}...\n")

    all_quotes = []
    for name, feed in feeds.items():
        try:
            quotes = await feed.fetch_quotes(symbols)
            all_quotes.extend(quotes)
        except Exception as exc:  # noqa: BLE001 - diagnostic script, report and continue
            print(f"  {name}: FAILED ({exc})")
        finally:
            await feed.close()

    by_symbol: dict[str, list] = {}
    for q in all_quotes:
        by_symbol.setdefault(q.symbol, []).append(q)

    for symbol in symbols:
        print(f"--- {symbol} ---")
        quotes = sorted(by_symbol.get(symbol, []), key=lambda q: q.exchange)
        if not quotes:
            print("  no data (all exchanges failed, or this exchange doesn't list this pair)")
            continue
        for q in quotes:
            print(f"  {q.exchange:12s} bid={q.bid:>14.4f}  ask={q.ask:>14.4f}")
        if len(quotes) >= 2:
            cheapest = min(quotes, key=lambda q: q.ask)
            priciest = max(quotes, key=lambda q: q.bid)
            gross_edge_pct = (priciest.bid - cheapest.ask) / cheapest.ask * 100.0
            print(f"  => buy on {cheapest.exchange} @ {cheapest.ask}, sell on {priciest.exchange} @ {priciest.bid}")
            print(f"  => gross edge: {gross_edge_pct:.3f}% (before fees)")
        print()

    opportunities = detect_crypto_arbitrage(
        all_quotes,
        min_edge_pct=cfg.markets.crypto.min_edge_pct,
        max_edge_pct=cfg.markets.crypto.max_edge_pct,
        fee_pct_per_leg=cfg.markets.crypto.fee_pct_per_leg,
    )
    print(f"Bot would currently detect {len(opportunities)} opportunity(ies) with your configured thresholds.")
    for opp in opportunities:
        print(f"  {opp.symbol}: net edge {opp.edge_pct:.3f}% ({opp.metadata['buy_exchange']} -> {opp.metadata['sell_exchange']})")


if __name__ == "__main__":
    asyncio.run(main())
