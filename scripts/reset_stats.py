"""Reset the dashboard figures back to zero.

Deletes the local SQLite trade log (data/trades.db by default, or whatever
ARBBOT_DB_PATH points at). Every dashboard number -- net P&L, stake deployed,
trades, win rate, the opportunity blotter -- is derived from that one file, so
removing it wipes the slate clean. The bot recreates an empty database the
next time it starts.

Stop the bot first (Ctrl+C in its terminal), run this, then start the bot
again. Nothing here touches your config or code -- only the recorded history.
"""

from __future__ import annotations

import os
from pathlib import Path

from arbbot.monitoring import trade_log


def main() -> None:
    db_path = Path(os.environ.get("ARBBOT_DB_PATH", str(trade_log.DEFAULT_DB_PATH)))
    if db_path.exists():
        db_path.unlink()
        print(f"Reset complete -- deleted {db_path}. All figures are back to $0.")
        print("Start the bot again (python scripts/run_paper.py) to begin a fresh run.")
    else:
        print(f"Nothing to reset -- no trade log found at {db_path}. Figures are already empty.")


if __name__ == "__main__":
    main()
