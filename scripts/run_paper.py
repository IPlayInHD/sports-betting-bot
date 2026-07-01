#!/usr/bin/env python3
"""CLI: start the bot in paper-trading mode using either real (if
ODDS_API_KEY is set) or synthetic sportsbook data, and Polymarket's public
read-only market data (or synthetic, if ARBBOT_USE_MOCK_DATA=true).

This script always forces mode=paper regardless of config.yaml, so it is
safe to run without any credentials -- it never places a real order.

Usage:
    python scripts/run_paper.py --config config/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arbbot.config import load_config, load_secrets  # noqa: E402
from arbbot.main import run  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bot in paper-trading mode")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg.mode = "paper"  # hard override: this script never trades live
    secrets = load_secrets()

    asyncio.run(run(cfg, secrets))


if __name__ == "__main__":
    main()
