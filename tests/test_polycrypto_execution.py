from __future__ import annotations

import pytest

from arbbot.execution.paper import PaperExecutionClient
from arbbot.markets.polycrypto.execution import PolycryptoOrderManager
from arbbot.models import MarketFamily, MarketOpportunity, OrderStatus
from arbbot.risk.position_sizing import size_polycrypto_opportunity


def make_complement_opportunity(yes_ask: float = 0.48, no_ask: float = 0.49) -> MarketOpportunity:
    return MarketOpportunity(
        family=MarketFamily.POLYCRYPTO,
        symbol="Will Bitcoin be above $70,000?",
        edge_pct=2.9,
        confidence=0.9,
        legs=[
            {"venue": "polymarket", "action": "buy", "symbol": "Yes", "token_id": "t:yes", "price": yes_ask},
            {"venue": "polymarket", "action": "buy", "symbol": "No", "token_id": "t:no", "price": no_ask},
        ],
        metadata={"strategy": "complement", "market_id": "mkt-1", "min_leg_liquidity_usd": 2000.0},
    )


def make_spot_anchor_opportunity() -> MarketOpportunity:
    return MarketOpportunity(
        family=MarketFamily.POLYCRYPTO,
        symbol="BTC/USD",
        edge_pct=15.0,
        confidence=0.8,
        legs=[{"venue": "polymarket", "action": "buy", "symbol": "Yes", "token_id": "t:yes", "price": 0.60}],
        metadata={"strategy": "spot_anchor", "market_id": "mkt-1", "model_prob": 0.85, "side": "yes"},
    )


@pytest.mark.asyncio
async def test_complement_locks_in_profit_at_fill_time():
    manager = PolycryptoOrderManager(client=PaperExecutionClient(slippage_bps=0.0))
    position = await manager.execute_opportunity(make_complement_opportunity(), total_stake_usd=9.7)

    assert all(o.status == OrderStatus.FILLED for o in position.orders)
    # $9.70 buys 10 YES+NO pairs at 0.97 combined; each pair pays $1.
    assert position.guaranteed_profit_usd == pytest.approx(10 * (1 - 0.97), abs=1e-9)


@pytest.mark.asyncio
async def test_complement_profit_reflects_paper_slippage():
    manager = PolycryptoOrderManager(client=PaperExecutionClient(slippage_bps=50.0))
    position = await manager.execute_opportunity(make_complement_opportunity(), total_stake_usd=9.7)
    assert position.guaranteed_profit_usd is not None
    assert position.guaranteed_profit_usd < 10 * (1 - 0.97)  # adverse fills eat into the edge


@pytest.mark.asyncio
async def test_complement_legs_hold_equal_share_counts():
    manager = PolycryptoOrderManager(client=PaperExecutionClient(slippage_bps=0.0))
    position = await manager.execute_opportunity(make_complement_opportunity(), total_stake_usd=9.7)
    shares = [o.size_usd / o.price for o in position.orders]
    assert shares[0] == pytest.approx(shares[1])


@pytest.mark.asyncio
async def test_spot_anchor_position_stays_open():
    manager = PolycryptoOrderManager(client=PaperExecutionClient(slippage_bps=0.0))
    position = await manager.execute_opportunity(make_spot_anchor_opportunity(), total_stake_usd=2.0)
    assert len(position.orders) == 1
    assert position.orders[0].status == OrderStatus.FILLED
    assert position.guaranteed_profit_usd is None  # settles at market resolution, not fill


class TestPolycryptoSizing:
    def test_complement_uses_caps_scaled_by_confidence(self):
        opp = make_complement_opportunity()
        stake = size_polycrypto_opportunity(
            opp, bankroll_usd=50.0, max_stake_per_trade_pct=4.0, max_stake_per_trade_usd=25.0
        )
        assert stake == pytest.approx(50.0 * 0.04 * 0.9)

    def test_complement_capped_by_book_depth(self):
        opp = make_complement_opportunity()
        opp.metadata["min_leg_liquidity_usd"] = 10.0
        stake = size_polycrypto_opportunity(
            opp, bankroll_usd=50.0, max_stake_per_trade_pct=4.0, max_stake_per_trade_usd=25.0
        )
        assert stake == pytest.approx(1.0)  # 10% of the thinner leg's depth

    def test_spot_anchor_uses_fractional_kelly(self):
        opp = make_spot_anchor_opportunity()
        stake = size_polycrypto_opportunity(
            opp, bankroll_usd=50.0, max_stake_per_trade_pct=4.0, max_stake_per_trade_usd=25.0, kelly_fraction_cap=0.15
        )
        # Kelly for p=0.85 at 1/0.6 payout: b=2/3, f=(0.85*2/3-0.15)/(2/3)=0.625
        expected = 0.625 * 0.15 * 0.8 * 50.0
        assert stake == pytest.approx(min(expected, 50.0 * 0.04))

    def test_spot_anchor_without_model_metadata_sizes_zero(self):
        opp = make_spot_anchor_opportunity()
        del opp.metadata["model_prob"]
        assert (
            size_polycrypto_opportunity(opp, bankroll_usd=50.0, max_stake_per_trade_pct=4.0, max_stake_per_trade_usd=25.0)
            == 0.0
        )
