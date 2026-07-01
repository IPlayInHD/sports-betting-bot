"""Layer 7: multi-factor confidence scoring.

Combines several independent signals into a single 0-1 confidence score that
position sizing uses to scale stake size -- higher-confidence signals get
closer to their full risk-managed size, marginal ones get scaled down rather
than rejected outright. This is a second, finer-grained lever (beyond the
binary filter pipeline) for improving realized win rate / risk-adjusted
return.
"""

from __future__ import annotations

from arbbot.models import GapSignal, SignalType


def score_confidence(signal: GapSignal, historical_fill_rate: float = 1.0) -> float:
    matched = signal.matched_market
    match_score_component = matched.match_score

    liquidity = matched.polymarket_quote.liquidity_usd
    liquidity_component = min(1.0, liquidity / 5000.0)

    spread_pct = matched.polymarket_quote.spread_pct
    spread_component = max(0.0, 1.0 - spread_pct / 5.0)

    book_count = len(matched.sportsbook_quotes)
    corroboration_component = min(1.0, book_count / 3.0)

    fill_rate_component = max(0.0, min(1.0, historical_fill_rate))

    if signal.signal_type == SignalType.ARBITRAGE:
        weights = {"match": 0.30, "liquidity": 0.25, "spread": 0.20, "corroboration": 0.10, "fill_rate": 0.15}
    else:  # VALUE_EDGE relies more heavily on corroborating books
        weights = {"match": 0.20, "liquidity": 0.20, "spread": 0.15, "corroboration": 0.30, "fill_rate": 0.15}

    score = (
        weights["match"] * match_score_component
        + weights["liquidity"] * liquidity_component
        + weights["spread"] * spread_component
        + weights["corroboration"] * corroboration_component
        + weights["fill_rate"] * fill_rate_component
    )
    return round(min(1.0, max(0.0, score)), 4)
