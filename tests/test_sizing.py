from unittest.mock import MagicMock

from bot.signal import Signal
from bot.sizing import compute


def _cfg(leverage=3.0, base_pct=0.20):
    cfg = MagicMock()
    cfg.leverage = leverage
    cfg.base_position_pct = base_pct
    return cfg


def _sig(multiplier: float, direction: str = "long") -> Signal:
    label = "strong_long" if direction == "long" else "strong_short"
    return Signal(score=3, label=label, multiplier=multiplier, direction=direction)


# ── Long sizing ───────────────────────────────────────────────────────────────

def test_strong_long_full_size():
    size = compute(10_000.0, 2000.0, _sig(1.0, "long"), _cfg())
    assert size.seed_usd == 2_000.0          # 10k * 0.20 * 1.0
    assert abs(size.supply - 1.0) < 1e-9     # 2000 / 2000 ETH
    assert abs(size.borrow - 2.0) < 1e-9     # 1.0 * (3 - 1) USDC... wait, borrow in USDC


def test_moderate_long_half_size():
    size = compute(10_000.0, 2000.0, _sig(0.5, "long"), _cfg())
    assert size.seed_usd == 1_000.0
    assert abs(size.supply - 0.5) < 1e-9
    assert abs(size.borrow - 1.0) < 1e-9


def test_long_leverage_2x():
    size = compute(10_000.0, 1000.0, _sig(1.0, "long"), _cfg(leverage=2.0))
    assert abs(size.borrow - size.supply) < 1e-9  # borrow = supply * (2-1)


# ── Short sizing ──────────────────────────────────────────────────────────────

def test_strong_short_full_size():
    size = compute(10_000.0, 2000.0, _sig(1.0, "short"), _cfg())
    assert size.seed_usd == 2_000.0
    assert abs(size.supply - 2_000.0) < 1e-9    # USDC collateral = seed_usd
    assert abs(size.borrow - 2.0) < 1e-9        # (lev-1)*seed / price = 2*2000/2000 = 2 ETH


def test_moderate_short_half_size():
    size = compute(10_000.0, 2000.0, _sig(0.5, "short"), _cfg())
    assert size.seed_usd == 1_000.0
    assert abs(size.supply - 1_000.0) < 1e-9
    assert abs(size.borrow - 1.0) < 1e-9        # 2*1000/2000 = 1 ETH


# ── Zero / edge cases ─────────────────────────────────────────────────────────

def test_zero_multiplier_returns_zero():
    size = compute(10_000.0, 2000.0, _sig(0.0), _cfg())
    assert size.seed_usd == 0.0
    assert size.supply == 0.0
    assert size.borrow == 0.0


def test_zero_price_returns_zero():
    size = compute(10_000.0, 0.0, _sig(1.0), _cfg())
    assert size.supply == 0.0
