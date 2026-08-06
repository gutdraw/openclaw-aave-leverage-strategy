from bot.config import BotConfig
from bot.main import _find_chain_position

_CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _config() -> BotConfig:
    return BotConfig(
        asset="cbBTC",
        borrow_asset="USDC",
        short_borrow_asset="cbBTC",
    )


def _position(**overrides: object) -> dict:
    position = {
        "supplySymbol": "cbBTC",
        "supplyAsset": _CBBTC,
        "borrowSymbol": "USDC",
        "borrowAsset": _USDC,
        "aTokenBalance": 0.005,
        "variableDebt": 250.0,
    }
    position.update(overrides)
    return position


def test_matches_idless_position_using_mcp_symbols() -> None:
    position = _position()

    matched = _find_chain_position(
        {"aavePositions": {"positions": [position]}},
        "cbBTC/USDC",
        "long",
        _config(),
    )

    assert matched is position


def test_matches_idless_position_using_contract_addresses() -> None:
    position = _position(
        supplySymbol=None,
        borrowSymbol=None,
    )

    matched = _find_chain_position(
        {"aavePositions": {"positions": [position]}},
        "cbBTC/USDC",
        "long",
        _config(),
    )

    assert matched is position


def test_matches_short_positions_with_address_fields() -> None:
    position = _position(
        supplySymbol=None,
        supplyAsset=_USDC,
        borrowSymbol=None,
        borrowAsset=_CBBTC,
    )

    matched = _find_chain_position(
        {"aavePositions": {"positions": [position]}},
        "USDC/cbBTC",
        "short",
        _config(),
    )

    assert matched is position


def test_does_not_guess_between_multiple_matching_positions() -> None:
    positions = [_position(), _position(aTokenBalance=0.006)]

    assert (
        _find_chain_position(
            {"aavePositions": {"positions": positions}},
            "cbBTC/USDC",
            "long",
            _config(),
        )
        is None
    )


def test_rejects_single_position_with_wrong_assets() -> None:
    position = _position(
        supplySymbol="WETH", supplyAsset="0x4200000000000000000000000000000000000006"
    )

    assert (
        _find_chain_position(
            {"aavePositions": {"positions": [position]}},
            "cbBTC/USDC",
            "long",
            _config(),
        )
        is None
    )
