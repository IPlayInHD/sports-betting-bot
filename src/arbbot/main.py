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
from arbbot.markets.polycrypto.data import PolymarketCryptoDataClient, PolymarketCryptoFeed
from arbbot.markets.polycrypto.detector import (
    SpotAnchorConfig,
    detect_complement_arbitrage,
    detect_spot_anchored_gaps,
)
from arbbot.markets.polycrypto.execution import PolycryptoOrderManager
from arbbot.markets.polycrypto.mock import MockPolymarketCryptoFeed
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
from arbbot.risk.position_sizing import size_crypto_opportunity, size_polycrypto_opportunity, size_signal
from arbbot.risk.risk_manager import RiskLimits, RiskManager
from arbbot.strategy.adaptive import AdaptivePoller, SignalThrottle
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
) -> int:
    """Returns the number of signals that survived the filter pipeline, which
    the adaptive poller uses to decide whether to burst-poll.
    """
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
        return 0

    if not sportsbook_quotes or not polymarket_quotes:
        return 0

    matches = match_markets(
        sportsbook_quotes,
        polymarket_quotes,
        min_match_score=cfg.matching.min_match_score,
        max_date_skew_hours=cfg.matching.max_date_skew_hours,
    )
    if not matches:
        return 0

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
        return 0

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

        event_id = signal.metadata.get("event_id", signal.matched_market.match_id)
        skip_reason = None
        if stake_usd < cfg.risk.min_stake_usd:
            skip_reason = f"sized stake ${stake_usd:.2f} below min_stake_usd"
        else:
            can_trade, reason = risk_manager.can_trade(stake_usd)
            if not can_trade:
                skip_reason = reason
        if skip_reason:
            logger.info("signal %s skipped: %s", signal.signal_id, skip_reason)
            trade_log.record_opportunity(
                db_conn,
                family="sports",
                strategy=signal.signal_type.value,
                symbol=event_id,
                detail=event_id,
                edge_pct=signal.edge_pct,
                confidence=signal.confidence,
                status="skipped",
                reason=skip_reason,
            )
            continue

        position = await order_manager.execute_signal(signal, stake_usd)
        exposure_tracker.on_open(signal)
        risk_manager.on_position_opened(stake_usd)

        latency_ms = (position.orders[0].submitted_at - signal.created_at) * 1000.0 if position.orders else 0.0
        metrics.record_latency(latency_ms)

        trade_log.record_opportunity(
            db_conn,
            family="sports",
            strategy=signal.signal_type.value,
            symbol=event_id,
            detail=event_id,
            edge_pct=signal.edge_pct,
            confidence=signal.confidence,
            status="executed",
        )
        trade_log.record_trade(
            db_conn,
            signal_id=signal.signal_id,
            mode=cfg.mode,
            signal_type=signal.signal_type.value,
            event_id=event_id,
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

    return len(accepted)


async def _run_crypto_cycle(
    cfg: AppConfig,
    price_feeds: list[CryptoPriceFeed],
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: CryptoOrderManager,
    throttle: SignalThrottle,
    db_conn,
) -> int:
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
        return 0

    opportunities = detect_crypto_arbitrage(
        quotes,
        min_edge_pct=crypto_cfg.min_edge_pct,
        max_edge_pct=crypto_cfg.max_edge_pct,
        fee_pct_per_leg=crypto_cfg.fee_pct_per_leg,
    )

    for opp in opportunities:
        def _skip(reason: str, opp=opp) -> None:
            logger.info("opportunity %s skipped: %s", opp.opportunity_id, reason)
            trade_log.record_opportunity(
                db_conn,
                family="crypto",
                strategy="cross_exchange",
                symbol=opp.symbol,
                detail=f"{opp.metadata.get('buy_exchange')} -> {opp.metadata.get('sell_exchange')}",
                edge_pct=opp.edge_pct,
                confidence=opp.confidence,
                status="skipped",
                reason=reason,
            )

        exposure_key = f"crypto:{opp.symbol}"
        if not throttle.ready(exposure_key):
            _skip("cooldown active (recently traded this symbol)")
            continue
        exposure_result = exposure_tracker.check_key(exposure_key, crypto_cfg.max_concurrent_positions_per_symbol)
        if not exposure_result.passed:
            _skip(exposure_result.reason)
            continue

        stake_usd = size_crypto_opportunity(
            opp,
            bankroll_usd=cfg.risk.bankroll_usd,
            max_stake_per_trade_pct=crypto_cfg.max_stake_per_trade_pct,
            max_stake_per_trade_usd=crypto_cfg.max_stake_per_trade_usd,
        )
        if stake_usd < cfg.risk.min_stake_usd:
            _skip(f"sized stake ${stake_usd:.2f} below min_stake_usd")
            continue

        can_trade, reason = risk_manager.can_trade(stake_usd)
        if not can_trade:
            _skip(reason)
            continue

        position = await order_manager.execute_opportunity(opp, stake_usd)
        exposure_tracker.on_open_key(exposure_key)
        risk_manager.on_position_opened(stake_usd)
        throttle.fire(exposure_key)

        # Unlike sports (which waits on a real-world game outcome), both legs
        # of a crypto arb settle synchronously -- the round trip is already
        # complete once orders fill, so release exposure/risk immediately
        # instead of leaving the position "open" indefinitely.
        if position.guaranteed_profit_usd is not None:
            risk_manager.on_position_closed(stake_usd, position.guaranteed_profit_usd)
            exposure_tracker.on_close_key(exposure_key)

        latency_ms = (position.orders[0].submitted_at - opp.created_at) * 1000.0 if position.orders else 0.0

        trade_log.record_opportunity(
            db_conn,
            family="crypto",
            strategy="cross_exchange",
            symbol=opp.symbol,
            detail=f"{opp.metadata.get('buy_exchange')} -> {opp.metadata.get('sell_exchange')}",
            edge_pct=opp.edge_pct,
            confidence=opp.confidence,
            status="executed",
        )
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

    return len(opportunities)


async def _run_polycrypto_cycle(
    cfg: AppConfig,
    poly_feed: PolymarketCryptoFeed,
    spot_feeds: list[CryptoPriceFeed],
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: PolycryptoOrderManager,
    throttle: SignalThrottle,
    db_conn,
) -> int:
    pc_cfg = cfg.markets.polymarket_crypto

    # return_exceptions=True on both branches so a transient network/HTTP
    # error from the public APIs becomes a concise one-line warning below,
    # not a full traceback dumped on every polling cycle.
    quotes_task = poly_feed.fetch_crypto_markets()
    if pc_cfg.spot_anchor.enabled and spot_feeds:
        spot_results = await asyncio.gather(
            quotes_task,
            *(feed.fetch_quotes(cfg.markets.crypto.symbols) for feed in spot_feeds),
            return_exceptions=True,
        )
        quotes_result, spot_quote_results = spot_results[0], spot_results[1:]
    else:
        quotes_result = (await asyncio.gather(quotes_task, return_exceptions=True))[0]
        spot_quote_results = []

    if isinstance(quotes_result, Exception):
        logger.warning("failed to fetch polymarket crypto markets (will retry next cycle): %s", quotes_result)
        return 0
    poly_quotes = quotes_result
    if not poly_quotes:
        return 0

    # Average each symbol's mid across whichever exchanges responded this
    # cycle -- a multi-venue consensus anchor rather than trusting one feed.
    mids_by_symbol: dict[str, list[float]] = {}
    for result in spot_quote_results:
        if isinstance(result, Exception):
            continue
        for q in result:
            mids_by_symbol.setdefault(q.symbol, []).append(q.mid)
    spot_mids = {symbol: sum(mids) / len(mids) for symbol, mids in mids_by_symbol.items()}

    opportunities = []
    if pc_cfg.complement.enabled:
        opportunities.extend(
            detect_complement_arbitrage(
                poly_quotes,
                min_edge_pct=pc_cfg.complement.min_edge_pct,
                max_edge_pct=pc_cfg.complement.max_edge_pct,
                fee_buffer_pct=pc_cfg.complement.fee_buffer_pct,
                min_liquidity_usd=pc_cfg.min_liquidity_usd,
                max_spread_cents=pc_cfg.max_spread_cents,
            )
        )
    if pc_cfg.spot_anchor.enabled and spot_mids:
        opportunities.extend(
            detect_spot_anchored_gaps(
                poly_quotes,
                spot_mids,
                SpotAnchorConfig(
                    min_edge_pct=pc_cfg.spot_anchor.min_edge_pct,
                    max_edge_pct=pc_cfg.spot_anchor.max_edge_pct,
                    min_confidence=pc_cfg.spot_anchor.min_confidence,
                    max_days_to_expiry=pc_cfg.spot_anchor.max_days_to_expiry,
                    annualized_vol=pc_cfg.spot_anchor.annualized_vol,
                    default_annualized_vol=pc_cfg.spot_anchor.default_annualized_vol,
                ),
                min_liquidity_usd=pc_cfg.min_liquidity_usd,
                max_spread_cents=pc_cfg.max_spread_cents,
            )
        )

    for opp in opportunities:
        strategy = opp.metadata.get("strategy", "complement")
        question = str(opp.metadata.get("question", ""))[:120]

        def _skip(reason: str, opp=opp, strategy=strategy, question=question) -> None:
            logger.info("polycrypto opportunity %s skipped: %s", opp.opportunity_id, reason)
            trade_log.record_opportunity(
                db_conn,
                family="polycrypto",
                strategy=strategy,
                symbol=opp.symbol,
                detail=question,
                edge_pct=opp.edge_pct,
                confidence=opp.confidence,
                status="skipped",
                reason=reason,
            )

        exposure_key = f"polycrypto:{opp.metadata.get('market_id', opp.symbol)}"
        if not throttle.ready(exposure_key):
            _skip("cooldown active (recently traded this market)")
            continue
        exposure_result = exposure_tracker.check_key(exposure_key, pc_cfg.max_concurrent_positions_per_market)
        if not exposure_result.passed:
            _skip(exposure_result.reason)
            continue

        stake_usd = size_polycrypto_opportunity(
            opp,
            bankroll_usd=cfg.risk.bankroll_usd,
            max_stake_per_trade_pct=pc_cfg.max_stake_per_trade_pct,
            max_stake_per_trade_usd=pc_cfg.max_stake_per_trade_usd,
            kelly_fraction_cap=pc_cfg.spot_anchor.kelly_fraction,
        )
        if stake_usd < cfg.risk.min_stake_usd:
            _skip(f"sized stake ${stake_usd:.2f} below min_stake_usd")
            continue

        can_trade, reason = risk_manager.can_trade(stake_usd)
        if not can_trade:
            _skip(reason)
            continue

        position = await order_manager.execute_opportunity(opp, stake_usd)
        exposure_tracker.on_open_key(exposure_key)
        risk_manager.on_position_opened(stake_usd)
        throttle.fire(exposure_key)

        # Complement trades settle at fill time (every YES+NO pair pays $1
        # regardless of resolution), so release exposure immediately like
        # crypto cross-exchange arb. Spot-anchor positions stay open until
        # the market actually resolves.
        if position.guaranteed_profit_usd is not None:
            risk_manager.on_position_closed(stake_usd, position.guaranteed_profit_usd)
            exposure_tracker.on_close_key(exposure_key)

        latency_ms = (position.orders[0].submitted_at - opp.created_at) * 1000.0 if position.orders else 0.0

        trade_log.record_opportunity(
            db_conn,
            family="polycrypto",
            strategy=strategy,
            symbol=opp.symbol,
            detail=question,
            edge_pct=opp.edge_pct,
            confidence=opp.confidence,
            status="executed",
        )
        trade_log.record_trade(
            db_conn,
            signal_id=opp.opportunity_id,
            mode=cfg.mode,
            signal_type=f"polycrypto_{strategy}",
            event_id=question or opp.symbol,
            edge_pct=opp.edge_pct,
            confidence=opp.confidence,
            stake_usd=stake_usd,
            locked_in_profit_usd=position.guaranteed_profit_usd,
            latency_ms=latency_ms,
            sportsbook_fraction=0.0,
            polymarket_fraction=1.0,
            market_family="polycrypto",
        )

        log_event(
            logger,
            logging.INFO,
            "polycrypto opportunity executed",
            opportunity_id=opp.opportunity_id,
            strategy=strategy,
            question=question,
            edge_pct=round(opp.edge_pct, 3),
            confidence=opp.confidence,
            stake_usd=round(stake_usd, 2),
            locked_in_profit_usd=position.guaranteed_profit_usd,
        )

    return len(opportunities)


def _build_data_sources(
    cfg: AppConfig, secrets: Secrets
) -> tuple[OddsProvider, PolymarketDataClient, bool, bool]:
    """Returns (odds_provider, polymarket_client, use_mock, sports_enabled).

    Synthetic data is used ONLY when explicitly requested with
    ARBBOT_USE_MOCK_DATA=true. Otherwise everything runs on REAL data:
    Polymarket (Gamma + CLOB) and the crypto exchanges expose public,
    key-free price feeds, so the crypto and polycrypto desks always run live.
    The sports desk is the sole exception -- it needs a paid odds feed, so
    without ODDS_API_KEY it is DISABLED rather than fed fabricated data (a
    missing key must never silently turn real observation into a simulation).
    """
    if os.getenv("ARBBOT_USE_MOCK_DATA", "").lower() == "true":
        logger.warning("ARBBOT_USE_MOCK_DATA=true -- ALL desks run on synthetic data (demo/offline only)")
        return MockOddsProvider(), MockPolymarketClient(), True, True

    poly_client = ClobMarketDataClient()
    if secrets.odds_api_key:
        return TheOddsApiProvider(api_key=secrets.odds_api_key), poly_client, False, True

    logger.warning(
        "No ODDS_API_KEY set -- running REAL market data for the crypto and "
        "Polymarket desks; the SPORTS desk is DISABLED (it needs a paid odds "
        "feed and this build will not fabricate sports data). Set ODDS_API_KEY "
        "to enable it."
    )
    return MockOddsProvider(), poly_client, False, False


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
    poller: AdaptivePoller,
    db_conn,
) -> None:
    while True:
        cycle_start = time.perf_counter()
        found = 0
        try:
            found = await _run_cycle(
                cfg, odds_provider, poly_client, exposure_tracker, risk_manager, order_manager, metrics, filter_cfg, db_conn
            )
        except Exception:  # noqa: BLE001 - keep the polling loop alive across transient errors
            logger.exception("unhandled error in sports polling cycle")
        if metrics.total_trades and metrics.total_trades % 10 == 0:
            log_event(logger, logging.INFO, "metrics snapshot", **metrics.summary())
        poll_interval = poller.on_cycle(found)
        elapsed = time.perf_counter() - cycle_start
        await asyncio.sleep(max(0.0, poll_interval - elapsed))


