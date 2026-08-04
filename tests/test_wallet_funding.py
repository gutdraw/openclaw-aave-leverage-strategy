from unittest.mock import MagicMock, patch

import pytest

from bot.main import _ensure_wallet_token


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.short_borrow_asset = "cbBTC"
    cfg.asset = "cbBTC"
    return cfg


def _market_data(usdc: float, cbbtc: float) -> MagicMock:
    data = MagicMock()
    data.price = 63_687.0
    data.position_data = {
        "tokenBalances": {
            "USDC": usdc,
            "cbBTC": cbbtc,
        }
    }
    return data


def _swap_mocks() -> tuple[MagicMock, MagicMock]:
    mcp = MagicMock()
    mcp.swap.return_value = {"transaction_steps": []}

    signer = MagicMock()
    signer.execute_steps.return_value = "0xtest-swap"
    return mcp, signer


def test_short_swaps_asset_when_combined_balances_cover_seed() -> None:
    mcp, signer = _swap_mocks()
    cycle_entry: dict[str, object] = {}

    with patch("bot.main.time.sleep"):
        result = _ensure_wallet_token(
            "short",
            107.0,
            _market_data(usdc=54.87, cbbtc=0.001562),
            _config(),
            mcp,
            signer,
            cycle_entry,
        )

    assert result is None
    swap_args = mcp.swap.call_args.args
    assert swap_args[0] == "cbBTC"
    assert swap_args[1] == "USDC"
    assert swap_args[2] == pytest.approx(0.00082017146)
    assert swap_args[2] < 0.001562
    signer.wait_for_receipt.assert_called_once_with("0xtest-swap")
    assert "cbBTC → USDC" in cycle_entry["pre_swap"]


def test_short_skips_when_combined_balances_do_not_cover_seed() -> None:
    mcp, signer = _swap_mocks()
    cycle_entry: dict[str, object] = {}

    result = _ensure_wallet_token(
        "short",
        107.0,
        _market_data(usdc=50.0, cbbtc=0.0005),
        _config(),
        mcp,
        signer,
        cycle_entry,
    )

    assert result is False
    assert cycle_entry["decision"] == "skip_insufficient_funds"
    mcp.swap.assert_not_called()
