"""Backtest engine: replays synthetic (or user-supplied) historical matched
markets through the exact same detection -> filter -> confidence -> sizing
pipeline used live, then settles each simulated trade against the known
ground-truth outcome to measure win rate and P&L before risking real money.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arbbot.backtest.synthetic_data import SyntheticEvent
from arbbot.config import AppConfig
from arbbot.models import GapSignal, SignalType
from arbbot.risk.position_sizing import size_signal
from arbbot.risk.risk_manager import RiskLimits, RiskManager
from arbbot.strategy.arbitrage import detect_arbitrage
from arbbot.strategy.confidence import score_confidence
from arbbot.strategy.filters import ExposureTracker, FilterConfig, run_filter_pipeline
from arbbot.strategy.value_edge import detect_value_edges


@dataclass(slots=True)
class BacktestTradeRecord:
    signal_id: str
    signal_type: str
    event_id: str
    stake_usd: float
    pnl_usd: float
    won: bool
    edge_pct: float


@dataclass(slots=True)
class BacktestResult:
    trades: list[BacktestTradeRecord] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def total_pnl_usd(self) -> float:
        return sum(t.pnl_usd for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.won) / len(self.trades)

    def summary(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "win_rate": round(self.win_rate, 4),
            "avg_edge_pct": round(sum(t.edge_pct for t in self.trades) / len(self.trades), 3)
            if self.trades
            else 0.0,
        }


def _settle_arbitrage(signal: GapSignal, stake_usd: float, winning_outcome_name: str) -> float:
    total_payout = 0.0
    for leg in signal.metadata.get("legs", []):
        if leg["outcome_name"] == winning_outcome_name:
            total_payout += (stake_usd * leg["stake_fraction"]) * (1.0 / leg["prob"])
    return total_payout - stake_usd


def _settle_value_edge(signal: GapSignal, stake_usd: float, winning_outcome_name: str) -> float:
    poly_ask = signal.metadata.get("poly_ask")
    won_outcome_name = signal.matched_market.polymarket_quote.outcome_name
    if poly_ask and won_outcome_name == winning_outcome_name:
        return stake_usd * (1.0 / poly_ask) - stake_usd
    return -stake_usd


def _risk_limits_from_config(cfg: AppConfig) -> RiskLimits:
    return RiskLimits(
        bankroll_usd=cfg.risk.bankroll_usd,
        max_stake_per_trade_pct=cfg.risk.max_stake_per_trade_pct,
        max_stake_per_trade_usd=cfg.risk.max_stake_per_trade_usd,
        max_total_exposure_pct=cfg.risk.max_total_exposure_pct,
        max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
        max_consecutive_losses=cfg.risk.max_consecutive_losses,
    )


def run_backtest(events: list[SyntheticEvent], cfg: AppConfig) -> BacktestResult:
    result = BacktestResult()
    exposure_tracker = ExposureTracker()
    risk_manager = RiskManager(_risk_limits_from_config(cfg))
    filter_cfg = FilterConfig(
        max_quote_age_sec=3600.0,  # synthetic quotes are all "fresh" relative to generation time
        min_polymarket_liquidity_usd=cfg.filters.min_polymarket_liquidity_usd,
        max_spread_pct=cfg.filters.max_spread_pct,
        max_signal_latency_ms=60_000.0,  # backtests aren't wall-clock latency sensitive
        max_concurrent_positions_per_market_group=cfg.filters.max_concurrent_positions_per_market_group,
    )

    for event in events:
        matches = event.matched_markets
        signals: list[GapSignal] = []
        if cfg.strategy.arbitrage.enabled:
            signals.extend(
                detect_arbitrage(
                    matches,
                    min_edge_pct=cfg.strategy.arbitrage.min_edge_pct,
                    max_edge_pct=cfg.strategy.arbitrage.max_edge_pct,
                    fee_buffer_pct=cfg.risk.fee_buffer_pct,
                )
            )
        if cfg.strategy.value_edge.enabled:
            signals.extend(
                detect_value_edges(
                    matches,
                    min_books_agreeing=cfg.strategy.value_edge.min_books_agreeing,
                    min_edge_pct=cfg.strategy.value_edge.min_edge_pct,
                )
            )
        if not signals:
            continue

        accepted = run_filter_pipeline(signals, filter_cfg, exposure_tracker)
        winning_outcome_name = event.home_team if event.home_wins else event.away_team

        for signal in accepted:
            signal.confidence = score_confidence(signal)
            stake_usd = size_signal(
                signal,
                bankroll_usd=cfg.risk.bankroll_usd,
                max_stake_per_trade_pct=cfg.risk.max_stake_per_trade_pct,
                max_stake_per_trade_usd=cfg.risk.max_stake_per_trade_usd,
                kelly_fraction_cap=cfg.strategy.value_edge.kelly_fraction,
            )
            can_trade, _reason = risk_manager.can_trade(stake_usd)
            if not can_trade:
                continue

            if signal.signal_type == SignalType.ARBITRAGE:
                pnl = _settle_arbitrage(signal, stake_usd, winning_outcome_name)
            else:
                pnl = _settle_value_edge(signal, stake_usd, winning_outcome_name)

            risk_manager.on_position_opened(stake_usd)
            risk_manager.on_position_closed(stake_usd, pnl)

            result.trades.append(
                BacktestTradeRecord(
                    signal_id=signal.signal_id,
                    signal_type=signal.signal_type.value,
                    event_id=event.event_id,
                    stake_usd=round(stake_usd, 2),
                    pnl_usd=round(pnl, 2),
                    won=pnl > 0,
                    edge_pct=signal.edge_pct,
                )
            )

    return result
