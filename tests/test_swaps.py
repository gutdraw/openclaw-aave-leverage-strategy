from bot.swaps import SWAP_ROUTER, inject_swap_approve

_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_WETH = "0x4200000000000000000000000000000000000006"
_RECIPIENT = "0x1111111111111111111111111111111111111111"


def _single_swap(route: list[object]) -> dict:
    return {
        "type": "swap",
        "abi_fn": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
        "args": [route],
    }


def test_injects_exact_bounded_approval_for_current_single_route() -> None:
    response = {
        "transaction_steps": [
            _single_swap([_USDC, _WETH, 500, _RECIPIENT, 12345, 1, 0])
        ]
    }

    updated = inject_swap_approve(response)

    assert updated is not response
    assert updated["transaction_steps"][0]["type"] == "approve"
    assert updated["transaction_steps"][0]["contract"] == _USDC
    assert updated["transaction_steps"][0]["args"] == [SWAP_ROUTER, 12345]
    assert response["transaction_steps"][0]["type"] == "swap"


def test_extracts_amount_from_legacy_single_route() -> None:
    route = [_USDC, _WETH, 500, _RECIPIENT, 999, 54321, 1, 0]

    updated = inject_swap_approve({"transaction_steps": [_single_swap(route)]})

    assert updated["transaction_steps"][0]["args"] == [SWAP_ROUTER, 54321]


def test_extracts_token_and_amount_from_exact_input_path() -> None:
    path = f"0x{_USDC[2:]}0001f4{_WETH[2:]}"
    swap = {
        "type": "swap",
        "abi_fn": "exactInput(bytes,address,uint256,uint256,uint256)",
        "args": [path, _RECIPIENT, 999, 777, 1],
    }

    updated = inject_swap_approve({"transaction_steps": [swap]})

    assert updated["transaction_steps"][0]["contract"] == _USDC
    assert updated["transaction_steps"][0]["args"] == [SWAP_ROUTER, 777]


def test_keeps_sufficient_router_approval() -> None:
    approval = {
        "type": "approve",
        "contract": _USDC,
        "args": [SWAP_ROUTER, 20000],
    }
    response = {
        "transaction_steps": [
            approval,
            _single_swap([_USDC, _WETH, 500, _RECIPIENT, 12345, 1, 0]),
        ]
    }

    assert inject_swap_approve(response) is response


def test_replaces_undersized_router_approval_but_keeps_other_steps() -> None:
    undersized = {
        "type": "approve",
        "contract": _USDC,
        "args": [SWAP_ROUTER, 1],
    }
    unrelated = {"type": "approve", "contract": _WETH, "args": [SWAP_ROUTER, 50]}
    swap = _single_swap([_USDC, _WETH, 500, _RECIPIENT, 12345, 1, 0])

    updated = inject_swap_approve({"transaction_steps": [undersized, unrelated, swap]})

    steps = updated["transaction_steps"]
    assert steps[0]["args"] == [SWAP_ROUTER, 12345]
    assert unrelated in steps
    assert undersized not in steps


def test_ignores_eth_swaps_invalid_steps_and_missing_steps() -> None:
    eth_swap = {"type": "swap", "use_eth": True, "amount_in": 123}
    malformed = {"transaction_steps": ["not-a-step", eth_swap]}

    assert inject_swap_approve(malformed) is malformed
    assert inject_swap_approve({}) == {}
