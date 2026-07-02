"""Probability model for Polymarket crypto threshold markets, anchored to
live spot prices.

This is the same first-order model desks use to sanity-check binary/digital
option quotes: assume the spot price follows a zero-drift lognormal
(geometric Brownian motion under the martingale measure), so that

    P(S_T > K) = Phi( (ln(S/K) - sigma^2 T / 2) / (sigma sqrt(T)) )

with S the current spot, K the threshold, T the time to expiry in years and
sigma an assumed annualized volatility per asset (config-supplied -- crypto
realized vol is regime-dependent, which is exactly why the detector demands
a LARGE gap between this model and Polymarket's price plus a high confidence
score before acting, rather than trading every small disagreement).

Touch ("hit $X by <date>") markets use the reflection principle for driftless
Brownian motion: the probability of touching a barrier before expiry is
(approximately) twice the probability of finishing beyond it. Both are
textbook results, not proprietary edge -- the edge, when it exists, comes
from Polymarket quotes drifting away from any reasonable model faster than
its market makers re-price, which happens most visibly right after sharp
spot moves.
"""

from __future__ import annotations

import math

from arbbot.markets.polycrypto.parser import ThresholdKind, ThresholdMarket

SECONDS_PER_YEAR = 365.25 * 24 * 3600.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def terminal_above_probability(spot: float, threshold: float, t_years: float, annualized_vol: float) -> float:
    """P(S_T > threshold) under zero-drift lognormal dynamics."""
    if spot <= 0 or threshold <= 0:
        return 0.0
    if t_years <= 0:
        return 1.0 if spot > threshold else 0.0
    sigma_rt = annualized_vol * math.sqrt(t_years)
    if sigma_rt <= 0:
        return 1.0 if spot > threshold else 0.0
    d = (math.log(spot / threshold) - 0.5 * sigma_rt * sigma_rt) / sigma_rt
    return normal_cdf(d)


def touch_probability(spot: float, threshold: float, t_years: float, annualized_vol: float) -> float:
    """P(S touches threshold at any point before T). If spot is already past
    the barrier the market has effectively resolved YES. Otherwise apply the
    reflection principle: P(touch) ~= 2 * P(finish beyond barrier).
    """
    if spot <= 0 or threshold <= 0:
        return 0.0
    if spot == threshold:
        return 1.0
    if threshold > spot:  # upside barrier
        p_beyond = terminal_above_probability(spot, threshold, t_years, annualized_vol)
    else:  # downside barrier
        p_beyond = 1.0 - terminal_above_probability(spot, threshold, t_years, annualized_vol)
    return min(1.0, 2.0 * p_beyond)


def model_yes_probability(market: ThresholdMarket, spot: float, t_years: float, annualized_vol: float) -> float:
    """Model probability that the market's YES outcome resolves true."""
    if market.kind == ThresholdKind.TERMINAL_ABOVE:
        return terminal_above_probability(spot, market.threshold_usd, t_years, annualized_vol)
    if market.kind == ThresholdKind.TERMINAL_BELOW:
        return 1.0 - terminal_above_probability(spot, market.threshold_usd, t_years, annualized_vol)
    return touch_probability(spot, market.threshold_usd, t_years, annualized_vol)


def distance_in_sigmas(spot: float, threshold: float, t_years: float, annualized_vol: float) -> float:
    """How many volatility-standard-deviations away the threshold sits from
    spot over the remaining horizon. Used by confidence scoring: a model
    probability of ~0.99 backed by a 3-sigma distance is far more trustworthy
    than the same 0.99 squeezed out of a near-the-money strike.
    """
    if spot <= 0 or threshold <= 0 or t_years <= 0 or annualized_vol <= 0:
        return 0.0
    return abs(math.log(spot / threshold)) / (annualized_vol * math.sqrt(t_years))
