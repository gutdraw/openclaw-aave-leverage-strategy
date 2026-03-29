from unittest.mock import MagicMock

from bot.filters import apply_all
from bot.market import MarketData


def _data(**kwargs) -> MarketData:
    defaults = dict(
        price=2000.0,
        change_1h=0.5,
        change_24h=1.0,
        change_7d=2.0,
        borrow_apr=5.0,
        btc_dominance=50.0,
        health_factor=2.0,
        total_collateral_usd=10_000.0,
        position_data={},
    )
    defaults.update(kwargs)
    return MarketData(**defaults)


def _cfg(**kwargs):
    cfg = MagicMock()
    cfg.max_volatility_1h = 5.0
    cfg.max_borrow_apr = 8.0
    cfg.btc_dominance_rise_threshold = 2.0
    cfg.max_funding_rate_long = 0.05
    cfg.max_funding_rate_short = 0.05
    cfg.max_fear_greed_long = 85
    cfg.min_fear_greed_short = 15
    cfg.fear_greed_short_rsi_floor = 35.0
    cfg.min_volume_24h_usd = 0.0
    cfg.max_usdc_utilization = 0.92
    cfg.max_recent_liquidations = 3
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


# ── Volatility ────────────────────────────────────────────────────────────────

def test_no_filter_passes():
    result = apply_all(_data(), "strong_long", "long", None, 49.0, _cfg())
    assert not result.blocked


def test_volatility_blocks():
    result = apply_all(_data(change_1h=6.0), "strong_long", "long", None, None, _cfg())
    assert result.blocked
    assert result.decision == "skip_volatility"


def test_volatility_negative_spike():
    result = apply_all(_data(change_1h=-6.0), "strong_long", "long", None, None, _cfg())
    assert result.blocked
    assert result.decision == "skip_volatility"


# ── Borrow APR ────────────────────────────────────────────────────────────────

def test_borrow_apr_blocks_new_entry():
    result = apply_all(_data(borrow_apr=10.0), "strong_long", "long", None, None, _cfg())
    assert result.blocked
    assert result.decision == "skip_borrow_apr"


def test_borrow_apr_does_not_block_open_trade():
    open_trade = {"asset": "WETH", "action": "open", "direction": "long"}
    # open_trade present → filter 4 (overlap) triggers first, not borrow APR
    result = apply_all(_data(borrow_apr=10.0), "strong_long", "long", open_trade, None, _cfg())
    assert result.blocked
    assert result.decision == "skip_already_open"


# ── BTC dominance ─────────────────────────────────────────────────────────────

def test_btc_dominance_rising_blocks_long():
    result = apply_all(_data(btc_dominance=53.0), "strong_long", "long", None, 50.0, _cfg())
    assert result.blocked
    assert result.decision == "skip_btc_dominance"


def test_btc_dominance_rising_does_not_block_short():
    # Rising dominance is fine for shorts (BTC eating alts → alts weaken → good for short alts)
    result = apply_all(_data(btc_dominance=53.0), "strong_short", "short", None, 50.0, _cfg())
    assert not result.blocked


def test_btc_dominance_falling_blocks_short():
    # Falling dominance → alt season → bad for short alts
    result = apply_all(_data(btc_dominance=47.0), "strong_short", "short", None, 50.0, _cfg())
    assert result.blocked
    assert result.decision == "skip_btc_dominance"


def test_btc_dominance_falling_does_not_block_long():
    result = apply_all(_data(btc_dominance=47.0), "strong_long", "long", None, 50.0, _cfg())
    assert not result.blocked


def test_btc_dominance_skipped_when_no_prev():
    result = apply_all(_data(btc_dominance=53.0), "strong_long", "long", None, None, _cfg())
    assert not result.blocked


# ── Position overlap ──────────────────────────────────────────────────────────

def test_position_overlap_blocks_any_new_open():
    open_trade = {"asset": "WETH", "action": "open", "direction": "long"}
    result = apply_all(_data(), "strong_long", "long", open_trade, 49.0, _cfg())
    assert result.blocked
    assert result.decision == "skip_already_open"


def test_position_overlap_blocks_opposite_direction():
    # If long is open and signal flips short, still blocked — exit first
    open_trade = {"asset": "WETH", "action": "open", "direction": "long"}
    result = apply_all(_data(), "strong_short", "short", open_trade, 49.0, _cfg())
    assert result.blocked
    assert result.decision == "skip_already_open"
