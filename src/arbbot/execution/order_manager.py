"""Layer 9: order orchestration.

Builds the concrete per-leg orders for a sized signal and submits them to
both venues CONCURRENTLY (not sequentially) so total execution latency is
bounded by the slower leg rather than the sum of both -- this is the main
"reaction speed" lever at the execution layer, since arbitrage margins can
disappear within seconds as other participants (human or bot) trade against
the same gap.
"""

from __future__ import annotations

import logging
import time

from arbbot.execution.base import ExecutionClient
from arbbot.execution.concurrent_submit import submit_orders_concurrently
from arbbot.models import GapSignal, Order, OrderStatus, Position, Side, SignalType, Venue

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(
        self,
        polymarket_client: ExecutionClient,
        sportsbook_client: ExecutionClient,
        order_timeout_sec: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        self._polymarket_client = polymarket_client
        self._sportsbook_client = sportsbook_client
        self._timeout = order_timeout_sec
        self._max_retries = max_retries

    def _build_orders(self, signal: GapSignal, total_stake_usd: float) -> list[Order]:
        orders: list[Order] = []

        if signal.signal_type == SignalType.VALUE_EDGE:
            poly_quote = signal.matched_market.polymarket_quote
            orders.append(
                Order(
                    venue=Venue.POLYMARKET,
                    side=Side.YES,
                    market_ref=poly_quote.token_id,
                    price=poly_quote.best_ask,
                    size_usd=total_stake_usd,
                    signal_id=signal.signal_id,
                )
            )
            return orders

        poly_quote = signal.matched_market.polymarket_quote
        for leg in signal.metadata.get("legs", []):
            stake = total_stake_usd * leg["stake_fraction"]
            if leg["venue"] == "polymarket":
                orders.append(
                    Order(
                        venue=Venue.POLYMARKET,
                        side=Side.YES,
                        market_ref=poly_quote.token_id,
                        price=poly_quote.best_ask,
                        size_usd=stake,
                        signal_id=signal.signal_id,
                    )
                )
            else:
                orders.append(
                    Order(
                        venue=Venue.SPORTSBOOK,
                        side=Side.BACK,
                        market_ref=f"{leg.get('bookmaker', 'unknown')}:{leg['outcome_name']}",
                        price=1.0 / leg["prob"],
                        size_usd=stake,
                        signal_id=signal.signal_id,
                    )
                )
        return orders

    def _client_for_order(self, order: Order) -> ExecutionClient:
        return self._polymarket_client if order.venue == Venue.POLYMARKET else self._sportsbook_client

    async def execute_signal(self, signal: GapSignal, total_stake_usd: float) -> Position:
        orders = self._build_orders(signal, total_stake_usd)
        start = time.perf_counter()

        results = await submit_orders_concurrently(orders, self._client_for_order, self._timeout, self._max_retries)

        latency_ms = (time.perf_counter() - start) * 1000.0
        logger.info("signal %s executed in %.1fms across %d leg(s)", signal.signal_id, latency_ms, len(results))

        position = Position(signal_id=signal.signal_id, orders=list(results))

        # Unlike a directional value-edge bet, a true arbitrage's profit is
        # mathematically locked in the instant every leg fills -- it does not
        # depend on which outcome the game actually produces. So we can
        # record it immediately rather than waiting for real-world
        # settlement (which value-edge trades still require).
        all_filled = all(o.status == OrderStatus.FILLED for o in results)
        if signal.signal_type == SignalType.ARBITRAGE and all_filled:
            total_implied_prob = signal.metadata.get("total_implied_prob")
            if total_implied_prob:
                position.guaranteed_profit_usd = total_stake_usd * (1.0 / total_implied_prob - 1.0)

        return position
