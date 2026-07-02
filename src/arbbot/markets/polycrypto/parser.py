"""Parse Polymarket crypto question text into a structured threshold market.

Polymarket's crypto markets are mostly binary questions of the form
"Will Bitcoin be above $70,000 on July 31?" or "Will Ethereum reach $5,000
by December 31?". To price one against live spot exchange data (Layer 4/5
for this family) we need the underlying symbol, the strike/threshold price,
and whether the question is about the *terminal* price ("be above X on
date") or about *touching* a level at any point before expiry ("hit/reach
X by date") -- the two have very different probabilities for the same
strike, so conflating them would systematically misprice touch markets.

Anything that doesn't parse cleanly is skipped rather than guessed at:
unparseable markets can still be traded by the complement-arbitrage
detector, which needs no price model at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ThresholdKind(str, Enum):
    TERMINAL_ABOVE = "terminal_above"   # "be above $X on <date>"
    TERMINAL_BELOW = "terminal_below"   # "be below $X on <date>"
    TOUCH = "touch"                     # "hit/reach $X by <date>" (any time before expiry)


@dataclass(slots=True)
class ThresholdMarket:
    symbol: str            # normalized spot symbol, e.g. "BTC/USD"
    threshold_usd: float
    kind: ThresholdKind


# Common names Polymarket uses for the majors this bot watches on the spot side.
_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC/USD",
    "btc": "BTC/USD",
    "ethereum": "ETH/USD",
    "ether": "ETH/USD",
    "eth": "ETH/USD",
    "solana": "SOL/USD",
    "sol": "SOL/USD",
    "dogecoin": "DOGE/USD",
    "doge": "DOGE/USD",
    "litecoin": "LTC/USD",
    "ltc": "LTC/USD",
    "xrp": "XRP/USD",
}

_ASSET_RE = re.compile(
    r"\b(" + "|".join(sorted(_ASSET_ALIASES, key=len, reverse=True)) + r")\b", re.IGNORECASE
)

# "$70,000", "$70000.50", "$70k", "$1.5k" -- Polymarket sticks to USD strikes.
_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k)?", re.IGNORECASE)

# Touch phrasing covers both directions ("hit $100k", "dip to $50k"); which
# side the barrier sits on is decided at pricing time from spot vs threshold.
_TOUCH_RE = re.compile(r"\b(hit|reach|touch|cross)\b|\b(dip|drop|fall)\s+to\b", re.IGNORECASE)
_ABOVE_RE = re.compile(r"\b(above|over|higher than|greater than|at least|exceed)\b", re.IGNORECASE)
_BELOW_RE = re.compile(r"\b(below|under|lower than|less than)\b", re.IGNORECASE)


def parse_threshold_question(question: str) -> ThresholdMarket | None:
    """Return the structured market, or None when the question isn't a simple
    single-asset USD threshold (e.g. ratio markets, ETF-flow questions, or
    assets we have no spot feed for).
    """
    asset_match = _ASSET_RE.search(question)
    if asset_match is None:
        return None
    symbol = _ASSET_ALIASES[asset_match.group(1).lower()]

    price_match = _PRICE_RE.search(question)
    if price_match is None:
        return None
    threshold = float(price_match.group(1).replace(",", ""))
    if price_match.group(2):  # "k" suffix
        threshold *= 1_000.0
    if threshold <= 0:
        return None

    # Classify touch before above/below since "reach above $X" style phrasing
    # is still a touch payoff.
    if _TOUCH_RE.search(question):
        return ThresholdMarket(symbol=symbol, threshold_usd=threshold, kind=ThresholdKind.TOUCH)
    if _BELOW_RE.search(question):
        return ThresholdMarket(symbol=symbol, threshold_usd=threshold, kind=ThresholdKind.TERMINAL_BELOW)
    if _ABOVE_RE.search(question):
        return ThresholdMarket(symbol=symbol, threshold_usd=threshold, kind=ThresholdKind.TERMINAL_ABOVE)
    return None
