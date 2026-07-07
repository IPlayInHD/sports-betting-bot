from __future__ import annotations

from arbbot.config import AppConfig
from arbbot.main import _apply_conservative_mode


def test_conservative_mode_disables_directional_layers():
    cfg = AppConfig()
    cfg.conservative_mode = True
    cfg.strategy.value_edge.enabled = True
    cfg.markets.polymarket_crypto.spot_anchor.enabled = True

    _apply_conservative_mode(cfg)

    assert cfg.strategy.value_edge.enabled is False
    assert cfg.markets.polymarket_crypto.spot_anchor.enabled is False
    # Riskless layers are untouched.
    assert cfg.strategy.arbitrage.enabled is True
    assert cfg.markets.polymarket_crypto.complement.enabled is True
    assert cfg.markets.crypto.enabled is True


def test_conservative_mode_off_leaves_layers_as_configured():
    cfg = AppConfig()
    cfg.conservative_mode = False
    cfg.strategy.value_edge.enabled = True
    cfg.markets.polymarket_crypto.spot_anchor.enabled = True

    _apply_conservative_mode(cfg)

    assert cfg.strategy.value_edge.enabled is True
    assert cfg.markets.polymarket_crypto.spot_anchor.enabled is True


def test_conservative_mode_is_the_default():
    assert AppConfig().conservative_mode is True
