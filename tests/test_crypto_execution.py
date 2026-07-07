from __future__ import annotations

import pytest

from arbbot.markets.crypto.execution import CryptoOrderManager, PaperCryptoExecutionClient
from arbbot.models import MarketFamily, MarketOpportunity


def _opportunity(buy_price: float = 100.0, sell_price: float = 101.0) -> MarketOpportunity:
    return MarketOpportunity(
        family=MarketFamily.CRYPTO,
        symbol="BTC/USD",
        edge_pct=0.9,
        confidence=1.0,
        legs=[
            {"venue": "coinbase", "action": "buy", "symbol": "BTC/USD", "price": buy_price},
            {"venue": "kraken", "action": "sell", "symbol": "BTC/USD", "price": sell_price},
        ],
        metadata={"buy_exchange": "coinbase", "sell_exchange": "kraken"},
    )


def test_build_orders_uses_matched_quantity_not_equal_dollars():
    manager = CryptoOrderManager(client_for_exchange={})
    orders = manager._build_orders(_opportunity(buy_price=100.0, sell_price=101.0), total_stake_usd=1000.0)

    buy_order = next(o for o in orders if o.side == "buy")
    sell_order = next(o for o in orders if o.side == "sell")

    # quantity = 1000 / 100 = 10 units on both legs
    assert buy_order.size_usd == pytest.approx(1000.0)
    assert sell_order.size_usd == pytest.approx(10 * 101.0)


async def test_execute_opportunity_computes_guaranteed_profit():
    paper = PaperCryptoExecutionClient(slippage_bps=0.0)  # zero slippage for a deterministic assertion
    manager = CryptoOrderManager(client_for_exchange={"coinbase": paper, "kraken": paper})

    position = await manager.execute_opportunity(_opportunity(buy_price=100.0, sell_price=101.0), total_stake_usd=1000.0)

    # quantity = 10; buy_cost = 1000; sell_proceeds = 10 * 101 = 1010; profit = 10
    assert position.guaranteed_profit_usd == pytest.approx(10.0)


async def test_slippage_reduces_recorded_profit():
    # With adverse slippage the buy fills higher and the sell lower, so the
    # honest recorded profit must come in BELOW the zero-slippage $10 -- paper
    # P&L should not flatter itself by ignoring fills.
    paper = PaperCryptoExecutionClient(slippage_bps=50.0)
    manager = CryptoOrderManager(client_for_exchange={"coinbase": paper, "kraken": paper})

    position = await manager.execute_opportunity(_opportunity(buy_price=100.0, sell_price=101.0), total_stake_usd=1000.0)

    assert position.guaranteed_profit_usd is not None
    assert position.guaranteed_profit_usd < 10.0


async def test_execute_opportunity_no_profit_recorded_if_a_leg_is_rejected():
    class RejectingClient(PaperCryptoExecutionClient):
        async def submit(self, order):
            from arbbot.models import OrderStatus

            order.status = OrderStatus.REJECTED
            return order

    manager = CryptoOrderManager(client_for_exchange={"coinbase": RejectingClient(), "kraken": PaperCryptoExecutionClient()})
    position = await manager.execute_opportunity(_opportunity(), total_stake_usd=1000.0)

    assert position.guaranteed_profit_usd is None
