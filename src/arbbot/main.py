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
from collections import defaultdict

from arbbot.config import (
    AppConfig,
    Secrets,
    get_crypto_exchange_credentials,
    is_live_trading_authorized,
    load_config,
    load_secrets,
)
from arbbot.execution.base import ExecutionClient, ManualAlertSportsbookExecutionClient
from arbbot.execution.order_manager import OrderManager
from arbbot.execution.paper import PaperExecutionClient
from arbbot.logging_setup import log_event, setup_logging
from arbbot.markets.crypto.detector import detect_crypto_arbitrage
from arbbot.markets.crypto.exchanges import BinancePriceFeed, CoinbasePriceFeed, CryptoPriceFeed, KrakenPriceFeed
from arbbot.markets.crypto.execution import CcxtLiveExecutionClient, CryptoOrderManager, PaperCryptoExecutionClient
from arbbot.markets.crypto.mock import MockCryptoPriceFeed
from arbbot.matching.market_matcher import match_markets
from arbbot.models import CryptoQuote, GapSignal, SportsbookQuote
from arbbot.monitoring import trade_log
from arbbot.monitoring.alerts import AlertDispatcher
from arbbot.monitoring.metrics import MetricsTracker
from arbbot.odds.base import OddsProvider
from arbbot.odds.mock_provider import MockOddsProvider
from arbbot.odds.the_odds_api import TheOddsApiProvider
from arbbot.polymarket.base import PolymarketDataClient
from arbbot.polymarket.clob_execution import PolymarketLiveExecutionClient
from arbbot.polymarket.clob_market_data import ClobMarketDataClient
from arbbot.polymarket.mock_client import MockPolymarketClient
from arbbot.risk.position_sizing import size_crypto_opportunity, size_signal
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
    db_conn,
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

        latency_ms = (position.orders[0].submitted_at - signal.created_at) * 1000.0 if position.orders else 0.0
        metrics.record_latency(latency_ms)

        trade_log.record_trade(
            db_conn,
            signal_id=signal.signal_id,
            mode=cfg.mode,
            signal_type=signal.signal_type.value,
            event_id=signal.metadata.get("event_id", signal.matched_market.match_id),
            edge_pct=signal.edge_pct,
            confidence=signal.confidence,
            stake_usd=stake_usd,
            locked_in_profit_usd=position.guaranteed_profit_usd,
            latency_ms=latency_ms,
            sportsbook_fraction=signal.sportsbook_stake_fraction,
            polymarket_fraction=signal.polymarket_stake_fraction,
            market_family="sports",
        )

        log_event(
            logger,
            logging.INFO,
            "signal executed",
            signal_id=signal.signal_id,
            signal_type=signal.signal_type.value,
            edge_pct=round(signal.edge_pct, 3),
            confidence=signal.confidence,
            stake_usd=round(stake_usd, 2),
            locked_in_profit_usd=position.guaranteed_profit_usd,
        )


