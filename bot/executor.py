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

log = logging.getLogger(__name__)


@dataclass
class ExecResult:
    action: str             # "open" | "close" | "reduce" | "paper"
    tx_hash: Optional[str]  # None in paper mode
    raw: dict               # full MCP response or stub


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
        amount = size.supply   # USDC seed amount
    else:
        supply_asset = cfg.asset
        borrow_asset = cfg.borrow_asset
        amount = size.supply   # asset units after swap

    if cfg.paper_trading:
        if direction == "long":
            log.info("[PAPER] swap USDC → %s amount=%.6f", cfg.asset, amount)
        log.info(
            "[PAPER] open %s supply=%.4f %s borrow=%.4f %s",
            direction, amount, supply_asset, size.borrow, borrow_asset,
        )
        return ExecResult(
            action="paper", tx_hash=None,
            raw={"paper": True, "direction": direction, "supply": amount, "borrow": size.borrow},
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
        return ExecResult(action="paper", tx_hash=None, raw={"paper": True, "position_id": position_id})

    resp = mcp.prepare_close(position_id=position_id)
    tx_hash = signer.execute_steps(resp)
    log.info("close tx %s", tx_hash)

    # Swap asset→USDC after closing a long so bot is always flat in stable.
    # Use actual post-close wallet balance rather than the stale supply figure —
    # flash-loan unwind may return slightly more or less than the original supply.
    if direction == "long":
        actual_asset = 0.0
        try:
            pos = mcp.get_position()
            wb = pos.get("tokenBalances") or pos.get("wallet_balances") or {}
            actual_asset = float(wb.get(cfg.asset, 0) or 0)
        except Exception as e:
            log.warning("post-close balance fetch failed, falling back to supply amount: %s", e)
            actual_asset = asset_amount
        if actual_asset > 0:
            log.info("swap %s → USDC amount=%.6f (post-close balance)", cfg.asset, actual_asset)
            swap_resp = mcp.swap(token_in=cfg.asset, token_out="USDC", amount_in=actual_asset)
            swap_hash = signer.execute_steps(swap_resp)
            log.info("swap tx %s", swap_hash)

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
            direction, amount, supply_asset, size.borrow, borrow_asset,
        )
        return ExecResult(
            action="paper", tx_hash=None,
            raw={"paper": True, "direction": direction, "supply": amount, "borrow": size.borrow},
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
        log.info("[PAPER] reduce %s %s → leverage %.1f", direction, position_id, target_leverage)
        return ExecResult(action="paper", tx_hash=None, raw={"paper": True, "target_leverage": target_leverage})

    resp = mcp.prepare_reduce(
        supply_asset=supply_asset,
        borrow_asset=borrow_asset,
        target_leverage=target_leverage,
    )
    tx_hash = signer.execute_steps(resp)
    log.info("reduce tx %s", tx_hash)
    return ExecResult(action="reduce", tx_hash=tx_hash, raw=resp)
