from __future__ import annotations

import pytest

from arbbot.odds.normalization import (
    american_to_decimal,
    decimal_to_american,
    decimal_to_implied_prob,
    devig_multiplicative,
    devig_power,
    implied_prob_to_decimal,
    overround_pct,
)


def test_american_to_decimal_positive():
    assert american_to_decimal(150) == pytest.approx(2.5)


def test_american_to_decimal_negative():
    assert american_to_decimal(-150) == pytest.approx(1.6667, rel=1e-3)


def test_decimal_american_round_trip():
    for decimal in (1.5, 2.0, 3.25, 1.05):
        american = decimal_to_american(decimal)
        assert decimal_to_american(decimal) == pytest.approx(american)
        assert american_to_decimal(american) == pytest.approx(decimal, rel=1e-6)


def test_decimal_to_implied_prob():
    assert decimal_to_implied_prob(2.0) == pytest.approx(0.5)
    assert decimal_to_implied_prob(4.0) == pytest.approx(0.25)


def test_implied_prob_to_decimal_inverse():
    assert implied_prob_to_decimal(0.25) == pytest.approx(4.0)


def test_devig_multiplicative_symmetric():
    result = devig_multiplicative([0.55, 0.55])
    assert result == pytest.approx([0.5, 0.5])
    assert sum(result) == pytest.approx(1.0)


def test_devig_power_symmetric_matches_multiplicative():
    result = devig_power([0.55, 0.55])
    assert result == pytest.approx([0.5, 0.5], abs=1e-4)
    assert sum(result) == pytest.approx(1.0, abs=1e-6)


def test_devig_power_sums_to_one_asymmetric():
    result = devig_power([0.45, 0.35, 0.30])
    assert sum(result) == pytest.approx(1.0, abs=1e-6)
    assert all(0.0 < p < 1.0 for p in result)


def test_overround_pct():
    assert overround_pct([0.55, 0.55]) == pytest.approx(10.0)
    assert overround_pct([0.5, 0.5]) == pytest.approx(0.0)
