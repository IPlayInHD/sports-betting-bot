"""Layer 9: execution engine abstraction.

Two concrete implementations exist:
  * PaperExecutionClient (execution/paper.py) -- simulates fills for both
    venues. This is the default in every mode except "live".
  * Live: the Polymarket leg is submitted for real via
    polymarket/clob_execution.py; the sportsbook leg is NOT auto-submitted
    (see ManualAlertSportsbookExecutionClient below), because most retail
    sportsbooks' terms of service explicitly prohibit automated / bot
    wagering and arbitrage betting, and can result in account limitation or
    closure. If you trade through a licensed betting exchange with a public
    order API (e.g. Betfair Exchange), you can implement a real automated
    adapter here following this same interface -- confirm that venue's terms
    permit it first.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from arbbot.models import Order, OrderStatus


class ExecutionClient(ABC):
    venue_name: str = "base"

    @abstractmethod
    async def submit(self, order: Order) -> Order:
        raise NotImplementedError

    async def cancel(self, order: Order) -> bool:
        return False


class ManualAlertSportsbookExecutionClient(ExecutionClient):
    """Does not place real sportsbook bets. Surfaces the required leg to a
    human (via the supplied alert callback, e.g. a Slack/Telegram webhook) to
    place by hand, fast, instead of silently auto-submitting it.
    """

    venue_name = "sportsbook_manual"

    def __init__(self, alert_fn) -> None:
        self._alert_fn = alert_fn

    async def submit(self, order: Order) -> Order:
        order.submitted_at = time.time()
        order.status = OrderStatus.PENDING
        await self._alert_fn(order)
        return order
