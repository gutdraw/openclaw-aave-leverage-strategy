import pytest

from bot.signer import Signer, _LEVERAGE_ROUTER, _SWAP_ROUTER
from bot.swaps import inject_swap_approve

_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_WETH = "0x4200000000000000000000000000000000000006"


def _signer() -> Signer:
    return Signer("http://127.0.0.1:1", "0x" + "11" * 32)


def test_signer_accepts_allowlisted_leverage_step() -> None:
    signer = _signer()
    signer._validate_step(
        {
            "contract": _LEVERAGE_ROUTER,
            "abi_fn": "openPosition()",
            "args": [],
            "value": 0,
        }
    )


def test_signer_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        _signer()._validate_step(
            {
                "contract": "0x000000000000000000000000000000000000dEaD",
                "abi_fn": "openPosition()",
                "args": [],
            }
        )


def test_signer_rejects_unknown_approval_spender() -> None:
    with pytest.raises(ValueError, match="spender is not allowlisted"):
        _signer()._validate_step(
            {
                "contract": _USDC,
                "abi_fn": "approve(address,uint256)",
                "args": ["0x000000000000000000000000000000000000dEaD", 100],
            }
        )


def test_signer_requires_swap_recipient_to_be_wallet() -> None:
    signer = _signer()
    route = [
        _USDC,
        _WETH,
        500,
        "0x000000000000000000000000000000000000dEaD",
        100,
        1,
        0,
    ]
    with pytest.raises(ValueError, match="recipient"):
        signer._validate_step(
            {
                "contract": _SWAP_ROUTER,
                "abi_fn": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
                "args": [route],
                "type": "swap",
            }
        )


def test_swap_approval_is_bounded_to_amount_in() -> None:
    response = {
        "transaction_steps": [
            {
                "type": "swap",
                "abi_fn": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
                "args": [
                    [
                        _USDC,
                        _WETH,
                        500,
                        "0x0000000000000000000000000000000000000001",
                        12345,
                        1,
                        0,
                    ]
                ],
            }
        ]
    }

    updated = inject_swap_approve(response)

    assert updated is not response
    approval = updated["transaction_steps"][0]
    assert approval["contract"] == _USDC
    assert approval["args"] == [_SWAP_ROUTER, 12345]
    assert response["transaction_steps"][0]["type"] == "swap"
