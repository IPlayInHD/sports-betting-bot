"""Simulated execution client used for paper trading, backtests, and tests.

Fills are instantaneous at the signal's quoted price, adjusted by a
configurable slippage allowance in basis points in the adverse direction (you
pay slightly more / receive slightly worse odds than the observed quote) --
this keeps paper-trading P&L realistic rather than flattering.
"""

from __future__ import annotations

import time

from arbbot.execution.base import ExecutionClient
from arbbot.models import Order, OrderStatus, Side


class PaperExecutionClient(ExecutionClient):
    venue_name = "paper"

    def __init__(self, slippage_bps: float = 15.0) -> None:
        self._slippage = slippage_bps / 10_000.0

    async def submit(self, order: Order) -> Order:
        order.submitted_at = time.time()
        if order.side == Side.BACK:
            # Decimal odds shrinking slightly is adverse to the bettor.
            fill_price = order.price * (1.0 - self._slippage)
        else:
            # Polymarket share price rising slightly is adverse to a buyer.
            fill_price = min(order.price * (1.0 + self._slippage), 0.9999)
        order.filled_price = round(fill_price, 4)
        order.filled_size_usd = order.size_usd
        order.status = OrderStatus.FILLED
        order.resolved_at = time.time()
        return order

    async def cancel(self, order: Order) -> bool:
        if order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            order.status = OrderStatus.CANCELLED
            return True
        return False