async def _run_crypto_cycle(
    cfg: AppConfig,
    price_feeds: list[CryptoPriceFeed],
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: CryptoOrderManager,
    db_conn,
) -> None:
    crypto_cfg = cfg.markets.crypto

    results = await asyncio.gather(
        *(feed.fetch_quotes(crypto_cfg.symbols) for feed in price_feeds), return_exceptions=True
    )
    quotes: list[CryptoQuote] = []
    for feed, result in zip(price_feeds, results):
        if isinstance(result, Exception):
            logger.error("failed to fetch crypto quotes from %s: %s", feed.exchange_name, result)
            continue
        quotes.extend(result)

    if not quotes:
        return

    opportunities = detect_crypto_arbitrage(
        quotes,
        min_edge_pct=crypto_cfg.min_edge_pct,
        max_edge_pct=crypto_cfg.max_edge_pct,
        fee_pct_per_leg=crypto_cfg.fee_pct_per_leg,
    )

    for opp in opportunities:
        exposure_key = f"crypto:{opp.symbol}"
        exposure_result = exposure_tracker.check_key(exposure_key, crypto_cfg.max_concurrent_positions_per_symbol)
        if not exposure_result.passed:
            logger.info("opportunity %s skipped: %s", opp.opportunity_id, exposure_result.reason)
            continue

        stake_usd = size_crypto_opportunity(
            opp,
            bankroll_usd=cfg.risk.bankroll_usd,
            max_stake_per_trade_pct=crypto_cfg.max_stake_per_trade_pct,
            max_stake_per_trade_usd=crypto_cfg.max_stake_per_trade_usd,
        )

        can_trade, reason = risk_manager.can_trade(stake_usd)
        if not can_trade:
            logger.info("opportunity %s skipped: %s", opp.opportunity_id, reason)
            continue

        position = await order_manager.execute_opportunity(opp, stake_usd)
        exposure_tracker.on_open_key(exposure_key)
        risk_manager.on_position_opened(stake_usd)

        # Unlike sports (which waits on a real-world game outcome), both legs
        # of a crypto arb settle synchronously -- the round trip is already
        # complete once orders fill, so release exposure/risk immediately
        # instead of leaving the position "open" indefinitely.
        if position.guaranteed_profit_usd is not None:
            risk_manager.on_position_closed(stake_usd, position.guaranteed_profit_usd)
            exposure_tracker.on_close_key(exposure_key)

        latency_ms = (position.orders[0].submitted_at - opp.created_at) * 1000.0 if position.orders else 0.0

        trade_log.record_trade(
            db_conn,
            signal_id=opp.opportunity_id,
            mode=cfg.mode,
            signal_type="crypto_arbitrage",
            event_id=opp.symbol,
            edge_pct=opp.edge_pct,
            confidence=opp.confidence,
            stake_usd=stake_usd,
            locked_in_profit_usd=position.guaranteed_profit_usd,
            latency_ms=latency_ms,
            sportsbook_fraction=0.0,
            polymarket_fraction=0.0,
            market_family="crypto",
        )

        log_event(
            logger,
            logging.INFO,
            "crypto opportunity executed",
            opportunity_id=opp.opportunity_id,
            symbol=opp.symbol,
            edge_pct=round(opp.edge_pct, 3),
            stake_usd=round(stake_usd, 2),
            locked_in_profit_usd=position.guaranteed_profit_usd,
            buy_exchange=opp.metadata.get("buy_exchange"),
            sell_exchange=opp.metadata.get("sell_exchange"),
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


def _build_crypto_price_feeds(cfg: AppConfig, use_mock: bool) -> list[CryptoPriceFeed]:
    if use_mock:
        return [MockCryptoPriceFeed()]

    feed_factories = {
        "coinbase": CoinbasePriceFeed,
        "kraken": KrakenPriceFeed,
        "binanceus": lambda: BinancePriceFeed(base_url="https://api.binance.us"),
        "binance": lambda: BinancePriceFeed(base_url="https://api.binance.com"),
    }
    feeds = []
    for exchange_id in cfg.markets.crypto.exchanges:
        factory = feed_factories.get(exchange_id)
        if factory is None:
            logger.warning("unknown crypto exchange id '%s' in config, skipping", exchange_id)
            continue
        feeds.append(factory())
    return feeds


def _build_crypto_execution_clients(
    cfg: AppConfig, secrets: Secrets, feeds: list[CryptoPriceFeed]
) -> dict[str, ExecutionClient]:
    if cfg.mode != "live":
        paper = PaperCryptoExecutionClient(slippage_bps=cfg.execution.paper_slippage_bps)
        # A defaultdict (not a plain dict) because MockCryptoPriceFeed is a
        # single feed object that simulates several distinct fake exchange
        # names internally -- there's no 1:1 mapping from feed to exchange
        # name to pre-populate here, and in paper mode every venue routes to
        # the same simulated client regardless of name anyway.
        return defaultdict(lambda: paper)

    if not is_live_trading_authorized(cfg, secrets):
        raise RuntimeError(
            "mode is 'live' but live trading is not authorized. Set "
            "I_UNDERSTAND_THE_RISKS=true in your environment AFTER reading the "
            "README safety section. Refusing to start."
        )

    logger.warning("LIVE TRADING ENABLED -- real orders will be submitted to your crypto exchange(s)")
    clients: dict[str, ExecutionClient] = {}
    for feed in feeds:
        api_key, api_secret = get_crypto_exchange_credentials(feed.exchange_name)
        clients[feed.exchange_name] = CcxtLiveExecutionClient(
            exchange_id=feed.exchange_name, api_key=api_key, api_secret=api_secret, confirmed=True
        )
    return clients


async def _sports_loop(
    cfg: AppConfig,
    odds_provider: OddsProvider,
    poly_client: PolymarketDataClient,
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    metrics: MetricsTracker,
    filter_cfg: FilterConfig,
    db_conn,
) -> None:
    poll_interval = min(cfg.polling.odds_poll_interval_sec, cfg.polling.polymarket_poll_interval_sec)
    while True:
        cycle_start = time.perf_counter()
        try:
            await _run_cycle(
                cfg, odds_provider, poly_client, exposure_tracker, risk_manager, order_manager, metrics, filter_cfg, db_conn
            )
        except Exception:  # noqa: BLE001 - keep the polling loop alive across transient errors
            logger.exception("unhandled error in sports polling cycle")
        if metrics.total_trades and metrics.total_trades % 10 == 0:
            log_event(logger, logging.INFO, "metrics snapshot", **metrics.summary())
        elapsed = time.perf_counter() - cycle_start
        await asyncio.sleep(max(0.0, poll_interval - elapsed))


async def _crypto_loop(
    cfg: AppConfig,
    price_feeds: list[CryptoPriceFeed],
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: CryptoOrderManager,
    db_conn,
) -> None:
    poll_interval = cfg.markets.crypto.poll_interval_sec
    while True:
        cycle_start = time.perf_counter()
        try:
            await _run_crypto_cycle(cfg, price_feeds, exposure_tracker, risk_manager, order_manager, db_conn)
        except Exception:  # noqa: BLE001 - keep the polling loop alive across transient errors
            logger.exception("unhandled error in crypto polling cycle")
        elapsed = time.perf_counter() - cycle_start
        await asyncio.sleep(max(0.0, poll_interval - elapsed))


async def _heartbeat_loop(db_conn, risk_manager: RiskManager, interval_sec: float = 5.0) -> None:
    while True:
        trade_log.record_heartbeat(
            db_conn,
            trading_halted=risk_manager.state.trading_halted,
            halt_reason=risk_manager.state.halt_reason,
            open_exposure_usd=risk_manager.state.open_exposure_usd,
        )
        await asyncio.sleep(interval_sec)


async def run(cfg: AppConfig, secrets: Secrets) -> None:
    setup_logging(cfg.monitoring.log_level)

    odds_provider, poly_client, use_mock = _build_data_sources(cfg, secrets)
    alert_dispatcher = AlertDispatcher(webhook_url=secrets.alert_webhook_url)
    db_conn = trade_log.get_connection()
    trade_log.record_startup(db_conn, mode=cfg.mode, use_mock=use_mock, bankroll_usd=cfg.risk.bankroll_usd)
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

    tasks = [
        asyncio.create_task(
            _sports_loop(
                cfg, odds_provider, poly_client, exposure_tracker, risk_manager, order_manager, metrics, filter_cfg, db_conn
            )
        ),
        asyncio.create_task(_heartbeat_loop(db_conn, risk_manager)),
    ]

    crypto_feeds: list[CryptoPriceFeed] = []
    if cfg.markets.crypto.enabled:
        crypto_feeds = _build_crypto_price_feeds(cfg, use_mock)
        crypto_exec_clients = _build_crypto_execution_clients(cfg, secrets, crypto_feeds)
        crypto_order_manager = CryptoOrderManager(
            client_for_exchange=crypto_exec_clients,
            order_timeout_sec=cfg.execution.order_timeout_sec,
            max_retries=cfg.execution.max_retries,
        )
        tasks.append(
            asyncio.create_task(_crypto_loop(cfg, crypto_feeds, exposure_tracker, risk_manager, crypto_order_manager, db_conn))
        )

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await odds_provider.close()
        await poly_client.close()
        for feed in crypto_feeds:
            await feed.close()
        db_conn.close()


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
