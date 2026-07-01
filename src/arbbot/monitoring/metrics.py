"""Layer 10: post-trade monitoring / feedback loop.

Tracks realized win rate, P&L, and per-stage latency so the operator can see
whether the bot is behaving as intended, and so `historical_fill_rate` can be
fed back into the confidence scorer (Layer 7) to close the loop over time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class TradeOutcome:
    signal_id: str
    stake_usd: float
    pnl_usd: float
    won: bool
    latency_ms: float


class MetricsTracker:
    def __init__(self, window_size: int = 200) -> None:
        self._outcomes: deque[TradeOutcome] = deque(maxlen=window_size)
        self._latencies_ms: deque[float] = deque(maxlen=window_size)
        self.total_trades: int = 0
        self.total_pnl_usd: float = 0.0

    def record_latency(self, latency_ms: float) -> None:
        self._latencies_ms.append(latency_ms)

    def record_outcome(self, outcome: TradeOutcome) -> None:
        self._outcomes.append(outcome)
        self.total_trades += 1
        self.total_pnl_usd += outcome.pnl_usd

    @property
    def win_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        wins = sum(1 for o in self._outcomes if o.won)
        return wins / len(self._outcomes)

    @property
    def fill_rate(self) -> float:
        # Placeholder hook: in a full deployment this would divide filled
        # orders by attempted orders per venue. Kept at 1.0 until wired to
        # real per-order fill telemetry from the execution layer.
        return 1.0

    def latency_percentile(self, pct: float) -> float:
        if not self._latencies_ms:
            return 0.0
        data = sorted(self._latencies_ms)
        idx = min(len(data) - 1, int(len(data) * pct / 100.0))
        return data[idx]

    def summary(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "win_rate": round(self.win_rate, 4),
            "p50_latency_ms": round(self.latency_percentile(50), 1),
            "p95_latency_ms": round(self.latency_percentile(95), 1),
            "p99_latency_ms": round(self.latency_percentile(99), 1),
        }
