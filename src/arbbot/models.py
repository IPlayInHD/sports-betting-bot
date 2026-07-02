"""Core domain models shared across all layers of the bot."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


def now_ts() -> float:
    return time.time()


class Side(str, Enum):
    BACK = "back"          # take the sportsbook price on an outcome
    YES = "yes"             # buy YES shares on Polymarket
    NO = "no"               # buy NO shares on Polymarket


class Venue(str, Enum):
    SPORTSBOOK = "sportsbook"
    POLYMARKET = "polymarket"


class MarketFamily(str, Enum):
    """Which asset class / strategy family a trade belongs to. Sports keeps
    its own richer GapSignal/MatchedMarket shape; every other family
    produces the more generic MarketOpportunity below.
    """

    SPORTS = "sports"
    CRYPTO = "crypto"
    POLYCRYPTO = "polycrypto"   # Polymarket crypto prediction markets vs. spot exchanges
    FOREX = "forex"
    EQUITIES = "equities"
    DERIVATIVES = "derivatives"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SignalType(str, Enum):
    ARBITRAGE = "arbitrage"       # riskless cross-market gap (Layer 4)
    VALUE_EDGE = "value_edge"     # statistical consensus edge (Layer 5)


@dataclass(slots=True)
class SportsbookQuote:
    """A single outcome price from a single bookmaker for a single event."""

    bookmaker: str
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: float
    outcome_name: str          # e.g. "Los Angeles Lakers"
    decimal_odds: float
    observed_at: float = field(default_factory=now_ts)

    @property
    def implied_prob(self) -> float:
        return 1.0 / self.decimal_odds


@dataclass(slots=True)
class PolymarketQuote:
    """Best bid/ask snapshot for one outcome token of a Polymarket market."""

    market_id: str             # condition_id
    token_id: str               # CLOB token id for this specific outcome
    question: str
    outcome_name: str           # e.g. "Lakers" for a "Will the Lakers win?" market
    best_bid: float              # highest price a buyer will pay for this outcome (0-1)
    best_ask: float              # lowest price a seller will accept for this outcome (0-1)
    liquidity_usd: float
    end_date: float
    observed_at: float = field(default_factory=now_ts)

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_pct(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return (self.best_ask - self.best_bid) / self.mid * 100.0


@dataclass(slots=True)
class MatchedMarket:
    """A sportsbook event outcome paired with its equivalent Polymarket token."""

    match_id: str
    sportsbook_quotes: list[SportsbookQuote]   # one per bookmaker for this outcome
    polymarket_quote: PolymarketQuote
    match_score: float                          # fuzzy-match confidence 0-1

    @property
    def best_sportsbook_quote(self) -> SportsbookQuote:
        return max(self.sportsbook_quotes, key=lambda q: q.decimal_odds)


@dataclass(slots=True)
class GapSignal:
    """A detected pricing gap ready to be filtered / sized / executed."""

    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_type: SignalType = SignalType.ARBITRAGE
    matched_market: MatchedMarket | None = None
    edge_pct: float = 0.0
    confidence: float = 0.0
    sportsbook_stake_fraction: float = 0.0
    polymarket_stake_fraction: float = 0.0
    polymarket_side: Side = Side.NO
    created_at: float = field(default_factory=now_ts)
    metadata: dict = field(default_factory=dict)

    @property
    def age_sec(self) -> float:
        return now_ts() - self.created_at


@dataclass(slots=True)
class CryptoQuote:
    """Best bid/ask for one symbol on one exchange."""

    exchange: str
    symbol: str            # normalized form, e.g. "BTC/USD"
    bid: float
    ask: float
    observed_at: float = field(default_factory=now_ts)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(slots=True)
class MarketOpportunity:
    """Generalized signal shape for non-sports asset classes (crypto, forex,
    equities, derivatives). Each leg is a plain dict:
    {"venue": str, "action": "buy"|"sell", "symbol": str, "price": float,
    "stake_fraction": float}. Kept intentionally looser than GapSignal since
    these strategies' leg shapes vary more (2-venue buy/sell, N-pair
    triangular, single-instrument basis, etc.) than sports' consistent
    "outcome vs outcome" structure.
    """

    opportunity_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    family: MarketFamily = MarketFamily.CRYPTO
    symbol: str = ""
    edge_pct: float = 0.0
    confidence: float = 1.0
    legs: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=now_ts)
    metadata: dict = field(default_factory=dict)

    @property
    def age_sec(self) -> float:
        return now_ts() - self.created_at


@dataclass(slots=True)
class Order:
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    venue: Venue = Venue.POLYMARKET
    side: Side = Side.YES
    market_ref: str = ""       # token_id or bookmaker+event_id
    price: float = 0.0
    size_usd: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_price: float | None = None
    filled_size_usd: float | None = None
    submitted_at: float | None = None
    resolved_at: float | None = None
    signal_id: str | None = None


@dataclass(slots=True)
class Position:
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str = ""
    orders: list[Order] = field(default_factory=list)
    opened_at: float = field(default_factory=now_ts)
    closed_at: float | None = None
    realized_pnl_usd: float | None = None
    guaranteed_profit_usd: float | None = None

    @property
    def total_staked_usd(self) -> float:
        return sum(o.filled_size_usd or 0.0 for o in self.orders)
