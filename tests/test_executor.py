from unittest.mock import MagicMock

from bot.executor import close_position


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.paper_trading = False
    cfg.asset = "cbBTC"
    return cfg


def _position(balance: float) -> dict:
    return {"tokenBalances": {"cbBTC": balance}}


def test_close_swaps_only_newly_received_asset() -> None:
    mcp = MagicMock()
    mcp.get_position.side_effect = [_position(0.005), _position(0.007)]
    mcp.prepare_close.return_value = {"transaction_steps": [{"type": "close"}]}
    mcp.swap.return_value = {"transaction_steps": []}

    signer = MagicMock()
    signer.execute_steps.side_effect = ["close-tx", "swap-tx"]

    result = close_position("cbBTC/USDC", "long", 0.001, _config(), mcp, signer)

    assert result.tx_hash == "close-tx"
    assert mcp.swap.call_args.kwargs["amount_in"] == 0.002
    assert signer.execute_steps.call_count == 2


def test_close_records_confirmed_close_when_proceeds_swap_fails() -> None:
    mcp = MagicMock()
    mcp.get_position.side_effect = [_position(0.005), _position(0.007)]
    mcp.prepare_close.return_value = {"transaction_steps": [{"type": "close"}]}
    mcp.swap.return_value = {"transaction_steps": []}

    signer = MagicMock()
    signer.execute_steps.side_effect = ["close-tx", RuntimeError("swap failed")]

    result = close_position("cbBTC/USDC", "long", 0.001, _config(), mcp, signer)

    assert result.tx_hash == "close-tx"
    assert "post_close_swap_error" in result.raw
