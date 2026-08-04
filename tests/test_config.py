import tempfile

import pytest
import yaml

from bot.config import BotConfig


def _config_file(**overrides) -> str:
    values = {
        "user_address": "0x0000000000000000000000000000000000000001",
        "mcp_session_token": "test-token",
        "paper_trading": True,
        "position_id": "cbBTC/USDC",
        "short_position_id": "USDC/cbBTC",
    }
    values.update(overrides)
    handle = tempfile.NamedTemporaryFile(suffix=".yml", delete=False, mode="w")
    yaml.safe_dump(values, handle)
    handle.close()
    return handle.name


def test_load_preserves_position_ids() -> None:
    path = _config_file()
    config = BotConfig.load(path)
    assert config.position_id == "cbBTC/USDC"
    assert config.short_position_id == "USDC/cbBTC"


def test_load_rejects_unknown_keys() -> None:
    path = _config_file(positon_id="typo")
    with pytest.raises(ValueError, match="unknown config keys: positon_id"):
        BotConfig.load(path)
