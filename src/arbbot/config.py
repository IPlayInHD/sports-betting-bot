"""Configuration loading: YAML file for strategy/risk parameters, environment
variables (via .env) for secrets. Secrets are never read from the YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PollingConfig(BaseModel):
    odds_poll_interval_sec: float = 2.0
    polymarket_poll_interval_sec: float = 1.0
    max_quote_age_sec: float = 3.0


class MatchingConfig(BaseModel):
    min_match_score: float = 0.82
    max_date_skew_hours: float = 6.0


class ArbitrageStrategyConfig(BaseModel):
    enabled: bool = True
    min_edge_pct: float = 0.5
    max_edge_pct: float = 8.0


class ValueEdgeStrategyConfig(BaseModel):
    enabled: bool = False
    min_books_agreeing: int = 3
    min_edge_pct: float = 3.0
    kelly_fraction: float = 0.15


class StrategyConfig(BaseModel):
    arbitrage: ArbitrageStrategyConfig = ArbitrageStrategyConfig()
    value_edge: ValueEdgeStrategyConfig = ValueEdgeStrategyConfig()


class FiltersConfig(BaseModel):
    min_polymarket_liquidity_usd: float = 500
    max_spread_pct: float = 3.0
    max_signal_latency_ms: float = 800
    max_concurrent_positions_per_market_group: int = 1


class RiskConfig(BaseModel):
    bankroll_usd: float = 1000
    max_stake_per_trade_pct: float = 2.0
    max_stake_per_trade_usd: float = 50
    max_total_exposure_pct: float = 25
    max_daily_loss_pct: float = 5
    max_consecutive_losses: int = 5
    fee_buffer_pct: float = 1.0


class ExecutionConfig(BaseModel):
    paper_slippage_bps: float = 15
    order_timeout_sec: float = 5
    max_retries: int = 2


class MonitoringConfig(BaseModel):
    log_level: str = "INFO"
    metrics_window_size: int = 200


class CryptoMarketConfig(BaseModel):
    enabled: bool = True
    # ccxt/exchange ids. Default assumes a US-based user (binanceus, not
    # binance.com, which blocks US IPs); switch to "binance" if that's not you.
    exchanges: list[str] = Field(default_factory=lambda: ["coinbase", "kraken", "binanceus"])
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"])
    poll_interval_sec: float = 2.0
    min_edge_pct: float = 0.3
    max_edge_pct: float = 5.0
    fee_pct_per_leg: float = 0.1
    max_stake_per_trade_pct: float = 2.0
    max_stake_per_trade_usd: float = 50.0
    max_concurrent_positions_per_symbol: int = 1


class MarketsConfig(BaseModel):
    crypto: CryptoMarketConfig = CryptoMarketConfig()


class AppConfig(BaseModel):
    mode: Literal["paper", "backtest", "live"] = "paper"
    sports: list[str] = Field(default_factory=list)
    polling: PollingConfig = PollingConfig()
    matching: MatchingConfig = MatchingConfig()
    strategy: StrategyConfig = StrategyConfig()
    filters: FiltersConfig = FiltersConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    markets: MarketsConfig = MarketsConfig()


class Secrets(BaseSettings):
    """Loaded from environment / .env file. Never persisted to YAML or logs."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    odds_api_key: str | None = Field(default=None, alias="ODDS_API_KEY")
    polymarket_private_key: str | None = Field(default=None, alias="POLYMARKET_PRIVATE_KEY")
    polymarket_api_key: str | None = Field(default=None, alias="POLYMARKET_API_KEY")
    polymarket_api_secret: str | None = Field(default=None, alias="POLYMARKET_API_SECRET")
    polymarket_api_passphrase: str | None = Field(default=None, alias="POLYMARKET_API_PASSPHRASE")
    polymarket_funder_address: str | None = Field(default=None, alias="POLYMARKET_FUNDER_ADDRESS")
    i_understand_the_risks: bool = Field(default=False, alias="I_UNDERSTAND_THE_RISKS")
    alert_webhook_url: str | None = Field(default=None, alias="ALERT_WEBHOOK_URL")


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        return AppConfig()
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


def load_secrets() -> Secrets:
    return Secrets()


def get_crypto_exchange_credentials(exchange_id: str) -> tuple[str | None, str | None]:
    """Per-exchange API credentials for live crypto trading, e.g.
    CRYPTO_COINBASE_API_KEY / CRYPTO_COINBASE_API_SECRET. Plain env lookups
    (not a fixed pydantic field) since the exchange list is user-configurable
    rather than a fixed set.
    """
    prefix = f"CRYPTO_{exchange_id.upper()}"
    return os.getenv(f"{prefix}_API_KEY"), os.getenv(f"{prefix}_API_SECRET")


def is_live_trading_authorized(cfg: AppConfig, secrets: Secrets) -> bool:
    """Live trading requires BOTH the config mode set to 'live' AND the explicit
    environment-variable safety acknowledgement. This dual opt-in prevents a bot
    from accidentally trading real money because of a single misconfigured flag.
    """
    return cfg.mode == "live" and secrets.i_understand_the_risks is True
