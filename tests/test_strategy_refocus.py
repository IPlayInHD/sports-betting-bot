"""Tests for the strategy-refocus changes: crypto desk observation-only, and
Polymarket complement scanning all binary markets rather than just crypto.
"""

from __future__ import annotations

import pytest

from arbbot.config import AppConfig
from arbbot.main import _run_crypto_cycle
from arbbot.markets.crypto.exchanges import CryptoPriceFeed
from arbbot.markets.crypto.execution import CryptoOrderManager, PaperCryptoExecutionClient
from arbbot.markets.polycrypto.data import PolymarketCryptoDataClient
from arbbot.models import CryptoQuote
from arbbot.monitoring import trade_log
from arbbot.risk.risk_manager import RiskLimits, RiskManager
from arbbot.strategy.adaptive import SignalThrottle
from arbbot.strategy.filters import ExposureTracker


class _GapFeed(CryptoPriceFeed):
    """Two exchanges with a clean, fee-clearing gap so a detection is guaranteed."""

    exchange_name = "gapfeed"

    async def fetch_quotes(self, symbols):
        out = []
        for s in symbols:
            out.append(CryptoQuote(exchange="venue_a", symbol=s, bid=99.0, ask=100.0))
            out.append(CryptoQuote(exchange="venue_b", symbol=s, bid=102.0, ask=103.0))
        return out


def _crypto_ctx(tmp_path):
    conn = trade_log.get_connection(tmp_path / "trades.db")
    risk = RiskManager(RiskLimits(bankroll_usd=100.0, max_stake_per_trade_pct=4.0, max_stake_per_trade_usd=25.0))
    order_mgr = CryptoOrderManager(client_for_exchange={"venue_a": PaperCryptoExecutionClient(), "venue_b": PaperCryptoExecutionClient()})
    return conn, ExposureTracker(), risk, order_mgr, SignalThrottle(cooldown_sec=0.0)


async def test_observation_only_logs_but_never_trades(tmp_path):
    cfg = AppConfig()
    cfg.markets.crypto.observation_only = True
    cfg.markets.crypto.symbols = ["BTC/USD"]
    conn, exposure, risk, order_mgr, throttle = _crypto_ctx(tmp_path)

    found = await _run_crypto_cycle(cfg, [_GapFeed()], exposure, risk, order_mgr, throttle, conn)

    assert found == 1
    # No trades recorded...
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    # ...but the opportunity is logged as skipped with the honest reason.
    rows = conn.execute("SELECT status, reason FROM opportunities WHERE family='crypto'").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert "observation only" in rows[0]["reason"]


async def test_trading_mode_still_executes_crypto(tmp_path):
    cfg = AppConfig()
    cfg.markets.crypto.observation_only = False
    cfg.markets.crypto.symbols = ["BTC/USD"]
    conn, exposure, risk, order_mgr, throttle = _crypto_ctx(tmp_path)

    await _run_crypto_cycle(cfg, [_GapFeed()], exposure, risk, order_mgr, throttle, conn)

    # With observation_only off, the fee-clearing gap is actually traded.
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE market_family='crypto'").fetchone()[0] == 1


class _RecordingClob:
    """Captures which tag the data client asks for."""

    def __init__(self):
        self.calls = []

    async def fetch_markets_by_tag(self, tag, limit=200):
        self.calls.append(tag)
        return []


async def test_scan_all_markets_requests_untagged_feed():
    clob = _RecordingClob()
    client = PolymarketCryptoDataClient(clob=clob, scan_all_markets=True)
    await client.fetch_crypto_markets()
    assert clob.calls == [None]  # None => all active markets, not a single tag


async def test_tag_restricted_mode_requests_each_tag():
    clob = _RecordingClob()
    client = PolymarketCryptoDataClient(clob=clob, scan_all_markets=False, tags=["crypto", "politics"])
    await client.fetch_crypto_markets()
    assert clob.calls == ["crypto", "politics"]


def test_refocus_defaults():
    cfg = AppConfig()
    assert cfg.markets.crypto.observation_only is True
    assert cfg.markets.polymarket_crypto.scan_all_markets is True
    # These changes must not disturb the win-rate-first stance or capital.
    assert cfg.conservative_mode is True
    assert cfg.risk.bankroll_usd == 100
