"""Layer 8b: portfolio-level risk management and kill switches.

This is the last gate before an order is sent to the execution layer. It
tracks running exposure and P&L and will refuse new trades (or halt the bot
entirely) once configured limits are breached, independent of how attractive
any individual signal looks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RiskLimits:
    bankroll_usd: float = 1000.0
    max_stake_per_trade_pct: float = 2.0
    max_stake_per_trade_usd: float = 50.0
    max_total_exposure_pct: float = 25.0
    max_daily_loss_pct: float = 5.0
    max_consecutive_losses: int = 5


@dataclass(slots=True)
class RiskState:
    open_exposure_usd: float = 0.0
    daily_realized_pnl_usd: float = 0.0
    consecutive_losses: int = 0
    trading_halted: bool = False
    halt_reason: str = ""
    day_start_ts: float = field(default_factory=time.time)


class RiskManager:
    """Stateful risk manager. One instance should be shared across the whole
    bot run so exposure/loss tracking is global, not per-signal.
    """

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.state = RiskState()

    def _maybe_roll_day(self) -> None:
        if time.time() - self.state.day_start_ts >= 86400:
            logger.info("risk_manager: rolling daily P&L window")
            self.state.daily_realized_pnl_usd = 0.0
            self.state.day_start_ts = time.time()

    def can_trade(self, proposed_stake_usd: float) -> tuple[bool, str]:
        self._maybe_roll_day()

        if self.state.trading_halted:
            return False, f"trading halted: {self.state.halt_reason}"

        if proposed_stake_usd <= 0:
            return False, "zero/negative stake"

        max_exposure = self.limits.bankroll_usd * (self.limits.max_total_exposure_pct / 100.0)
        if self.state.open_exposure_usd + proposed_stake_usd > max_exposure:
            return False, (
                f"would exceed max total exposure "
                f"(${self.state.open_exposure_usd:.2f} + ${proposed_stake_usd:.2f} > ${max_exposure:.2f})"
            )

        max_daily_loss = self.limits.bankroll_usd * (self.limits.max_daily_loss_pct / 100.0)
        if self.state.daily_realized_pnl_usd <= -max_daily_loss:
            self._halt(f"daily loss limit reached (${self.state.daily_realized_pnl_usd:.2f})")
            return False, self.state.halt_reason

        if self.state.consecutive_losses >= self.limits.max_consecutive_losses:
            self._halt(f"{self.state.consecutive_losses} consecutive losses")
            return False, self.state.halt_reason

        return True, ""

    def _halt(self, reason: str) -> None:
        self.state.trading_halted = True
        self.state.halt_reason = reason
        logger.warning("risk_manager: TRADING HALTED - %s", reason)

    def reset_halt(self) -> None:
        """Manual override to resume trading after a halt (e.g. next day, or
        after human review). Never called automatically.
        """
        self.state.trading_halted = False
        self.state.halt_reason = ""
        self.state.consecutive_losses = 0

    def on_position_opened(self, stake_usd: float) -> None:
        self.state.open_exposure_usd += stake_usd

    def on_position_closed(self, stake_usd: float, realized_pnl_usd: float) -> None:
        self.state.open_exposure_usd = max(0.0, self.state.open_exposure_usd - stake_usd)
        self.state.daily_realized_pnl_usd += realized_pnl_usd
        if realized_pnl_usd < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
