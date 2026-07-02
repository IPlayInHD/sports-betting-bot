"""Execution for the Polymarket-crypto family.

Both legs (or the single leg, for spot-anchor signals) live on Polymarket,
so this reuses the exact same execution clients as the sports pipeline's
Polymarket leg: PaperExecutionClient in paper mode, PolymarketLiveExecutionClient
(py-clob-client) behind the dual live opt-in. What differs is order
construction and settlement math:

  * complement: buy Q shares of BOTH outcomes; every share pair pays exactly
    $1 at resolution, so the profit Q - total_cost is locked in at fill time
    and recorded immediately, like crypto cross-exchange arb.
  * spot_anchor: buy one side only; nothing is guaranteed until the market
    resolves, so guaranteed_profit_usd stays None (the trade shows as
    unsettled in the dashboard until resolution).
"""

from __future__ import annotations

from arbbot.execution.base import ExecutionClient
from arbbot.execution.concurrent_submit import submit_orders_concurrently
from arbbot.models import MarketOpportunity, Order, OrderStatus, Position, Side, Venue


class PolycryptoOrderManager:
    def __init__(self, client: ExecutionClient, order_timeout_sec: float = 5.0, max_retries: int = 2) -> None:
        self._client = client
        self._timeout = order_timeout_sec
        self._max_retries = max_retries

    @staticmethod
    def _build_orders(opportunity: MarketOpportunity, total_stake_usd: float) -> tuple[list[Order], float]:
        """Returns (orders, shares). For complement trades both legs must hold
        the same SHARE count (each YES+NO pair pays $1), so the stake is
        split proportionally to each leg's price rather than equally.
        """
        combined_price = sum(leg["price"] for leg in opportunity.legs)
        if combined_price <= 0:
            return [], 0.0
        shares = total_stake_usd / combined_price

        orders = []
        for leg in opportunity.legs:
            side = Side.YES if leg["symbol"].strip().lower() == "yes" else Side.NO
            orders.append(
                Order(
                    venue=Venue.POLYMARKET,
                    side=side,
                    market_ref=leg["token_id"],
                    price=leg["price"],
                    size_usd=shares * leg["price"],
                    signal_id=opportunity.opportunity_id,
                )
            )
        return orders, shares

    async def execute_opportunity(self, opportunity: MarketOpportunity, total_stake_usd: float) -> Position:
        orders, shares = self._build_orders(opportunity, total_stake_usd)

        results = await submit_orders_concurrently(orders, lambda _: self._client, self._timeout, self._max_retries)

        position = Position(signal_id=opportunity.opportunity_id, orders=list(results))
        is_complement = opportunity.metadata.get("strategy") == "complement"
        if is_complement and results and all(o.status == OrderStatus.FILLED for o in results):
            # Each of the `shares` YES+NO pairs pays $1 at resolution no
            # matter the outcome; cost uses actual filled prices so paper
            # slippage flows through to recorded P&L.
            total_cost = sum(shares * (o.filled_price if o.filled_price is not None else o.price) for o in results)
            position.guaranteed_profit_usd = shares * 1.0 - total_cost
        return position
