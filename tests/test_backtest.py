from __future__ import annotations

from arbbot.backtest.engine import run_backtest
from arbbot.backtest.synthetic_data import generate_synthetic_events
from arbbot.config import AppConfig


def test_pure_arbitrage_backtest_has_perfect_win_rate():
    """Proportional-stake arbitrage guarantees the same payout regardless of
    which outcome occurs, so every accepted arbitrage trade should win. This
    is the core "low risk, high win rate" property the strategy relies on.
    """
    cfg = AppConfig()
    cfg.strategy.arbitrage.enabled = True
    cfg.strategy.value_edge.enabled = False
    cfg.risk.max_total_exposure_pct = 10_000  # don't let exposure caps suppress trades in this check
    cfg.risk.max_stake_per_trade_usd = 10_000
    cfg.risk.max_daily_loss_pct = 10_000

    events = generate_synthetic_events(n_events=300, seed=7, arb_probability=0.4)
    result = run_backtest(events, cfg)

    assert result.total_trades > 0
    assert result.win_rate == 1.0
    assert result.total_pnl_usd > 0


def test_backtest_respects_min_edge_threshold():
    cfg = AppConfig()
    cfg.strategy.arbitrage.enabled = True
    cfg.strategy.arbitrage.min_edge_pct = 50.0  # unreasonably high -- nothing should qualify
    cfg.strategy.value_edge.enabled = False

    events = generate_synthetic_events(n_events=100, seed=7, arb_probability=0.4)
    result = run_backtest(events, cfg)

    assert result.total_trades == 0
