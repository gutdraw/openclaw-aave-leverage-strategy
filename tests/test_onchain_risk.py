from unittest.mock import MagicMock

from bot.onchain import _reserve_configuration


def test_reserve_configuration_decodes_live_risk_bits() -> None:
    config = 7300 | (7800 << 16) | (8 << 48) | (2 << 168)
    web3 = MagicMock()
    web3.eth.call.return_value = config.to_bytes(32, "big") + b"\x00" * 320

    result = _reserve_configuration(web3, "cbBTC")

    assert result is not None
    assert result["ltv"] == 0.73
    assert result["liquidation_threshold"] == 0.78
    assert result["decimals"] == 8
    assert result["emode_category"] == 2
