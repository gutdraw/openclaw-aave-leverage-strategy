"""Shared safeguards for ERC-20 swaps returned by the MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Uniswap V3 SwapRouter02 on Base.
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"


@dataclass(frozen=True)
class SwapDetails:
    function: str
    deadline: Optional[int]
    amount_in: Optional[int]
    amount_out_minimum: Optional[int]
    amount_in_maximum: Optional[int] = None


_EMBEDDED_SWAP_FUNCTIONS = {
    "openPosition",
    "closePosition",
    "increaseLeverage",
    "reduceLeverage",
    "openLeverage",
    "closeLeverage",
    "increasePosition",
    "reducePosition",
}


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
    if fn_name == "exactInputSingle":
        route = args[0]
        if not isinstance(route, (list, tuple)):
            return None, _as_int(step.get("amount_in"))
        # Current MCP shape: (tokenIn, tokenOut, fee, recipient, amountIn, ...).
        token_in = route[0] if len(route) > 0 else None
        amount_index = 5 if len(route) >= 8 else 4
        amount_in = _as_int(route[amount_index]) if len(route) > amount_index else None
        return (
            token_in if isinstance(token_in, str) else None,
            amount_in or _as_int(step.get("amount_in")),
        )

    if fn_name == "exactInput":
        # Supports both flat args and the current MCP tuple shape.
        route = (
            args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
        )
        first = route[0]
        token_in = _path_token_in(first)
        amount_index = (
            3 if len(args) == 1 and len(route) >= 5 else 2 if len(args) == 1 else 3
        )
        amount_in = _as_int(route[amount_index]) if len(route) > amount_index else None
        return token_in, amount_in or _as_int(step.get("amount_in"))

    return None, _as_int(step.get("amount_in"))


def _swap_details(step: dict) -> SwapDetails:
    """Extract deadline and output floor from supported Uniswap V3 shapes."""
    args = step.get("args")
    fn_name = str(step.get("abi_fn", "")).split("(", 1)[0]
    if not isinstance(args, list) or not args:
        return SwapDetails(fn_name, None, None, None)
    if fn_name == "exactInputSingle":
        route = args[0]
        if not isinstance(route, (list, tuple)):
            return SwapDetails(fn_name, None, None, None)
        if len(route) >= 8:
            return SwapDetails(
                fn_name, _as_int(route[4]), _as_int(route[5]), _as_int(route[6])
            )
        # Legacy MCP shape omitted a contract deadline. The live executor adds
        # a client-side expiry before validating/signing this response.
        return SwapDetails(fn_name, None, _as_int(route[4]), _as_int(route[5]))
    if fn_name == "exactInput":
        route = (
            args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
        )
        if len(route) >= 5:
            return SwapDetails(
                fn_name, _as_int(route[2]), _as_int(route[3]), _as_int(route[4])
            )
        if len(route) >= 4:
            # The current MCP server uses the legacy tuple without a contract
            # deadline. ``bound_swap_response`` supplies a client-side expiry.
            return SwapDetails(fn_name, None, _as_int(route[2]), _as_int(route[3]))
    if fn_name in {"openPosition", "openLeverage"} and len(args) >= 9:
        return SwapDetails(
            fn_name,
            _as_int(step.get("deadline")),
            _as_int(args[5]),
            _as_int(args[8]),
        )
    if fn_name in {"increaseLeverage", "increasePosition"} and len(args) >= 7:
        return SwapDetails(
            fn_name,
            _as_int(step.get("deadline")),
            _as_int(args[4]),
            _as_int(args[6]),
        )
    if fn_name in {"closePosition", "closeLeverage"} and len(args) >= 8:
        return SwapDetails(
            fn_name,
            _as_int(step.get("deadline")),
            _as_int(args[4]),
            _as_int(args[7]),
        )
    if fn_name in {"reduceLeverage", "reducePosition"} and len(args) >= 7:
        return SwapDetails(
            fn_name,
            _as_int(step.get("deadline")),
            _as_int(args[4]),
            None,
            _as_int(args[6]),
        )
    return SwapDetails(fn_name, None, None, None)


def _is_swap_step(step: object) -> bool:
    if not isinstance(step, dict):
        return False
    if step.get("type") == "swap":
        return True
    return str(step.get("abi_fn", "")).split("(", 1)[0] in _EMBEDDED_SWAP_FUNCTIONS


def _quote_value(quote: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = quote.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _quote_timestamp(quote: dict) -> Optional[float]:
    value = quote.get("quoted_at", quote.get("timestamp"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        return None


def bound_swap_response(
    resp: dict, *, deadline_seconds: int, now: Optional[float] = None
) -> dict:
    """Attach local freshness bounds to the MCP server's legacy swap shape.

    The current server returns ``summary.min_out_atomic`` and a seven-field
    ``exactInputSingle`` tuple. That tuple has no contract-level deadline, so
    this function adds a non-ABI ``client_deadline`` used by the validator. The
    amountOutMinimum remains the on-chain price-impact protection. A future MCP
    response with a real deadline is left unchanged.
    """
    steps = resp.get("transaction_steps")
    if not isinstance(steps, list):
        return resp
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    result = dict(resp)

    quote = resp.get("quote") or resp.get("swap_quote")
    if isinstance(quote, dict):
        quote_copy = dict(quote)
        if (
            _quote_value(
                quote_copy,
                "amount_out",
                "amountOut",
                "expected_amount_out",
                "expectedAmountOut",
                "min_out_atomic",
                "minOut",
                "max_in",
                "maxIn",
                "max_in_atomic",
            )
            is not None
            and _quote_timestamp(quote_copy) is None
        ):
            quote_copy["quoted_at"] = current
        result["quote"] = quote_copy
    else:
        summary = resp.get("summary")
        amount_out = (
            _quote_value(summary, "min_out_atomic", "minOut", "amount_out")
            if isinstance(summary, dict)
            else None
        )
        max_in = (
            _quote_value(summary, "max_in_atomic", "maxIn", "max_in")
            if isinstance(summary, dict)
            else None
        )
        if amount_out is not None:
            result["quote"] = {
                "amount_out": amount_out,
                "quoted_at": current,
                "source": "mcp_summary_min_out",
            }
        elif max_in is not None:
            result["quote"] = {
                "max_in": max_in,
                "quoted_at": current,
                "source": "mcp_summary_max_in",
            }
        else:
            for step in steps:
                if not _is_swap_step(step):
                    continue
                details = _swap_details(step)
                if details.amount_out_minimum is not None:
                    result["quote"] = {
                        "amount_out": details.amount_out_minimum,
                        "quoted_at": current,
                        "source": "mcp_step_min_out",
                    }
                    break
                if details.amount_in_maximum is not None:
                    result["quote"] = {
                        "max_in": details.amount_in_maximum,
                        "quoted_at": current,
                        "source": "mcp_step_max_in",
                    }
                    break

    bounded_steps: list[object] = []
    for step in steps:
        if not _is_swap_step(step):
            bounded_steps.append(step)
            continue
        details = _swap_details(step)
        if details.deadline is None and "client_deadline" not in step:
            bounded_steps.append(
                {**step, "client_deadline": int(current + deadline_seconds)}
            )
        else:
            bounded_steps.append(step)
    result["transaction_steps"] = bounded_steps
    return result


def validate_swap_response(
    resp: dict,
    *,
    slippage_bps: int,
    quote_max_age_seconds: int,
    deadline_seconds: int,
    require_quote: bool,
    now: Optional[float] = None,
) -> None:
    """Fail closed unless every live swap has bounded execution parameters."""
    steps = resp.get("transaction_steps")
    if not isinstance(steps, list):
        return
    swap_steps = [step for step in steps if _is_swap_step(step)]
    if not swap_steps:
        return
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    quote = resp.get("quote") or resp.get("swap_quote")
    if require_quote and not isinstance(quote, dict):
        raise ValueError("swap response is missing fresh quote metadata")

    expected_out = None
    expected_max_in = None
    if isinstance(quote, dict):
        expected_out = _quote_value(
            quote,
            "amount_out",
            "amountOut",
            "expected_amount_out",
            "expectedAmountOut",
            "min_out_atomic",
            "minOut",
        )
        expected_max_in = _quote_value(quote, "max_in", "maxIn", "max_in_atomic")
        quoted_at = _quote_timestamp(quote)
        if (expected_out is None and expected_max_in is None) or quoted_at is None:
            raise ValueError(
                "swap quote must include amount_out or max_in and quoted_at"
            )
        if expected_out is not None and expected_out <= 0:
            raise ValueError("swap quote amount_out must be positive")
        if expected_max_in is not None and expected_max_in <= 0:
            raise ValueError("swap quote max_in must be positive")
        if current - quoted_at < 0 or current - quoted_at > quote_max_age_seconds:
            raise ValueError("swap quote is stale")

    for step in swap_steps:
        details = _swap_details(step)
        deadline = details.deadline or _as_int(step.get("client_deadline"))
        if deadline is None:
            raise ValueError("swap is missing a bounded deadline")
        if deadline < current:
            raise ValueError("swap deadline has expired")
        if deadline > current + deadline_seconds:
            raise ValueError("swap deadline exceeds the configured safety window")
        if details.amount_in is None or details.amount_in <= 0:
            raise ValueError("swap amountIn must be positive")
        if details.amount_out_minimum is None or details.amount_out_minimum <= 0:
            if details.amount_in_maximum is None:
                raise ValueError("swap amountOutMinimum must be positive")
        if details.amount_in_maximum is not None and details.amount_in_maximum <= 0:
            raise ValueError("swap max input must be positive")
        if expected_out is not None:
            minimum = int(expected_out * (10_000 - slippage_bps) / 10_000)
            if (
                details.amount_out_minimum is None
                or details.amount_out_minimum < minimum
            ):
                raise ValueError(
                    "swap amountOutMinimum is below the configured quote floor"
                )
        if expected_max_in is not None and details.amount_in_maximum is not None:
            if details.amount_in_maximum > expected_max_in:
                raise ValueError("swap max input exceeds the quoted safety cap")


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
