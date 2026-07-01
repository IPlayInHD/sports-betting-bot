from __future__ import annotations

from arbbot.risk.risk_manager import RiskLimits, RiskManager


def _manager(**overrides) -> RiskManager:
    defaults = dict(
        bankroll_usd=1000.0,
        max_stake_per_trade_pct=5.0,
        max_stake_per_trade_usd=100.0,
        max_total_exposure_pct=25.0,
        max_daily_loss_pct=5.0,
        max_consecutive_losses=3,
    )
    defaults.update(overrides)
    return RiskManager(RiskLimits(**defaults))


def test_can_trade_allows_within_limits():
    rm = _manager()
    can_trade, reason = rm.can_trade(50.0)
    assert can_trade is True
    assert reason == ""


def test_can_trade_rejects_zero_or_negative_stake():
    rm = _manager()
    can_trade, _ = rm.can_trade(0.0)
    assert can_trade is False


def test_can_trade_rejects_over_exposure_cap():
    rm = _manager(max_total_exposure_pct=10.0)  # max exposure = $100
    rm.on_position_opened(90.0)
    can_trade, reason = rm.can_trade(20.0)
    assert can_trade is False
    assert "exposure" in reason


def test_daily_loss_limit_halts_trading():
    rm = _manager(max_daily_loss_pct=5.0)  # max daily loss = $50
    rm.on_position_opened(50.0)
    rm.on_position_closed(50.0, -60.0)  # breach the daily loss limit
    can_trade, reason = rm.can_trade(10.0)
    assert can_trade is False
    assert rm.state.trading_halted is True
    assert "daily loss" in reason


def test_consecutive_losses_halts_trading():
    rm = _manager(max_consecutive_losses=3)
    for _ in range(3):
        rm.on_position_opened(10.0)
        rm.on_position_closed(10.0, -1.0)
    can_trade, reason = rm.can_trade(10.0)
    assert can_trade is False
    assert "consecutive losses" in reason


def test_win_resets_consecutive_loss_counter():
    rm = _manager(max_consecutive_losses=3)
    rm.on_position_opened(10.0)
    rm.on_position_closed(10.0, -1.0)
    rm.on_position_opened(10.0)
    rm.on_position_closed(10.0, 5.0)  # a win resets the streak
    assert rm.state.consecutive_losses == 0


def test_reset_halt_resumes_trading():
    # Use a consecutive-losses halt (not a daily-loss halt): reset_halt()
    # deliberately clears the consecutive-loss counter but leaves
    # daily_realized_pnl_usd untouched, so a daily-loss halt should persist
    # for the rest of the day even after a manual reset -- only a
    # consecutive-loss halt is meant to be manually clearable intraday.
    rm = _manager(max_consecutive_losses=2)
    for _ in range(2):
        rm.on_position_opened(10.0)
        rm.on_position_closed(10.0, -1.0)

    can_trade, _ = rm.can_trade(1.0)
    assert can_trade is False
    assert rm.state.trading_halted is True

    rm.reset_halt()
    assert rm.state.trading_halted is False
    can_trade, _ = rm.can_trade(1.0)
    assert can_trade is True
