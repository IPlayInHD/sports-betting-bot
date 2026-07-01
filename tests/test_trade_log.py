from __future__ import annotations

from arbbot.monitoring import trade_log


def test_record_and_read_startup_status(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = trade_log.get_connection(db_path)

    trade_log.record_startup(conn, mode="paper", use_mock=True, bankroll_usd=1000.0)
    row = conn.execute("SELECT * FROM status WHERE id = 1").fetchone()

    assert row["mode"] == "paper"
    assert row["use_mock"] == 1
    assert row["bankroll_usd"] == 1000.0
    assert row["trading_halted"] == 0


def test_record_heartbeat_updates_status(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = trade_log.get_connection(db_path)
    trade_log.record_startup(conn, mode="paper", use_mock=True, bankroll_usd=1000.0)

    trade_log.record_heartbeat(conn, trading_halted=True, halt_reason="daily loss limit", open_exposure_usd=42.5)
    row = conn.execute("SELECT * FROM status WHERE id = 1").fetchone()

    assert row["trading_halted"] == 1
    assert row["halt_reason"] == "daily loss limit"
    assert row["open_exposure_usd"] == 42.5


def test_record_trade_and_query(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = trade_log.get_connection(db_path)

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

    rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["signal_id"] == "sig-1"
    assert row["locked_in_profit_usd"] == 0.38
    assert row["stake_usd"] == 15.0


def test_record_trade_upserts_on_duplicate_signal_id(tmp_path):
    db_path = tmp_path / "trades.db"
    conn = trade_log.get_connection(db_path)

    common = dict(
        conn=conn,
        signal_id="sig-1",
        mode="paper",
        signal_type="arbitrage",
        event_id="evt-1",
        edge_pct=2.5,
        confidence=0.8,
        stake_usd=15.0,
        latency_ms=12.4,
        sportsbook_fraction=0.4,
        polymarket_fraction=0.6,
    )
    trade_log.record_trade(**common, locked_in_profit_usd=None)
    trade_log.record_trade(**common, locked_in_profit_usd=0.38)

    rows = conn.execute("SELECT * FROM trades").fetchall()
    assert len(rows) == 1
    assert rows[0]["locked_in_profit_usd"] == 0.38
