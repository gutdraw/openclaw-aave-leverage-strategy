from unittest.mock import MagicMock

import bot.onchain as onchain
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


def test_fetch_account_risk_reads_only_account_data(monkeypatch) -> None:
    pool = MagicMock()
    pool.functions.getUserAccountData.return_value.call.return_value = (
        47582000000,
        32904000000,
        10000000000,
        7800,
        7300,
        1127900000000000000,
    )

    class FakeWeb3:
        class HTTPProvider:
            def __init__(self, rpc_url, request_kwargs):
                self.rpc_url = rpc_url
                self.request_kwargs = request_kwargs

        @staticmethod
        def is_address(value):
            return value == "0x0000000000000000000000000000000000000001"

        @staticmethod
        def to_checksum_address(value):
            return value

        def __init__(self, provider):
            self.provider = provider
            self.eth = MagicMock()
            self.eth.contract.return_value = pool
            self.eth.block_number = 123456

    monkeypatch.setattr(onchain, "Web3", FakeWeb3)

    result = onchain.fetch_account_risk(
        "https://rpc.example", "0x0000000000000000000000000000000000000001"
    )

    assert result.available is True
    assert result.health_factor == 1.1279
    assert result.total_collateral_usd == 475.82
    assert result.total_debt_usd == 329.04
    assert result.block_number == 123456
    pool.functions.getUserAccountData.assert_called_once()


def test_fetch_account_risk_rejects_invalid_address() -> None:
    result = onchain.fetch_account_risk("https://rpc.example", "not-an-address")

    assert result.available is False
    assert result.error == "invalid_user_address"
