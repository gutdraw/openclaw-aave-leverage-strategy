"""
Trade executor — calls MCP prepare_* tools and (in live mode) signs & sends.

In paper-trading mode every prepare_* call is skipped; the executor just
returns a stub result so the rest of the cycle logic (logging, state) still runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from bot.config import BotConfig
from bot.mcp_client import MCPClient
from bot.sizing import PositionSize
from bot.swaps import inject_swap_approve

log = logging.getLogger(__name__)


def _wallet_token_balance(position: dict, token: str) -> float:
    balances = position.get("tokenBalances") or position.get("wallet_balances") or {}
    try:
        return max(float(balances.get(token, 0) or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class ExecResult:
    action: str  # "open" | "close" | "reduce" | "paper"
    tx_hash: Optional[str]  # None in paper mode
    raw: dict  # full MCP response or stub


def open_position(
    size: PositionSize,
    direction: str,
    position_id: str,
    cfg: BotConfig,
    mcp: MCPClient,
    signer=None,
) -> ExecResult:
    """
    Open a leveraged position.

    Long:  swap USDC→asset first (bot is always flat in USDC between trades),
           then supply_asset=cfg.asset, borrow_asset=USDC
    Short: supply_asset=USDC (already flat), borrow_asset=cfg.short_borrow_asset
    """
    if direction == "short":
        supply_asset = "USDC"
        borrow_asset = cfg.short_borrow_asset
        amount = size.supply  # USDC seed amount
    else:
        supply_asset = cfg.asset
        borrow_asset = cfg.borrow_asset
        amount = size.supply  # asset units after swap

    if cfg.paper_trading:
        if direction == "long":
            log.info("[PAPER] swap USDC → %s amount=%.6f", cfg.asset, amount)
        log.info(
            "[PAPER] open %s supply=%.4f %s borrow=%.4f %s",
            direction,
            amount,
            supply_asset,
            size.borrow,
            borrow_asset,
        )
        return ExecResult(
            action="paper",
            tx_hash=None,
            raw={
                "paper": True,
                "direction": direction,
                "supply": amount,
                "borrow": size.borrow,
            },
        )

    # Pre-swap (USDC→asset for longs) is handled by _ensure_wallet_token in main.py
    # before executor is called — do not swap again here.

    resp = mcp.prepare_open(
        leverage=cfg.leverage_for(direction),
        amount=amount,
        supply_asset=supply_asset,
        borrow_asset=borrow_asset,
    )
    tx_hash = signer.execute_steps(resp)
    log.info("open %s tx %s", direction, tx_hash)
    return ExecResult(action="open", tx_hash=tx_hash, raw=resp)


def close_position(
    position_id: str,
    direction: str,
    asset_amount: float,
    cfg: BotConfig,
    mcp: MCPClient,
    signer=None,
) -> ExecResult:
    """
    Close a leveraged position and return to flat USDC.

    Long close: flash loan repays USDC debt, returns cfg.asset to wallet.
                Then swap asset→USDC so bot is flat in stable.
    Short close: flash loan repays asset debt, returns USDC to wallet.
                 Already flat — no swap needed.
    """
    if cfg.paper_trading:
        log.info("[PAPER] close %s", position_id)
        if direction == "long":
            log.info("[PAPER] swap %s → USDC amount=%.6f", cfg.asset, asset_amount)
        return ExecResult(
            action="paper",
            tx_hash=None,
            raw={"paper": True, "position_id": position_id},
        )

    before_asset: Optional[float] = None
    if direction == "long":
        try:
            before_asset = _wallet_token_balance(mcp.get_position(), cfg.asset)
        except Exception as e:
            log.warning("pre-close balance fetch failed: %s", e)

    resp = mcp.prepare_close(position_id=position_id)
    tx_hash = signer.execute_steps(resp)
    log.info("close tx %s", tx_hash)

    # Swap asset→USDC after closing a long so bot is always flat in stable.
    # Swap only the asset received from this close. Never swap the full wallet
    # balance: the wallet may contain unrelated funds or a pre-existing hedge.
    if direction == "long":
        received_asset: Optional[float] = None
        try:
            pos = mcp.get_position()
            after_asset = _wallet_token_balance(pos, cfg.asset)
            if before_asset is not None:
                received_asset = max(after_asset - before_asset, 0.0)
            else:
                # Without a pre-close baseline, cap the fallback to the
                # position amount supplied by local state.
                received_asset = min(after_asset, max(asset_amount, 0.0))
        except Exception as e:
            log.warning(
                "post-close balance fetch failed, falling back to position amount: %s",
                e,
            )
            received_asset = max(asset_amount, 0.0)
        if received_asset and received_asset > 0:
            try:
                log.info(
                    "swap %s → USDC amount=%.6f (close proceeds)",
                    cfg.asset,
                    received_asset,
                )
                swap_resp = inject_swap_approve(
                    mcp.swap(
                        token_in=cfg.asset,
                        token_out="USDC",
                        amount_in=received_asset,
                    )
                )
                swap_hash = signer.execute_steps(swap_resp)
                log.info("swap tx %s", swap_hash)
            except Exception as e:
                # The Aave close is already confirmed. Record the close as
                # complete so the bot does not retry and duplicate exposure;
                # the remaining wallet asset can be handled on the next cycle.
                log.error("position close confirmed but proceeds swap failed: %s", e)
                return ExecResult(
                    action="close",
                    tx_hash=tx_hash,
                    raw={**resp, "post_close_swap_error": str(e)},
                )

    return ExecResult(action="close", tx_hash=tx_hash, raw=resp)


def increase_position(
    size: PositionSize,
    direction: str,
    position_id: str,
    cfg: BotConfig,
    mcp: MCPClient,
    signer=None,
) -> ExecResult:
    """Add to an existing leveraged position (moderate → strong signal upgrade)."""
    if direction == "short":
        supply_asset = "USDC"
        borrow_asset = cfg.short_borrow_asset
        amount = size.supply
    else:
        supply_asset = cfg.asset
        borrow_asset = cfg.borrow_asset
        amount = size.supply

    if cfg.paper_trading:
        if direction == "long":
            log.info("[PAPER] swap USDC → %s amount=%.6f (increase)", cfg.asset, amount)
        log.info(
            "[PAPER] increase %s supply=+%.4f %s borrow=+%.4f %s",
            direction,
            amount,
            supply_asset,
            size.borrow,
            borrow_asset,
        )
        return ExecResult(
            action="paper",
            tx_hash=None,
            raw={
                "paper": True,
                "direction": direction,
                "supply": amount,
                "borrow": size.borrow,
            },
        )

    # Pre-swap handled by _ensure_wallet_token in main.py before executor is called.

    resp = mcp.prepare_increase(
        leverage=cfg.leverage_for(direction),
        amount=amount,
        supply_asset=supply_asset,
        borrow_asset=borrow_asset,
    )
    tx_hash = signer.execute_steps(resp)
    log.info("increase %s tx %s", direction, tx_hash)
    return ExecResult(action="increase", tx_hash=tx_hash, raw=resp)


def reduce_position(
    position_id: str,
    direction: str,
    target_leverage: float,
    cfg: BotConfig,
    mcp: MCPClient,
    signer=None,
) -> ExecResult:
    if direction == "short":
        supply_asset = "USDC"
        borrow_asset = cfg.short_borrow_asset
    else:
        supply_asset = cfg.asset
        borrow_asset = cfg.borrow_asset

    if cfg.paper_trading:
        log.info(
            "[PAPER] reduce %s %s → leverage %.1f",
            direction,
            position_id,
            target_leverage,
        )
        return ExecResult(
            action="paper",
            tx_hash=None,
            raw={"paper": True, "target_leverage": target_leverage},
        )

    resp = mcp.prepare_reduce(
        supply_asset=supply_asset,
        borrow_asset=borrow_asset,
        target_leverage=target_leverage,
    )
    tx_hash = signer.execute_steps(resp)
    log.info("reduce tx %s", tx_hash)
    return ExecResult(action="reduce", tx_hash=tx_hash, raw=resp)