async def _crypto_loop(
    cfg: AppConfig,
    price_feeds: list[CryptoPriceFeed],
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: CryptoOrderManager,
    poller: AdaptivePoller,
    db_conn,
) -> None:
    throttle = SignalThrottle(cooldown_sec=cfg.markets.crypto.cooldown_sec)
    while True:
        cycle_start = time.perf_counter()
        found = 0
        try:
            found = await _run_crypto_cycle(
                cfg, price_feeds, exposure_tracker, risk_manager, order_manager, throttle, db_conn
            )
        except Exception:  # noqa: BLE001 - keep the polling loop alive across transient errors
            logger.exception("unhandled error in crypto polling cycle")
        throttle.prune()
        poll_interval = poller.on_cycle(found)
        elapsed = time.perf_counter() - cycle_start
        await asyncio.sleep(max(0.0, poll_interval - elapsed))


async def _polycrypto_loop(
    cfg: AppConfig,
    poly_feed: PolymarketCryptoFeed,
    spot_feeds: list[CryptoPriceFeed],
    exposure_tracker: ExposureTracker,
    risk_manager: RiskManager,
    order_manager: PolycryptoOrderManager,
    poller: AdaptivePoller,
    db_conn,
) -> None:
    throttle = SignalThrottle(cooldown_sec=cfg.markets.polymarket_crypto.cooldown_sec)
    while True:
        cycle_start = time.perf_counter()
        found = 0
        try:
            found = await _run_polycrypto_cycle(
                cfg, poly_feed, spot_feeds, exposure_tracker, risk_manager, order_manager, throttle, db_conn
            )
        except Exception:  # noqa: BLE001 - keep the polling loop alive across transient errors
            logger.exception("unhandled error in polycrypto polling cycle")
        throttle.prune()
        poll_interval = poller.on_cycle(found)
        elapsed = time.perf_counter() - cycle_start
        await asyncio.sleep(max(0.0, poll_interval - elapsed))


