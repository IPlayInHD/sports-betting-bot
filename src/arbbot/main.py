"""Orchestrator: wires all ten layers together into a single async polling loop.

    Layer 1  data ingestion         odds/*, polymarket/*
    Layer 2  normalization/de-vig   odds/normalization.py
    Layer 3  market matching        matching/market_matcher.py
    Layer 4  arbitrage detection    strategy/arbitrage.py
    Layer 5  value-edge detection   strategy/value_edge.py   (opt-in)
    Layer 6  filter pipeline        strategy/filters.py
    Layer 7  confidence scoring     strategy/confidence.py
    Layer 8  risk mgmt / sizing     risk/*
    Layer 9  execution              execution/*
    Layer 10 monitoring             monitoring/*

Defaults to paper trading. Live trading requires config mode: live AND the
I_UNDERSTAND_THE_RISKS=true environment variable (see config.py and README).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

from arbbot.config import AppConfig, Secrets, is_live_trading_authorized, load_config, load_secrets
from arbbot.execution.base import ExecutionClient, ManualAlertSportsbookExecutionClient
from arbbot.execution.order_manager import OrderManager
from arbbot.execution.paper import PaperExecutionClient
from arbbot.logging_setup import log_event, setup_logging
from arbbot.matching.market_matcher import match_markets
from arbbot.models import GapSignal, SportsbookQuote
from arbbot.monitoring.alerts import AlertDispatcher
from arbbot.monitoring.metrics import MetricsTracker
from arbbot.odds.base import OddsProvider
from arbbot.odds.mock_provider import MockOddsProvider
from arbbot.odds.the_odds_api import TheOddsApiProvider
from arbbot.polymarket.base import PolymarketDataClient
from arbbot.polymarket.clob_execution import PolymarketLiveExecutionClient
from arbbot.polymarket.clob_market_data import ClobMarketDataClient
from arbbot.polymarket.mock_client import MockPolymarketClient
from arbbot.risk.position_sizing import size_signal
from arbbot.risk.risk_manager import RiskLimits, RiskManager
from arbbot.strategy.arbitrage import detect_arbitrage
from arbbot.strategy.confidence import score_confidence
from arbbot.strategy.filters import ExposureTracker, FilterConfig, run_filter_pipeline
from arbbot.strategy.value_edge import detect_value_edges

logger = logging.getLogger("arbbot.main")


async def _run_cycle(
    cfg: AppConfig,
    odds_provider: OddsProvider,
    poly_client: PolymarketDataClient,
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    metrics: MetricsTracker,
    filter_cfg: FilterConfig,
) -> None:
    odds_results = await asyncio.gather(
        *(odds_provider.fetch_quotes(sport) for sport in cfg.sports), return_exceptions=True
    )
    sportsbook_quotes: list[SportsbookQuote] = []
    for sport, result in zip(cfg.sports, odds_results):
        if isinstance(result, Exception):
            logger.error("failed to fetch odds for %s: %s", sport, result)
            continue
        sportsbook_quotes.extend(result)

    try:
        polymarket_quotes = await poly_client.fetch_sports_markets()
    except Exception as exc:  # noqa: BLE001 - external API boundary
        logger.error("failed to fetch polymarket markets: %s", exc)
        return

    if not sportsbook_quotes or not polymarket_quotes:
        return

    matches = match_markets(
        sportsbook_quotes,
        polymarket_quotes,
        min_match_score=cfg.matching.min_match_score,
        max_date_skew_hours=cfg.matching.max_date_skew_hours,
    )
    if not matches:
        return

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
        return

    accepted = run_filter_pipeline(signals, filter_cfg, exposure_tracker)

    for signal in accepted:
        signal.confidence = score_confidence(signal, historical_fill_rate=metrics.fill_rate)
        stake_usd = size_signal(
            signal,
            bankroll_usd=cfg.risk.bankroll_usd,
            max_stake_per_trade_pct=cfg.risk.max_stake_per_trade_pct,
            max_stake_per_trade_usd=cfg.risk.max_stake_per_trade_usd,
            kelly_fraction_cap=cfg.strategy.value_edge.kelly_fraction,
        )

        can_trade, reason = risk_manager.can_trade(stake_usd)
        if not can_trade:
            logger.info("signal %s skipped: %s", signal.signal_id, reason)
            continue

        position = await order_manager.execute_signal(signal, stake_usd)
        exposure_tracker.on_open(signal)
        risk_manager.on_position_opened(stake_usd)
        if position.orders:
            metrics.record_latency((position.orders[0].submitted_at - signal.created_at) * 1000.0)

        log_event(
            logger,
            logging.INFO,
            "signal executed",
            signal_id=signal.signal_id,
            signal_type=signal.signal_type.value,
            edge_pct=round(signal.edge_pct, 3),
            confidence=signal.confidence,
            stake_usd=round(stake_usd, 2),
        )


def _build_data_sources(cfg: AppConfig, secrets: Secrets) -> tuple[OddsProvider, PolymarketDataClient, bool]:
    use_mock = os.getenv("ARBBOT_USE_MOCK_DATA", "").lower() == "true" or not secrets.odds_api_key
    if use_mock:
        logger.warning("ODDS_API_KEY not set (or ARBBOT_USE_MOCK_DATA=true) -- using synthetic odds/market data")
        return MockOddsProvider(), MockPolymarketClient(), True
    return TheOddsApiProvider(api_key=secrets.odds_api_key), ClobMarketDataClient(), False


def _build_execution_clients(
    cfg: AppConfig, secrets: Secrets, alert_dispatcher: AlertDispatcher
) -> tuple[ExecutionClient, ExecutionClient]:
    if cfg.mode != "live":
        paper = PaperExecutionClient(slippage_bps=cfg.execution.paper_slippage_bps)
        logger.info("Running in PAPER TRADING mode -- no real orders will be placed")
        return paper, paper

    if not is_live_trading_authorized(cfg, secrets):
        raise RuntimeError(
            "mode is 'live' but live trading is not authorized. Set "
            "I_UNDERSTAND_THE_RISKS=true in your environment AFTER reading the "
            "README safety section. Refusing to start."
        )

    logger.warning("LIVE TRADING ENABLED -- real orders will be submitted to Polymarket")
    polymarket_exec = PolymarketLiveExecutionClient(
        private_key=secrets.polymarket_private_key,
        api_key=secrets.polymarket_api_key,
        api_secret=secrets.polymarket_api_secret,
        api_passphrase=secrets.polymarket_api_passphrase,
        funder_address=secrets.polymarket_funder_address,
        confirmed=True,
    )
    sportsbook_exec = ManualAlertSportsbookExecutionClient(alert_dispatcher.order_needs_manual_action)
    return polymarket_exec, sportsbook_exec


async def run(cfg: AppConfig, secrets: Secrets) -> None:
    setup_logging(cfg.monitoring.log_level)

    odds_provider, poly_client, _ = _build_data_sources(cfg, secrets)
    alert_dispatcher = AlertDispatcher(webhook_url=secrets.alert_webhook_url)
    exposure_tracker = ExposureTracker()
    metrics = MetricsTracker(window_size=cfg.monitoring.metrics_window_size)
    risk_manager = RiskManager(
        RiskLimits(
            bankroll_usd=cfg.risk.bankroll_usd,
            max_stake_per_trade_pct=cfg.risk.max_stake_per_trade_pct,
            max_stake_per_trade_usd=cfg.risk.max_stake_per_trade_usd,
            max_total_exposure_pct=cfg.risk.max_total_exposure_pct,
            max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
            max_consecutive_losses=cfg.risk.max_consecutive_losses,
        )
    )
    filter_cfg = FilterConfig(
        max_quote_age_sec=cfg.polling.max_quote_age_sec,
        min_polymarket_liquidity_usd=cfg.filters.min_polymarket_liquidity_usd,
        max_spread_pct=cfg.filters.max_spread_pct,
        max_signal_latency_ms=cfg.filters.max_signal_latency_ms,
        max_concurrent_positions_per_market_group=cfg.filters.max_concurrent_positions_per_market_group,
    )

    polymarket_exec, sportsbook_exec = _build_execution_clients(cfg, secrets, alert_dispatcher)
    order_manager = OrderManager(
        polymarket_client=polymarket_exec,
        sportsbook_client=sportsbook_exec,
        order_timeout_sec=cfg.execution.order_timeout_sec,
        max_retries=cfg.execution.max_retries,
    )

    poll_interval = min(cfg.polling.odds_poll_interval_sec, cfg.polling.polymarket_poll_interval_sec)

    try:
        while True:
            cycle_start = time.perf_counter()
            try:
                await _run_cycle(
                    cfg, odds_provider, poly_client, exposure_tracker, risk_manager, order_manager, metrics, filter_cfg
                )
            except Exception:  # noqa: BLE001 - keep the polling loop alive across transient errors
                logger.exception("unhandled error in polling cycle")
            if metrics.total_trades and metrics.total_trades % 10 == 0:
                log_event(logger, logging.INFO, "metrics snapshot", **metrics.summary())
            elapsed = time.perf_counter() - cycle_start
            await asyncio.sleep(max(0.0, poll_interval - elapsed))
    finally:
        await odds_provider.close()
        await poly_client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sportsbook <-> Polymarket gap-trading bot")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    secrets = load_secrets()
    try:
        asyncio.run(run(cfg, secrets))
    except KeyboardInterrupt:
        logger.info("shutdown requested, exiting cleanly")


if __name__ == "__main__":
    main()
