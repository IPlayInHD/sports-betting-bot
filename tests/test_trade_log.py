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


def test_record_heartbeat_stores_poll_intervals(tmp_path):
    conn = trade_log.get_connection(tmp_path / "trades.db")
    trade_log.record_startup(conn, mode="paper", use_mock=True, bankroll_usd=50.0)

    trade_log.record_heartbeat(
        conn,
        trading_halted=False,
        halt_reason="",
        open_exposure_usd=0.0,
        poll_intervals={"crypto": 0.4, "polycrypto": 1.5},
    )
    row = conn.execute("SELECT poll_intervals FROM status WHERE id = 1").fetchone()
    assert '"crypto": 0.4' in row["poll_intervals"]


def test_record_opportunity_and_query(tmp_path):
    conn = trade_log.get_connection(tmp_path / "trades.db")

    trade_log.record_opportunity(
        conn,
        family="polycrypto",
        strategy="complement",
        symbol="Will Bitcoin be above $70,000?",
        detail="Will Bitcoin be above $70,000?",
        edge_pct=1.2,
        confidence=0.9,
        status="skipped",
        reason="cooldown active",
    )

    rows = conn.execute("SELECT * FROM opportunities").fetchall()
    assert len(rows) == 1
    assert rows[0]["family"] == "polycrypto"
    assert rows[0]["status"] == "skipped"
    assert rows[0]["reason"] == "cooldown active"


def test_opportunity_table_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(trade_log, "_MAX_OPPORTUNITY_ROWS", 5)
    conn = trade_log.get_connection(tmp_path / "trades.db")

    for i in range(12):
        trade_log.record_opportunity(
            conn,
            family="crypto",
            strategy="cross_exchange",
            symbol=f"SYM-{i}",
            detail="",
            edge_pct=0.5,
            confidence=1.0,
            status="executed",
        )

    rows = conn.execute("SELECT symbol FROM opportunities ORDER BY id").fetchall()
    assert len(rows) == 5
    assert rows[0]["symbol"] == "SYM-7"  # oldest rows pruned first


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