async def _heartbeat_loop(
    db_conn, risk_manager: RiskManager, pollers: dict[str, AdaptivePoller], interval_sec: float = 5.0
) -> None:
    while True:
        trade_log.record_heartbeat(
            db_conn,
            trading_halted=risk_manager.state.trading_halted,
            halt_reason=risk_manager.state.halt_reason,
            open_exposure_usd=risk_manager.state.open_exposure_usd,
            poll_intervals={name: round(p.current_interval_sec, 3) for name, p in pollers.items()},
        )
        await asyncio.sleep(interval_sec)


def _apply_conservative_mode(cfg: AppConfig) -> None:
    """Win-rate-first override: force every directional/statistical layer off
    so only the riskless arbitrage layers can trade. Mutates cfg in place;
    logged loudly so it's obvious in the startup output which layers are live.
    """
    if not cfg.conservative_mode:
        return
    disabled = []
    if cfg.strategy.value_edge.enabled:
        cfg.strategy.value_edge.enabled = False
        disabled.append("sports value_edge")
    if cfg.markets.polymarket_crypto.spot_anchor.enabled:
        cfg.markets.polymarket_crypto.spot_anchor.enabled = False
        disabled.append("polycrypto spot_anchor")
    logger.warning(
        "conservative_mode ON -- riskless arbitrage layers only%s",
        f" (disabled: {', '.join(disabled)})" if disabled else "",
    )


