"""SQLite-backed trade/status logging.

The dashboard runs as a separate process from the bot and has no access to
its in-memory state, so the bot writes every executed trade and a periodic
heartbeat/status snapshot to a local SQLite file, and the dashboard just
reads from it. Stdlib-only (sqlite3) -- no new runtime dependency for the
bot process itself.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path("data/trades.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    signal_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    event_id TEXT,
    edge_pct REAL,
    confidence REAL,
    stake_usd REAL,
    locked_in_profit_usd REAL,
    latency_ms REAL,
    sportsbook_fraction REAL,
    polymarket_fraction REAL,
    market_family TEXT NOT NULL DEFAULT 'sports'
);

CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT,
    use_mock INTEGER,
    started_at REAL,
    last_heartbeat REAL,
    trading_halted INTEGER,
    halt_reason TEXT,
    open_exposure_usd REAL,
    bankroll_usd REAL,
    poll_intervals TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    family TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT,
    detail TEXT,
    edge_pct REAL,
    confidence REAL,
    status TEXT NOT NULL,   -- 'executed' | 'skipped'
    reason TEXT
);
"""

# The opportunity feed is diagnostic, not an accounting record like trades:
# cap its size so a bot left running for weeks doesn't grow the DB unbounded.
_MAX_OPPORTUNITY_ROWS = 2000


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migrations for db files created before newer columns existed:
    # CREATE TABLE IF NOT EXISTS won't retroactively add a new column.
    for migration in (
        "ALTER TABLE trades ADD COLUMN market_family TEXT NOT NULL DEFAULT 'sports'",
        "ALTER TABLE status ADD COLUMN poll_intervals TEXT",
    ):
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def record_startup(conn: sqlite3.Connection, mode: str, use_mock: bool, bankroll_usd: float) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO status (id, mode, use_mock, started_at, last_heartbeat, trading_halted, halt_reason, open_exposure_usd, bankroll_usd)
        VALUES (1, ?, ?, ?, ?, 0, '', 0.0, ?)
        ON CONFLICT(id) DO UPDATE SET
            mode = excluded.mode,
            use_mock = excluded.use_mock,
            started_at = excluded.started_at,
            last_heartbeat = excluded.last_heartbeat,
            trading_halted = 0,
            halt_reason = '',
            open_exposure_usd = 0.0,
            bankroll_usd = excluded.bankroll_usd
        """,
        (mode, int(use_mock), now, now, bankroll_usd),
    )
    conn.commit()


def record_heartbeat(
    conn: sqlite3.Connection,
    trading_halted: bool,
    halt_reason: str,
    open_exposure_usd: float,
    poll_intervals: dict[str, float] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE status SET last_heartbeat = ?, trading_halted = ?, halt_reason = ?, open_exposure_usd = ?,
            poll_intervals = ?
        WHERE id = 1
        """,
        (
            time.time(),
            int(trading_halted),
            halt_reason,
            open_exposure_usd,
            json.dumps(poll_intervals) if poll_intervals else None,
        ),
    )
    conn.commit()


def record_opportunity(
    conn: sqlite3.Connection,
    family: str,
    strategy: str,
    symbol: str,
    detail: str,
    edge_pct: float,
    confidence: float,
    status: str,
    reason: str = "",
) -> None:
    """Log every detected opportunity -- executed or skipped, with the skip
    reason -- so the dashboard can show what the bot is seeing and why it
    did (or didn't) act, not just the trades that made it through.
    """
    conn.execute(
        """
        INSERT INTO opportunities (ts, family, strategy, symbol, detail, edge_pct, confidence, status, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (time.time(), family, strategy, symbol, detail, edge_pct, confidence, status, reason),
    )
    conn.execute(
        "DELETE FROM opportunities WHERE id <= (SELECT MAX(id) FROM opportunities) - ?",
        (_MAX_OPPORTUNITY_ROWS,),
    )
    conn.commit()


def record_trade(
    conn: sqlite3.Connection,
    signal_id: str,
    mode: str,
    signal_type: str,
    event_id: str,
    edge_pct: float,
    confidence: float,
    stake_usd: float,
    locked_in_profit_usd: float | None,
    latency_ms: float,
    sportsbook_fraction: float,
    polymarket_fraction: float,
    market_family: str = "sports",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO trades (
            signal_id, ts, mode, signal_type, event_id, edge_pct, confidence, stake_usd,
            locked_in_profit_usd, latency_ms, sportsbook_fraction, polymarket_fraction, market_family
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            time.time(),
            mode,
            signal_type,
            event_id,
            edge_pct,
            confidence,
            stake_usd,
            locked_in_profit_usd,
            latency_ms,
            sportsbook_fraction,
            polymarket_fraction,
            market_family,
        ),
    )
    conn.commit()
