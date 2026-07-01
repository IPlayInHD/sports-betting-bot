#!/usr/bin/env python3
"""CLI: generate synthetic historical data and run it through the full
detection/filter/sizing pipeline to sanity-check strategy behavior before
paper or live trading.

Usage:
    python scripts/run_backtest.py --events 500 --config config/config.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arbbot.backtest.engine import run_backtest  # noqa: E402
from arbbot.backtest.synthetic_data import generate_synthetic_events  # noqa: E402
from arbbot.config import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a synthetic-data backtest")
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    events = generate_synthetic_events(n_events=args.events, seed=args.seed)
    result = run_backtest(events, cfg)

    print(json.dumps(result.summary(), indent=2))


if __name__ == "__main__":
    main()
