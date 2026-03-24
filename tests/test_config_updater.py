import tempfile
from pathlib import Path

import yaml

from bot.config_updater import propose, get_bounds, BOUNDS


def _write_config(overrides: dict = {}) -> str:
    defaults = {
        "paper_trading": True,
        "asset": "WETH",
        "user_address": "0xABC123",
        "mcp_session_token": "tok_test",
        "mcp_url": "https://example.com",
        "leverage": 3.0,
        "max_leverage": 4.0,
        "base_position_pct": 0.20,
        "take_profit_pct": 5.0,
        "stop_loss_pct": 3.0,
        "max_borrow_apr": 8.0,
        "max_volatility_1h": 5.0,
        "btc_dominance_rise_threshold": 2.0,
        "hf_defense_reduce": 1.35,
        "hf_defense_close": 1.20,
        "min_open_hf": 1.30,
        "borrow_asset": "USDC",
        "short_borrow_asset": "WETH",
        "trades_file": "trades.jsonl",
        "private_key": "",
    }
    defaults.update(overrides)
    f = tempfile.NamedTemporaryFile(suffix=".yml", delete=False, mode="w")
    yaml.dump(defaults, f)
    f.close()
    return f.name


def _changes_log() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
    f.close()
    return f.name


def test_valid_change_applied():
    cfg = _write_config()
    log = _changes_log()
    result = propose({"take_profit_pct": 7.0}, cfg, log, reason="test")
    assert result.success
    assert result.applied == {"take_profit_pct": 7.0}
    assert result.rejected == {}
    # Verify written to disk
    raw = yaml.safe_load(Path(cfg).read_text())
    assert raw["take_profit_pct"] == 7.0


def test_multiple_valid_changes():
    cfg = _write_config()
    log = _changes_log()
    result = propose({"take_profit_pct": 7.0, "stop_loss_pct": 4.0}, cfg, log)
    assert result.success
    assert len(result.applied) == 2


def test_out_of_bounds_rejected():
    cfg = _write_config()
    log = _changes_log()
    result = propose({"leverage": 10.0}, cfg, log)
    assert not result.success
    assert "leverage" in result.rejected
    assert "out of bounds" in result.rejected["leverage"]


def test_immutable_field_rejected():
    cfg = _write_config()
    log = _changes_log()
    result = propose({"user_address": "0xHACKER"}, cfg, log)
    assert not result.success
    assert "user_address" in result.rejected


def test_live_mode_blocked():
    cfg = _write_config({"paper_trading": False})
    log = _changes_log()
    result = propose({"take_profit_pct": 7.0}, cfg, log)
    assert not result.success
    assert "paper_trading" in result.message


def test_hf_cross_validation():
    cfg = _write_config()
    log = _changes_log()
    # hf_defense_close must be < hf_defense_reduce
    result = propose({"hf_defense_close": 1.40, "hf_defense_reduce": 1.35}, cfg, log)
    # hf_defense_close=1.40 >= hf_defense_reduce=1.35 → invalid
    assert "hf_defense_close" in result.rejected


def test_partial_apply():
    cfg = _write_config()
    log = _changes_log()
    result = propose({"take_profit_pct": 7.0, "leverage": 99.0}, cfg, log)
    assert result.success           # partial success — take_profit applied
    assert "take_profit_pct" in result.applied
    assert "leverage" in result.rejected


def test_get_bounds_returns_all():
    bounds = get_bounds()
    assert "leverage" in bounds
    assert bounds["leverage"]["min"] == BOUNDS["leverage"][0]
    assert bounds["leverage"]["max"] == BOUNDS["leverage"][1]


def test_change_logged():
    cfg = _write_config()
    log = _changes_log()
    from bot.config_updater import get_change_history
    propose({"take_profit_pct": 7.0}, cfg, log, reason="test reason")
    history = get_change_history(log)
    assert len(history) == 1
    assert history[0]["applied"] == {"take_profit_pct": 7.0}
    assert history[0]["reason"] == "test reason"