async def run(cfg: AppConfig, secrets: Secrets) -> None:
    setup_logging(cfg.monitoring.log_level)
    _apply_conservative_mode(cfg)

    odds_provider, poly_client, use_mock, sports_enabled = _build_data_sources(cfg, secrets)
    if not cfg.sports:
        sports_enabled = False
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

    def _make_poller(base_interval_sec: float) -> AdaptivePoller:
        return AdaptivePoller(
            base_interval_sec=base_interval_sec,
            burst_interval_sec=cfg.polling.burst_interval_sec,
            decay=cfg.polling.burst_decay,
        )

    pollers: dict[str, AdaptivePoller] = {}
    tasks: list[asyncio.Task] = []
    if sports_enabled:
        pollers["sports"] = _make_poller(
            min(cfg.polling.odds_poll_interval_sec, cfg.polling.polymarket_poll_interval_sec)
        )
        tasks.append(
            asyncio.create_task(
                _sports_loop(
                    cfg,
                    odds_provider,
                    poly_client,
                    exposure_tracker,
                    risk_manager,
                    order_manager,
                    metrics,
                    filter_cfg,
                    pollers["sports"],
                    db_conn,
                )
            )
        )
    else:
        logger.info("sports desk not running (no odds feed) -- crypto + Polymarket desks only")

    crypto_feeds: list[CryptoPriceFeed] = []
    needs_spot_feeds = cfg.markets.crypto.enabled or (
        cfg.markets.polymarket_crypto.enabled and cfg.markets.polymarket_crypto.spot_anchor.enabled
    )
    if needs_spot_feeds:
        crypto_feeds = _build_crypto_price_feeds(cfg, use_mock)

    if cfg.markets.crypto.enabled:
        crypto_exec_clients = _build_crypto_execution_clients(cfg, secrets, crypto_feeds)
        crypto_order_manager = CryptoOrderManager(
            client_for_exchange=crypto_exec_clients,
            order_timeout_sec=cfg.execution.order_timeout_sec,
            max_retries=cfg.execution.max_retries,
        )
        pollers["crypto"] = _make_poller(cfg.markets.crypto.poll_interval_sec)
        tasks.append(
            asyncio.create_task(
                _crypto_loop(
                    cfg, crypto_feeds, exposure_tracker, risk_manager, crypto_order_manager, pollers["crypto"], db_conn
                )
            )
        )

    poly_crypto_feed: PolymarketCryptoFeed | None = None
    if cfg.markets.polymarket_crypto.enabled:
        poly_crypto_feed = (
            MockPolymarketCryptoFeed()
            if use_mock
            else PolymarketCryptoDataClient(market_limit=cfg.markets.polymarket_crypto.market_limit)
        )
        # Both polycrypto strategies execute only Polymarket legs, so they
        # share the sports pipeline's Polymarket execution client (paper in
        # paper mode, py-clob-client behind the dual opt-in when live).
        polycrypto_order_manager = PolycryptoOrderManager(
            client=polymarket_exec,
            order_timeout_sec=cfg.execution.order_timeout_sec,
            max_retries=cfg.execution.max_retries,
        )
        pollers["polycrypto"] = _make_poller(cfg.markets.polymarket_crypto.poll_interval_sec)
        tasks.append(
            asyncio.create_task(
                _polycrypto_loop(
                    cfg,
                    poly_crypto_feed,
                    crypto_feeds,
                    exposure_tracker,
                    risk_manager,
                    polycrypto_order_manager,
                    pollers["polycrypto"],
                    db_conn,
                )
            )
        )

    tasks.append(asyncio.create_task(_heartbeat_loop(db_conn, risk_manager, pollers)))

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await odds_provider.close()
        await poly_client.close()
        for feed in crypto_feeds:
            await feed.close()
        if poly_crypto_feed is not None:
            await poly_crypto_feed.close()
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
