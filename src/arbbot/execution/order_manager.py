"""Layer 9: order orchestration.

Builds the concrete per-leg orders for a sized signal and submits them to
both venues CONCURRENTLY (not sequentially) so total execution latency is
bounded by the slower leg rather than the sum of both -- this is the main
"reaction speed" lever at the execution layer, since arbitrage margins can
disappear within seconds as other participants (human or bot) trade against
the same gap.
"""

from __future__ import annotations

import asyncio
import logging
import time

from arbbot.execution.base import ExecutionClient
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

    async def _submit_with_retry(self, client: ExecutionClient, order: Order) -> Order:
        attempt = 0
        while True:
            try:
                return await asyncio.wait_for(client.submit(order), timeout=self._timeout)
            except Exception as exc:  # broad by design: this is the external-API boundary
                attempt += 1
                if attempt > self._max_retries:
                    logger.error("order %s failed after %d attempts: %s", order.order_id, attempt, exc)
                    order.status = OrderStatus.REJECTED
                    return order
                logger.warning("order %s attempt %d failed, retrying: %s", order.order_id, attempt, exc)

    async def execute_signal(self, signal: GapSignal, total_stake_usd: float) -> Position:
        orders = self._build_orders(signal, total_stake_usd)
        start = time.perf_counter()

        clients = [
            self._polymarket_client if o.venue == Venue.POLYMARKET else self._sportsbook_client for o in orders
        ]
        results = await asyncio.gather(*(self._submit_with_retry(c, o) for c, o in zip(clients, orders)))

        latency_ms = (time.perf_counter() - start) * 1000.0
        logger.info("signal %s executed in %.1fms across %d leg(s)", signal.signal_id, latency_ms, len(results))

        return Position(signal_id=signal.signal_id, orders=list(results))
