from __future__ import annotations

import pytest

from arbbot.strategy.adaptive import AdaptivePoller, SignalThrottle


class TestAdaptivePoller:
    def test_bursts_on_hit_and_decays_back(self):
        poller = AdaptivePoller(base_interval_sec=2.0, burst_interval_sec=0.4, decay=2.0)
        assert poller.on_cycle(1) == pytest.approx(0.4)
        assert poller.on_cycle(0) == pytest.approx(0.8)
        assert poller.on_cycle(0) == pytest.approx(1.6)
        assert poller.on_cycle(0) == pytest.approx(2.0)  # capped at base
        assert poller.on_cycle(0) == pytest.approx(2.0)

    def test_stays_in_burst_while_hits_continue(self):
        poller = AdaptivePoller(base_interval_sec=2.0, burst_interval_sec=0.4)
        for _ in range(5):
            assert poller.on_cycle(3) == pytest.approx(0.4)

    def test_burst_never_slower_than_base(self):
        poller = AdaptivePoller(base_interval_sec=1.0, burst_interval_sec=5.0)
        assert poller.on_cycle(1) == pytest.approx(1.0)


class TestSignalThrottle:
    def test_blocks_within_cooldown_and_releases_after(self):
        throttle = SignalThrottle(cooldown_sec=30.0)
        assert throttle.ready("BTC/USD", now=1000.0)
        throttle.fire("BTC/USD", now=1000.0)
        assert not throttle.ready("BTC/USD", now=1010.0)
        assert throttle.ready("BTC/USD", now=1030.0)

    def test_keys_are_independent(self):
        throttle = SignalThrottle(cooldown_sec=30.0)
        throttle.fire("BTC/USD", now=1000.0)
        assert throttle.ready("ETH/USD", now=1001.0)

    def test_prune_drops_expired_entries(self):
        throttle = SignalThrottle(cooldown_sec=30.0)
        throttle.fire("BTC/USD", now=1000.0)
        throttle.fire("ETH/USD", now=1025.0)
        throttle.prune(now=1040.0)
        assert throttle._last_fired == {"ETH/USD": 1025.0}
