"""Shared concurrent order submission with retry/timeout.

Used by every market's order manager (sports, crypto, ...): submitting all
legs of a multi-venue trade at once bounds total execution latency by the
slowest leg rather than the sum of all legs, which matters because gaps can
close within seconds of being detected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from arbbot.execution.base import ExecutionClient
from arbbot.models import Order, OrderStatus

logger = logging.getLogger(__name__)


async def submit_with_retry(
    client: ExecutionClient, order: Order, timeout_sec: float, max_retries: int
) -> Order:
    attempt = 0
    while True:
        try:
            return await asyncio.wait_for(client.submit(order), timeout=timeout_sec)
        except Exception as exc:  # broad by design: this is the external-API boundary
            attempt += 1
            if attempt > max_retries:
                logger.error("order %s failed after %d attempts: %s", order.order_id, attempt, exc)
                order.status = OrderStatus.REJECTED
                return order
            logger.warning("order %s attempt %d failed, retrying: %s", order.order_id, attempt, exc)


async def submit_orders_concurrently(
    orders: list[Order],
    client_for_order: Callable[[Order], ExecutionClient],
    timeout_sec: float,
    max_retries: int,
) -> list[Order]:
    return await asyncio.gather(
        *(submit_with_retry(client_for_order(o), o, timeout_sec, max_retries) for o in orders)
    )
