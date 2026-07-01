"""Read-only performance dashboard.

Runs as a separate process from the bot itself and reads from the same
local SQLite file the bot writes to (monitoring/trade_log.py). Start the
bot (scripts/run_paper.py or scripts/run_backtest.py won't populate this --
only a running bot does) in one terminal and this dashboard in another; it
never talks to the bot's execution layer directly and cannot place trades.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from arbbot.monitoring import trade_log

STATIC_DIR = Path(__file__).parent / "static"
HEARTBEAT_STALE_AFTER_SEC = 15.0

app = FastAPI(title="arbbot dashboard")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _db_path() -> Path:
    """Allows tests (and users who want a non-default location) to point the
    dashboard at a specific SQLite file via ARBBOT_DB_PATH.
    """
    override = os.environ.get("ARBBOT_DB_PATH")
    return Path(override) if override else trade_log.DEFAULT_DB_PATH


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
def status() -> dict:
    conn = trade_log.get_connection(_db_path())
    row = conn.execute("SELECT * FROM status WHERE id = 1").fetchone()
    conn.close()

    if row is None:
        return {"running": False}

    is_stale = (time.time() - row["last_heartbeat"]) > HEARTBEAT_STALE_AFTER_SEC
    return {
        "running": not is_stale,
        "mode": row["mode"],
        "use_mock_data": bool(row["use_mock"]),
        "started_at": row["started_at"],
        "last_heartbeat": row["last_heartbeat"],
        "trading_halted": bool(row["trading_halted"]),
        "halt_reason": row["halt_reason"],
        "open_exposure_usd": row["open_exposure_usd"],
        "bankroll_usd": row["bankroll_usd"],
    }


@app.get("/api/summary")
def summary() -> dict:
    conn = trade_log.get_connection(_db_path())
    rows = conn.execute("SELECT * FROM trades ORDER BY ts ASC").fetchall()
    conn.close()

    total_trades = len(rows)
    settled = [r for r in rows if r["locked_in_profit_usd"] is not None]
    total_locked_in_profit = sum(r["locked_in_profit_usd"] for r in settled)
    total_stake = sum(r["stake_usd"] for r in rows)
    wins = sum(1 for r in settled if r["locked_in_profit_usd"] > 0)
    win_rate = (wins / len(settled)) if settled else None
    avg_edge = (sum(r["edge_pct"] for r in rows) / total_trades) if total_trades else 0.0

    latencies = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)

    def _percentile(p: float) -> float:
        if not latencies:
            return 0.0
        idx = min(len(latencies) - 1, int(len(latencies) * p / 100))
        return latencies[idx]

    by_signal_type: dict[str, int] = {}
    for r in rows:
        by_signal_type[r["signal_type"]] = by_signal_type.get(r["signal_type"], 0) + 1

    return {
        "total_trades": total_trades,
        "settled_trades": len(settled),
        "total_stake_usd": round(total_stake, 2),
        "total_locked_in_profit_usd": round(total_locked_in_profit, 2),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_edge_pct": round(avg_edge, 3),
        "p50_latency_ms": round(_percentile(50), 1),
        "p95_latency_ms": round(_percentile(95), 1),
        "by_signal_type": by_signal_type,
    }


@app.get("/api/trades")
def trades(limit: int = 50) -> list[dict]:
    conn = trade_log.get_connection(_db_path())
    rows = conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/pnl_timeseries")
def pnl_timeseries() -> list[dict]:
    conn = trade_log.get_connection(_db_path())
    rows = conn.execute(
        "SELECT ts, locked_in_profit_usd FROM trades WHERE locked_in_profit_usd IS NOT NULL ORDER BY ts ASC"
    ).fetchall()
    conn.close()

    cumulative = 0.0
    series = []
    for r in rows:
        cumulative += r["locked_in_profit_usd"]
        series.append({"ts": r["ts"], "cumulative_pnl_usd": round(cumulative, 2)})
    return series
