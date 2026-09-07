import pytest

from bot.swaps import (
    SWAP_ROUTER,
    bound_swap_response,
    inject_swap_approve,
    validate_swap_response,
)

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


def test_extracts_amount_from_current_mcp_exact_input_tuple() -> None:
    path = f"0x{_USDC[2:]}0001f4{_WETH[2:]}"
    swap = {
        "type": "swap",
        "abi_fn": "exactInput((bytes,address,uint256,uint256))",
        "args": [[path, _RECIPIENT, 777, 1]],
    }

    updated = inject_swap_approve({"transaction_steps": [swap]})

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


def test_swap_policy_requires_fresh_quote_deadline_and_output_floor() -> None:
    response = {
        "quote": {"amount_out": 100_000, "quoted_at": 1_000.0},
        "transaction_steps": [
            {
                "type": "swap",
                "abi_fn": "exactInput(bytes,address,uint256,uint256,uint256)",
                "args": [
                    "0x" + _USDC[2:] + "0001f4" + _WETH[2:],
                    _RECIPIENT,
                    1_100,
                    10_000,
                    99_800,
                ],
            }
        ],
    }

    validate_swap_response(
        response,
        slippage_bps=20,
        quote_max_age_seconds=30,
        deadline_seconds=120,
        require_quote=True,
        now=1_001.0,
    )


def test_swap_policy_rejects_stale_or_unbounded_swap() -> None:
    response = {
        "quote": {"amount_out": 100_000, "quoted_at": 900.0},
        "transaction_steps": [
            {
                "type": "swap",
                "abi_fn": "exactInput(bytes,address,uint256,uint256,uint256)",
                "args": [
                    "0x" + _USDC[2:] + "0001f4" + _WETH[2:],
                    _RECIPIENT,
                    1_100,
                    10_000,
                    99_800,
                ],
            }
        ],
    }

    with pytest.raises(ValueError, match="stale"):
        validate_swap_response(
            response,
            slippage_bps=20,
            quote_max_age_seconds=30,
            deadline_seconds=120,
            require_quote=True,
            now=1_001.0,
        )

    response["quote"]["quoted_at"] = 1_000.0
    response["transaction_steps"][0]["args"][2] = 2_000
    with pytest.raises(ValueError, match="deadline"):
        validate_swap_response(
            response,
            slippage_bps=20,
            quote_max_age_seconds=30,
            deadline_seconds=120,
            require_quote=True,
            now=1_001.0,
        )


def test_swap_policy_bounds_current_mcp_legacy_payload() -> None:
    response = {
        "summary": {"min_out_atomic": "99800"},
        "transaction_steps": [
            _single_swap([_USDC, _WETH, 500, _RECIPIENT, 10_000, 99_800, 0])
        ],
    }

    bounded = bound_swap_response(response, deadline_seconds=120, now=1_000.0)

    validate_swap_response(
        bounded,
        slippage_bps=20,
        quote_max_age_seconds=30,
        deadline_seconds=120,
        require_quote=True,
        now=1_001.0,
    )
    assert "client_deadline" in bounded["transaction_steps"][0]
    assert "client_deadline" not in response["transaction_steps"][0]


def test_swap_policy_checks_embedded_aave_router_swap() -> None:
    response = {
        "summary": {"min_out_atomic": "1900"},
        "transaction_steps": [
            {
                "type": "openPosition",
                "abi_fn": "openPosition(address,address,address,address,uint256,uint256,uint24,bytes,uint256)",
                "args": [
                    _WETH,
                    _USDC,
                    _RECIPIENT,
                    _RECIPIENT,
                    "1000",
                    "2000",
                    "500",
                    "0x",
                    "1900",
                ],
            }
        ],
    }

    bounded = bound_swap_response(response, deadline_seconds=120, now=1_000.0)
    validate_swap_response(
        bounded,
        slippage_bps=20,
        quote_max_age_seconds=30,
        deadline_seconds=120,
        require_quote=True,
        now=1_001.0,
    )
    assert "client_deadline" in bounded["transaction_steps"][0]
