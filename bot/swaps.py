"""Shared safeguards for ERC-20 swaps returned by the MCP server."""

from __future__ import annotations

from typing import Optional

# Uniswap V3 SwapRouter02 on Base.
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"


def _as_int(value: object) -> Optional[int]:
    try:
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(value)
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _path_token_in(path: object) -> Optional[str]:
    if not isinstance(path, str):
        return None
    encoded = path[2:] if path.startswith("0x") else path
    if len(encoded) < 40:
        return None
    return "0x" + encoded[:40]


def _swap_token_and_amount(step: dict) -> tuple[Optional[str], Optional[int]]:
    """Extract tokenIn and amountIn from the normalized Uniswap step shape."""
    args = step.get("args")
    if not isinstance(args, list) or not args:
        return None, _as_int(step.get("amount_in"))

    fn_name = str(step.get("abi_fn", "")).split("(", 1)[0]
    first = args[0]
    if fn_name == "exactInputSingle" and isinstance(first, (list, tuple)):
        # Current MCP shape: (tokenIn, tokenOut, fee, recipient, amountIn, ...).
        token_in = first[0] if len(first) > 0 else None
        amount_index = 5 if len(first) >= 8 else 4
        amount_in = _as_int(first[amount_index]) if len(first) > amount_index else None
        return (
            token_in if isinstance(token_in, str) else None,
            amount_in or _as_int(step.get("amount_in")),
        )

    if fn_name == "exactInput":
        # (path, recipient, deadline, amountIn, amountOutMinimum)
        token_in = _path_token_in(first)
        amount_in = _as_int(args[3]) if len(args) > 3 else None
        return token_in, amount_in or _as_int(step.get("amount_in"))

    return None, _as_int(step.get("amount_in"))


def inject_swap_approve(resp: dict) -> dict:
    """Add a bounded approval when a swap response omitted one.

    The approval is limited to this swap's exact ``amountIn``.  The returned
    response is copied so callers do not mutate the MCP response object.
    """
    steps = resp.get("transaction_steps")
    if not isinstance(steps, list):
        return resp

    swap_step = next(
        (
            step
            for step in steps
            if isinstance(step, dict) and step.get("type") == "swap"
        ),
        None,
    )
    if not isinstance(swap_step, dict) or swap_step.get("use_eth"):
        return resp

    token_in, amount_in = _swap_token_and_amount(swap_step)
    if not token_in or amount_in is None or amount_in <= 0:
        return resp

    router_lower = SWAP_ROUTER.lower()
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "approve":
            continue
        args = step.get("args")
        if not isinstance(args, list) or len(args) < 2:
            continue
        spender = args[0]
        existing_amount = _as_int(args[1])
        contract = step.get("contract")
        if (
            isinstance(spender, str)
            and spender.lower() == router_lower
            and isinstance(contract, str)
            and contract.lower() == token_in.lower()
        ):
            if existing_amount is not None and existing_amount >= amount_in:
                return resp
            # A server-supplied undersized approval is replaced below.

    approval = {
        "step": 0,
        "title": "Approve token for Swap",
        "type": "approve",
        "contract": token_in,
        "abi_fn": "approve(address,uint256)",
        "args": [SWAP_ROUTER, amount_in],
        "gas": 100_000,
    }
    filtered_steps = [
        step
        for step in steps
        if not (
            isinstance(step, dict)
            and step.get("type") == "approve"
            and isinstance(step.get("contract"), str)
            and step["contract"].lower() == token_in.lower()
            and isinstance(step.get("args"), list)
            and len(step["args"]) >= 1
            and isinstance(step["args"][0], str)
            and step["args"][0].lower() == router_lower
        )
    ]
    return {**resp, "transaction_steps": [approval, *filtered_steps]}
