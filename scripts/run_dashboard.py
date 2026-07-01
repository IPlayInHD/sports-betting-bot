#!/usr/bin/env python3
"""CLI: launch the read-only performance dashboard.

Run this in a separate terminal tab/window from the bot itself
(scripts/run_paper.py or `python -m arbbot.main`). It reads from the same
local SQLite file (data/trades.db) the bot writes to, so start the bot
first (or at the same time) to see live data -- this dashboard never talks
to the bot's execution layer directly and cannot place trades.

Usage:
    python scripts/run_dashboard.py --port 8787
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the arbbot dashboard")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"Dashboard running at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run("arbbot.dashboard.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
