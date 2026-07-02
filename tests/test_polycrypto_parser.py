from __future__ import annotations

import pytest

from arbbot.markets.polycrypto.parser import ThresholdKind, parse_threshold_question


@pytest.mark.parametrize(
    ("question", "symbol", "threshold", "kind"),
    [
        ("Will Bitcoin be above $70,000 on July 31?", "BTC/USD", 70_000.0, ThresholdKind.TERMINAL_ABOVE),
        ("Will ETH be below $3,000 on December 31?", "ETH/USD", 3_000.0, ThresholdKind.TERMINAL_BELOW),
        ("Will Solana hit $500 by the end of 2026?", "SOL/USD", 500.0, ThresholdKind.TOUCH),
        ("Will Bitcoin reach $100k in 2026?", "BTC/USD", 100_000.0, ThresholdKind.TOUCH),
        ("Will Ethereum dip to $2,500 before March?", "ETH/USD", 2_500.0, ThresholdKind.TOUCH),
        ("Will Dogecoin be above $0.50 on Friday?", "DOGE/USD", 0.50, ThresholdKind.TERMINAL_ABOVE),
        ("Bitcoin over $65,000 at expiry?", "BTC/USD", 65_000.0, ThresholdKind.TERMINAL_ABOVE),
    ],
)
def test_parses_threshold_questions(question, symbol, threshold, kind):
    market = parse_threshold_question(question)
    assert market is not None
    assert market.symbol == symbol
    assert market.threshold_usd == pytest.approx(threshold)
    assert market.kind == kind


@pytest.mark.parametrize(
    "question",
    [
        "Will the Lakers beat the Celtics?",                      # not a crypto asset
        "Will Bitcoin dominance be above 60%?",                   # no $ threshold
        "Will a spot Solana ETF be approved this year?",          # no threshold at all
        "Will Bitcoin outperform Ethereum this quarter?",         # ratio market, no strike
    ],
)
def test_rejects_unparseable_questions(question):
    assert parse_threshold_question(question) is None


def test_touch_takes_precedence_over_above_phrasing():
    market = parse_threshold_question("Will Bitcoin reach above $90,000 by June?")
    assert market is not None
    assert market.kind == ThresholdKind.TOUCH
