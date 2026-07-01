"""Odds conversion and de-vig math (Layer 2).

Sportsbook prices always contain a bookmaker margin ("vig"). Removing it gives
a better estimate of the *fair* (no-vig) probability, which the value-edge
layer needs. Pure arbitrage detection (Layer 4) intentionally does NOT use
de-vigged probabilities -- it trades on the raw quoted price, since that is
what you actually transact at.
"""

from __future__ import annotations


def american_to_decimal(american: float) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    if american < 0:
        return 1.0 + 100.0 / abs(american)
    raise ValueError("American odds cannot be 0")


def decimal_to_american(decimal: float) -> float:
    if decimal <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    if decimal >= 2.0:
        return (decimal - 1.0) * 100.0
    return -100.0 / (decimal - 1.0)


def decimal_to_implied_prob(decimal: float) -> float:
    if decimal <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    return 1.0 / decimal


def implied_prob_to_decimal(prob: float) -> float:
    if not 0.0 < prob < 1.0:
        raise ValueError("Probability must be in (0, 1)")
    return 1.0 / prob


def devig_multiplicative(raw_probs: list[float]) -> list[float]:
    """Simplest de-vig: scale all implied probabilities down proportionally so
    they sum to 1.0. Fast and robust; slightly biased toward favorites vs.
    more sophisticated methods (e.g. Shin's method), but adequate for
    real-time filtering.
    """
    total = sum(raw_probs)
    if total <= 0:
        raise ValueError("Sum of probabilities must be positive")
    return [p / total for p in raw_probs]


def devig_power(raw_probs: list[float], tol: float = 1e-9, max_iter: int = 100) -> list[float]:
    """Power-method de-vig: find exponent k such that sum(p_i^k) == 1.

    This tends to preserve the *relative* skew between favorites and
    underdogs better than the multiplicative method, at the cost of a small
    iterative solve (binary search on k).
    """
    total = sum(raw_probs)
    if total <= 1.0:
        # No vig to remove (or already fair/underround) -- fall back cleanly.
        return devig_multiplicative(raw_probs)

    lo, hi = 0.01, 10.0
    for _ in range(max_iter):
        k = (lo + hi) / 2.0
        s = sum(p**k for p in raw_probs)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    return [p**k for p in raw_probs]


def overround_pct(raw_probs: list[float]) -> float:
    """The bookmaker's margin, e.g. 0.05 -> 5% overround."""
    return (sum(raw_probs) - 1.0) * 100.0
