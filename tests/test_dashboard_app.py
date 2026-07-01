from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

from arbbot.monitoring import trade_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "trades.db"
    monkeypatch.setenv("ARBBOT_DB_PATH", str(db_path))

    from arbbot.dashboard import app as dashboard_app

    importlib.reload(dashboard_app)  # picks up the env var via _db_path()
    return TestClient(dashboard_app.app), db_path


def test_status_reports_not_running_when_no_data(client):
    test_client, _ = client
    resp = test_client.get("/api/status")
    assert resp.status_code == 200
    assert resp.json() == {"running": False}


def test_status_reports_running_and_mode(client):
    test_client, db_path = client
    conn = trade_log.get_connection(db_path)
    trade_log.record_startup(conn, mode="paper", use_mock=True, bankroll_usd=1000.0)

    resp = test_client.get("/api/status")
    body = resp.json()
    assert body["running"] is True
    assert body["mode"] == "paper"
    assert body["use_mock_data"] is True


def test_summary_and_trades_endpoints(client):
    test_client, db_path = client
    conn = trade_log.get_connection(db_path)
    trade_log.record_startup(conn, mode="paper", use_mock=True, bankroll_usd=1000.0)
    trade_log.record_trade(
        conn,
        signal_id="sig-1",
        mode="paper",
        signal_type="arbitrage",
        event_id="evt-1",
        edge_pct=2.5,
        confidence=0.8,
        stake_usd=15.0,
        locked_in_profit_usd=0.38,
        latency_ms=12.4,
        sportsbook_fraction=0.4,
        polymarket_fraction=0.6,
    )

    summary = test_client.get("/api/summary").json()
    assert summary["total_trades"] == 1
    assert summary["win_rate"] == 1.0
    assert summary["total_locked_in_profit_usd"] == pytest.approx(0.38)

    trades = test_client.get("/api/trades").json()
    assert len(trades) == 1
    assert trades[0]["signal_id"] == "sig-1"

    series = test_client.get("/api/pnl_timeseries").json()
    assert series == [{"ts": series[0]["ts"], "cumulative_pnl_usd": 0.38}]


def test_index_serves_html(client):
    test_client, _ = client
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert "arbbot" in resp.text.lower()
