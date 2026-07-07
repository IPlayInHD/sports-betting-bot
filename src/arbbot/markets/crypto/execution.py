"""Crypto execution: paper (default) and live (optional, via ccxt).

Mirrors the sports execution split: paper trading simulates fills, live
trading requires the same dual opt-in (config mode: live + env var) plus
per-exchange API credentials, and delegates all exchange-specific signing to
the well-established `ccxt` library rather than hand-rolling each
exchange's auth scheme.
"""

from __future__ import annotations

import time

from arbbot.execution.base import ExecutionClient
from arbbot.execution.concurrent_submit import submit_orders_concurrently
from arbbot.models import MarketOpportunity, Order, OrderStatus, Position


class PaperCryptoExecutionClient(ExecutionClient):
    venue_name = "paper_crypto"

    def __init__(self, slippage_bps: float = 10.0) -> None:
        self._slippage = slippage_bps / 10_000.0

    async def submit(self, order: Order) -> Order:
        order.submitted_at = time.time()
        # order.side holds the plain string "buy"/"sell" (see CryptoOrderManager);
        # buying at a slightly higher price / selling at a slightly lower one
        # is the adverse (realistic) direction for slippage.
        if order.side == "buy":
            fill_price = order.price * (1.0 + self._slippage)
        else:
            fill_price = order.price * (1.0 - self._slippage)
        order.filled_price = round(fill_price, 8)
        order.filled_size_usd = order.size_usd
        order.status = OrderStatus.FILLED
        order.resolved_at = time.time()
        return order

    async def cancel(self, order: Order) -> bool:
        if order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            order.status = OrderStatus.CANCELLED
            return True
        return False


class CcxtLiveExecutionClient(ExecutionClient):
    """Places real market/limit orders on one specific exchange via ccxt.

    One instance per exchange (construct one per configured exchange, keyed
    by exchange id, the same way Polymarket's live client is one-per-venue).
    Requires `pip install arbbot[live-crypto]`.
    """

    def __init__(self, exchange_id: str, api_key: str, api_secret: str, confirmed: bool) -> None:
        if not confirmed:
            raise RuntimeError(
                "Refusing to initialize live crypto execution without explicit "
                "confirmation. Set mode: live in config.yaml AND "
                "I_UNDERSTAND_THE_RISKS=true in your environment."
            )
        if not api_key or not api_secret:
            raise ValueError(f"Missing API credentials for exchange '{exchange_id}'")

        try:
            import ccxt.async_support as ccxt
        except ImportError as e:
            raise ImportError(
                "ccxt is required for live crypto trading. Install with: pip install '.[live-crypto]'"
            ) from e

        self.venue_name = exchange_id
        exchange_class = getattr(ccxt, exchange_id)
        self._exchange = exchange_class({"apiKey": api_key, "secret": api_secret})

    async def submit(self, order: Order) -> Order:
        order.submitted_at = time.time()
        try:
            base, quote = order.market_ref.split("/")
            amount = order.size_usd / order.price if order.price > 0 else 0.0
            result = await self._exchange.create_order(
                symbol=f"{base}/{quote}", type="market", side=order.side, amount=amount
            )
            order.filled_price = float(result.get("average") or order.price)
            order.filled_size_usd = order.size_usd
            order.status = OrderStatus.FILLED
        except Exception:  # noqa: BLE001 - external exchange API boundary
            order.status = OrderStatus.REJECTED
            raise
        finally:
            order.resolved_at = time.time()
        return order

    async def close(self) -> None:
        await self._exchange.close()


class CryptoOrderManager:
    def __init__(
        self,
        client_for_exchange: dict[str, ExecutionClient],
        order_timeout_sec: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        self._client_for_exchange = client_for_exchange
        self._timeout = order_timeout_sec
        self._max_retries = max_retries

    def _build_orders(self, opportunity: MarketOpportunity, total_stake_usd: float) -> list[Order]:
        # Both legs must trade the same underlying QUANTITY (you're buying Q
        # coins on the cheap exchange and selling Q coins you already hold on
        # the expensive one) -- not an equal dollar split, since the two legs
        # trade at different prices by construction.
        buy_leg = next(leg for leg in opportunity.legs if leg["action"] == "buy")
        quantity = total_stake_usd / buy_leg["price"] if buy_leg["price"] > 0 else 0.0

        orders = []
        for leg in opportunity.legs:
            orders.append(
                Order(
                    venue=leg["venue"],  # plain exchange-name string, not the sports Venue enum
                    side=leg["action"],  # plain "buy"/"sell" string
                    market_ref=leg["symbol"],
                    price=leg["price"],
                    size_usd=quantity * leg["price"],
                    signal_id=opportunity.opportunity_id,
                )
            )
        return orders

    async def execute_opportunity(self, opportunity: MarketOpportunity, total_stake_usd: float) -> Position:
        orders = self._build_orders(opportunity, total_stake_usd)

        def client_for_order(order: Order) -> ExecutionClient:
            return self._client_for_exchange[order.venue]

        results = await submit_orders_concurrently(orders, client_for_order, self._timeout, self._max_retries)

        position = Position(signal_id=opportunity.opportunity_id, orders=list(results))
        if all(o.status == OrderStatus.FILLED for o in results):
            # Profit at ACTUAL filled prices, not quoted sizes, so paper
            # slippage (buy fills a touch higher, sell a touch lower) flows
            # into recorded P&L instead of flattering it -- the same
            # convention the polycrypto complement layer uses. quantity is
            # recovered from the order's quoted size/price.
            def _leg_value(o: Order) -> float:
                quantity = o.size_usd / o.price if o.price > 0 else 0.0
                fill = o.filled_price if o.filled_price is not None else o.price
                return quantity * fill

            buy_cost = sum(_leg_value(o) for o in results if o.side == "buy")
            sell_proceeds = sum(_leg_value(o) for o in results if o.side == "sell")
            position.guaranteed_profit_usd = sell_proceeds - buy_cost
        return position
