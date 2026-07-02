"""Layer 8a: position sizing.

Two distinct sizing rules are used because the two strategy layers have very
different risk profiles:

* Arbitrage signals are (near-)riskless, so sizing is simply "as much as the
  configured per-trade/exposure caps and available liquidity allow",
  optionally scaled down by confidence.
* Value-edge signals carry real variance, so they're sized with a
  fractional Kelly criterion, which is the standard approach for sizing bets
  with a known edge while controlling bankroll ruin risk.
"""

from __future__ import annotations

from arbbot.models import GapSignal, MarketOpportunity, SignalType


def kelly_fraction(win_prob: float, decimal_payout: float) -> float:
    """Standard Kelly formula for a binary bet.

    win_prob: probability the bet wins.
    decimal_payout: total return per $1 staked if it wins (i.e. decimal odds).
    Returns the fraction of bankroll to stake; can be negative (no bet) if
    there is no edge.
    """
    if decimal_payout <= 1.0:
        return 0.0
    b = decimal_payout - 1.0
    q = 1.0 - win_prob
    f = (b * win_prob - q) / b
    return max(0.0, f)


def size_arbitrage_signal(
    signal: GapSignal,
    bankroll_usd: float,
    max_stake_per_trade_pct: float,
    max_stake_per_trade_usd: float,
) -> float:
    """Return the total stake (USD, across all legs) for a riskless arbitrage
    signal, capped by both a percentage-of-bankroll and an absolute-dollar
    limit, then scaled by confidence and by available Polymarket liquidity so
    we never try to fill more size than the book can absorb without slippage.
    """
    pct_cap = bankroll_usd * (max_stake_per_trade_pct / 100.0)
    base_cap = min(pct_cap, max_stake_per_trade_usd)

    confidence_scaled = base_cap * max(0.25, signal.confidence)  # never zero out a passed signal entirely

    liquidity_cap = signal.matched_market.polymarket_quote.liquidity_usd * 0.10  # take <=10% of book depth
    return max(0.0, min(confidence_scaled, liquidity_cap))


def size_value_edge_signal(
    signal: GapSignal,
    bankroll_usd: float,
    kelly_fraction_cap: float,
    max_stake_per_trade_pct: float,
    max_stake_per_trade_usd: float,
) -> float:
    """Fractional-Kelly sizing for the higher-variance value-edge layer."""
    consensus_prob = signal.metadata.get("consensus_prob")
    poly_ask = signal.metadata.get("poly_ask")
    if consensus_prob is None or poly_ask is None or poly_ask <= 0:
        return 0.0

    decimal_payout = 1.0 / poly_ask
    full_kelly = kelly_fraction(consensus_prob, decimal_payout)
    fractional = full_kelly * kelly_fraction_cap * max(0.1, signal.confidence)

    pct_cap = max_stake_per_trade_pct / 100.0
    stake_fraction = min(fractional, pct_cap)
    stake_usd = stake_fraction * bankroll_usd
    return max(0.0, min(stake_usd, max_stake_per_trade_usd))


def size_crypto_opportunity(
    opportunity: MarketOpportunity,
    bankroll_usd: float,
    max_stake_per_trade_pct: float,
    max_stake_per_trade_usd: float,
) -> float:
    """Same "near-riskless -> use the full risk-managed cap, scaled by
    confidence" approach as sports arbitrage. Unlike Polymarket, the public
    ticker endpoints don't expose order book depth, so there's no liquidity
    cap here -- rely on the per-trade dollar cap to keep size conservative
    (real usage should assume real order book depth is unknown and keep this
    cap small).
    """
    pct_cap = bankroll_usd * (max_stake_per_trade_pct / 100.0)
    base_cap = min(pct_cap, max_stake_per_trade_usd)
    return base_cap * max(0.25, opportunity.confidence)


def size_polycrypto_opportunity(
    opportunity: MarketOpportunity,
    bankroll_usd: float,
    max_stake_per_trade_pct: float,
    max_stake_per_trade_usd: float,
    kelly_fraction_cap: float = 0.15,
) -> float:
    """Two sizing regimes matching the two polycrypto strategies:

    * complement is riskless once filled -> full risk-managed cap scaled by
      confidence, bounded by 10% of the thinner leg's book depth (same
      convention as sports arbitrage sizing).
    * spot_anchor is a model-based directional bet -> fractional Kelly on
      the model's win probability, like the sports value_edge layer.
    """
    pct_cap = bankroll_usd * (max_stake_per_trade_pct / 100.0)
    base_cap = min(pct_cap, max_stake_per_trade_usd)

    if opportunity.metadata.get("strategy") == "complement":
        stake = base_cap * max(0.25, opportunity.confidence)
        liquidity = opportunity.metadata.get("min_leg_liquidity_usd")
        if liquidity is not None:
            stake = min(stake, liquidity * 0.10)
        return max(0.0, stake)

    model_prob = opportunity.metadata.get("model_prob")
    side = opportunity.metadata.get("side")
    if model_prob is None or side not in ("yes", "no") or not opportunity.legs:
        return 0.0
    win_prob = model_prob if side == "yes" else 1.0 - model_prob
    price = opportunity.legs[0].get("price", 0.0)
    if price <= 0:
        return 0.0
    full_kelly = kelly_fraction(win_prob, 1.0 / price)
    stake = full_kelly * kelly_fraction_cap * max(0.1, opportunity.confidence) * bankroll_usd
    return max(0.0, min(stake, base_cap))


def size_signal(
    signal: GapSignal,
    bankroll_usd: float,
    max_stake_per_trade_pct: float,
    max_stake_per_trade_usd: float,
    kelly_fraction_cap: float = 0.15,
) -> float:
    if signal.signal_type == SignalType.ARBITRAGE:
        return size_arbitrage_signal(signal, bankroll_usd, max_stake_per_trade_pct, max_stake_per_trade_usd)
    return size_value_edge_signal(
        signal, bankroll_usd, kelly_fraction_cap, max_stake_per_trade_pct, max_stake_per_trade_usd
    )
