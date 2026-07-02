"""Polymarket crypto-market gap detection: two independent layers.

Layer A -- complement arbitrage (riskless, primary):
    A binary Polymarket market pays exactly $1 per share to whichever side
    wins. If the YES ask plus the NO ask is under $1 (after fees), buying
    both sides in equal share counts locks in the difference no matter how
    the market resolves -- the prediction-market equivalent of the sports
    "surebet" in strategy/arbitrage.py. Needs no price model and both legs
    execute on the same venue, so it's the closest thing to the "high
    frequency, high confidence" profile: small, frequent, resolution-proof
    gaps. These show up when the two order books drift apart faster than
    market makers rebalance them (most often right after sharp spot moves).

Layer B -- spot-anchored value gaps (statistical, secondary):
    Prices threshold markets ("Will BTC be above $X on <date>?") against
    live spot prices from the crypto exchanges (markets/crypto/exchanges.py)
    using the volatility model in pricing.py, and flags markets whose quote
    disagrees with the model by a wide margin. This is the cross-venue
    "Polymarket vs other markets" gap the bot hunts, but it is NOT riskless:
    the model can be wrong, so signals carry a real confidence score (sigma
    distance, liquidity, spread, time decay) and sizing treats them like
    value_edge, not arbitrage. min_edge here is deliberately much larger
    than Layer A's.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from arbbot.markets.polycrypto.parser import parse_threshold_question
from arbbot.markets.polycrypto.pricing import SECONDS_PER_YEAR, distance_in_sigmas, model_yes_probability
from arbbot.models import MarketFamily, MarketOpportunity, PolymarketQuote


@dataclass(slots=True)
class SpotAnchorConfig:
    min_edge_pct: float = 6.0            # model prob minus ask, in probability points
    max_edge_pct: float = 30.0           # beyond this assume the model (or quote) is broken
    min_confidence: float = 0.75
    max_days_to_expiry: float = 45.0     # vol assumptions degrade over long horizons
    annualized_vol: dict[str, float] | None = None
    default_annualized_vol: float = 0.80

    def vol_for(self, symbol: str) -> float:
        if self.annualized_vol and symbol in self.annualized_vol:
            return self.annualized_vol[symbol]
        return self.default_annualized_vol


def _group_by_market(quotes: list[PolymarketQuote]) -> dict[str, list[PolymarketQuote]]:
    grouped: dict[str, list[PolymarketQuote]] = defaultdict(list)
    for q in quotes:
        grouped[q.market_id].append(q)
    return grouped


def detect_complement_arbitrage(
    quotes: list[PolymarketQuote],
    min_edge_pct: float = 0.4,
    max_edge_pct: float = 5.0,
    fee_buffer_pct: float = 0.2,
    min_liquidity_usd: float = 250.0,
    max_spread_cents: float = 3.0,
) -> list[MarketOpportunity]:
    """Layer A: buy every outcome of a binary market when the combined ask is
    under $1 after the fee buffer. edge_pct is the locked-in return on cost.
    """
    opportunities: list[MarketOpportunity] = []
    for market_id, outcome_quotes in _group_by_market(quotes).items():
        if len(outcome_quotes) != 2:
            continue  # only binary markets have the $1 complement identity
        a, b = outcome_quotes
        combined_ask = a.best_ask + b.best_ask
        if combined_ask <= 0:
            continue

        gross_edge_pct = (1.0 - combined_ask) / combined_ask * 100.0
        edge_pct = gross_edge_pct - fee_buffer_pct
        if edge_pct < min_edge_pct or gross_edge_pct > max_edge_pct:
            continue
        if min(a.liquidity_usd, b.liquidity_usd) < min_liquidity_usd:
            continue
        # Spread is measured in absolute cents, not % of mid: prediction
        # market tokens near $0.05 have huge relative spreads that are still
        # perfectly tradeable in absolute terms.
        widest_spread = max(a.best_ask - a.best_bid, b.best_ask - b.best_bid)
        if widest_spread * 100.0 > max_spread_cents:
            continue

        # Riskless by construction, so confidence only reflects execution
        # quality: how deep the books are and how tight the quotes sit.
        liquidity_component = min(1.0, min(a.liquidity_usd, b.liquidity_usd) / 5000.0)
        # widest_spread can be negative on a crossed book, so clamp the
        # component (and thus confidence) rather than letting it exceed 1.
        spread_component = min(1.0, max(0.0, 1.0 - widest_spread / 0.05))
        confidence = round(min(1.0, 0.5 + 0.25 * liquidity_component + 0.25 * spread_component), 4)

        opportunities.append(
            MarketOpportunity(
                family=MarketFamily.POLYCRYPTO,
                symbol=a.question[:80],
                edge_pct=edge_pct,
                confidence=confidence,
                legs=[
                    {
                        "venue": "polymarket",
                        "action": "buy",
                        "symbol": q.outcome_name,
                        "token_id": q.token_id,
                        "price": q.best_ask,
                    }
                    for q in (a, b)
                ],
                metadata={
                    "strategy": "complement",
                    "market_id": market_id,
                    "question": a.question,
                    "combined_ask": round(combined_ask, 4),
                    "gross_edge_pct": round(gross_edge_pct, 4),
                    "min_leg_liquidity_usd": min(a.liquidity_usd, b.liquidity_usd),
                },
            )
        )
    return opportunities


def detect_spot_anchored_gaps(
    quotes: list[PolymarketQuote],
    spot_mids: dict[str, float],
    cfg: SpotAnchorConfig,
    min_liquidity_usd: float = 250.0,
    max_spread_cents: float = 3.0,
    now: float | None = None,
) -> list[MarketOpportunity]:
    """Layer B: buy the side of a threshold market that the spot-anchored
    volatility model says is materially underpriced. edge_pct is the gap in
    probability points between the model and the ask being lifted.
    """
    now = time.time() if now is None else now
    opportunities: list[MarketOpportunity] = []

    for market_id, outcome_quotes in _group_by_market(quotes).items():
        if len(outcome_quotes) != 2:
            continue
        yes = next((q for q in outcome_quotes if q.outcome_name.strip().lower() == "yes"), None)
        no = next((q for q in outcome_quotes if q.outcome_name.strip().lower() == "no"), None)
        if yes is None or no is None:
            continue

        market = parse_threshold_question(yes.question)
        if market is None:
            continue
        spot = spot_mids.get(market.symbol)
        if spot is None or spot <= 0:
            continue

        t_sec = yes.end_date - now
        if t_sec <= 0 or t_sec > cfg.max_days_to_expiry * 86400.0:
            continue
        if min(yes.liquidity_usd, no.liquidity_usd) < min_liquidity_usd:
            continue
        widest_spread = max(yes.best_ask - yes.best_bid, no.best_ask - no.best_bid)
        if widest_spread * 100.0 > max_spread_cents:
            continue

        t_years = t_sec / SECONDS_PER_YEAR
        vol = cfg.vol_for(market.symbol)
        model_p = model_yes_probability(market, spot, t_years, vol)

        # Whichever side the model says is cheap; edge in probability points.
        yes_edge_pct = (model_p - yes.best_ask) * 100.0
        no_edge_pct = ((1.0 - model_p) - no.best_ask) * 100.0
        side, quote, edge_pct = ("yes", yes, yes_edge_pct) if yes_edge_pct >= no_edge_pct else ("no", no, no_edge_pct)
        if edge_pct < cfg.min_edge_pct or edge_pct > cfg.max_edge_pct:
            continue

        sigmas = distance_in_sigmas(spot, market.threshold_usd, t_years, vol)
        sigma_component = min(1.0, sigmas / 3.0)
        liquidity_component = min(1.0, min(yes.liquidity_usd, no.liquidity_usd) / 5000.0)
        spread_component = min(1.0, max(0.0, 1.0 - widest_spread / 0.05))
        horizon_component = max(0.0, 1.0 - t_sec / (cfg.max_days_to_expiry * 86400.0))
        confidence = round(
            min(
                1.0,
                0.40 * sigma_component
                + 0.25 * liquidity_component
                + 0.20 * spread_component
                + 0.15 * horizon_component,
            ),
            4,
        )
        if confidence < cfg.min_confidence:
            continue

        opportunities.append(
            MarketOpportunity(
                family=MarketFamily.POLYCRYPTO,
                symbol=market.symbol,
                edge_pct=edge_pct,
                confidence=confidence,
                legs=[
                    {
                        "venue": "polymarket",
                        "action": "buy",
                        "symbol": quote.outcome_name,
                        "token_id": quote.token_id,
                        "price": quote.best_ask,
                    }
                ],
                metadata={
                    "strategy": "spot_anchor",
                    "market_id": market_id,
                    "question": yes.question,
                    "side": side,
                    "model_prob": round(model_p, 4),
                    "spot": spot,
                    "threshold_usd": market.threshold_usd,
                    "kind": market.kind.value,
                    "sigmas": round(sigmas, 3),
                    "days_to_expiry": round(t_sec / 86400.0, 2),
                    "min_leg_liquidity_usd": min(yes.liquidity_usd, no.liquidity_usd),
                },
            )
        )
    return opportunities
