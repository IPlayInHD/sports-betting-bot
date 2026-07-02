from __future__ import annotations

import pytest

from arbbot.markets.polycrypto.parser import ThresholdKind, ThresholdMarket
from arbbot.markets.polycrypto.pricing import (
    distance_in_sigmas,
    model_yes_probability,
    normal_cdf,
    terminal_above_probability,
    touch_probability,
)


def test_normal_cdf_reference_points():
    assert normal_cdf(0.0) == pytest.approx(0.5)
    assert normal_cdf(1.6449) == pytest.approx(0.95, abs=1e-3)
    assert normal_cdf(-1.6449) == pytest.approx(0.05, abs=1e-3)


def test_terminal_probability_far_above_threshold_is_near_one():
    # Spot double the threshold with a week to go: effectively resolved.
    p = terminal_above_probability(spot=100_000, threshold=50_000, t_years=7 / 365, annualized_vol=0.6)
    assert p > 0.99


def test_terminal_probability_far_below_threshold_is_near_zero():
    p = terminal_above_probability(spot=50_000, threshold=100_000, t_years=7 / 365, annualized_vol=0.6)
    assert p < 0.01


def test_terminal_probability_monotone_in_spot():
    ps = [
        terminal_above_probability(spot=s, threshold=70_000, t_years=30 / 365, annualized_vol=0.6)
        for s in (50_000, 60_000, 70_000, 80_000, 90_000)
    ]
    assert ps == sorted(ps)


def test_terminal_probability_at_zero_time_is_indicator():
    assert terminal_above_probability(70_001, 70_000, 0.0, 0.6) == 1.0
    assert terminal_above_probability(69_999, 70_000, 0.0, 0.6) == 0.0


def test_touch_probability_is_roughly_twice_terminal_and_capped():
    spot, threshold, t, vol = 65_000, 80_000, 30 / 365, 0.6
    p_terminal = terminal_above_probability(spot, threshold, t, vol)
    p_touch = touch_probability(spot, threshold, t, vol)
    assert p_touch == pytest.approx(min(1.0, 2 * p_terminal))
    assert 0.0 <= p_touch <= 1.0


def test_touch_probability_downside_barrier():
    # Barrier below spot: touching is more likely than finishing below.
    spot, threshold, t, vol = 65_000, 55_000, 30 / 365, 0.6
    p_finish_below = 1.0 - terminal_above_probability(spot, threshold, t, vol)
    assert touch_probability(spot, threshold, t, vol) == pytest.approx(min(1.0, 2 * p_finish_below))


def test_model_yes_probability_respects_kind():
    above = ThresholdMarket("BTC/USD", 70_000, ThresholdKind.TERMINAL_ABOVE)
    below = ThresholdMarket("BTC/USD", 70_000, ThresholdKind.TERMINAL_BELOW)
    p_above = model_yes_probability(above, spot=65_000, t_years=30 / 365, annualized_vol=0.6)
    p_below = model_yes_probability(below, spot=65_000, t_years=30 / 365, annualized_vol=0.6)
    assert p_above + p_below == pytest.approx(1.0)


def test_distance_in_sigmas_grows_with_gap_and_shrinks_with_time():
    near = distance_in_sigmas(65_000, 66_000, 30 / 365, 0.6)
    far = distance_in_sigmas(65_000, 90_000, 30 / 365, 0.6)
    assert far > near
    longer_horizon = distance_in_sigmas(65_000, 90_000, 365 / 365, 0.6)
    assert longer_horizon < far
