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

    Long:  supply_asset=cfg.asset (e.g. WETH), borrow_asset=cfg.borrow_asset (USDC)
    Short: supply_asset="USDC",                borrow_asset=cfg.short_borrow_asset (e.g. WETH)
    """
    if direction == "short":
        supply_asset = "USDC"
        borrow_asset = cfg.short_borrow_asset
        amount = size.supply   # USDC seed amount
    else:
        supply_asset = cfg.asset
        borrow_asset = cfg.borrow_asset
        amount = size.supply   # asset units

    if cfg.paper_trading:
        log.info(
            "[PAPER] open %s supply=%.4f %s borrow=%.4f %s",
            direction, amount, supply_asset, size.borrow, borrow_asset,
        )
        return ExecResult(
            action="paper", tx_hash=None,
            raw={"paper": True, "direction": direction, "supply": amount, "borrow": size.borrow},
        )

    resp = mcp.prepare_open(
        leverage=cfg.leverage,
        amount=amount,
        supply_asset=supply_asset,
        borrow_asset=borrow_asset,
    )
    tx_hash = signer.sign_and_send(resp["transaction"])
    signer.wait_for_receipt(tx_hash)
    log.info("open %s tx %s", direction, tx_hash)
    return ExecResult(action="open", tx_hash=tx_hash, raw=resp)


def close_position(
    position_id: str,
    cfg: BotConfig,
    mcp: MCPClient,
    signer=None,
) -> ExecResult:
    if cfg.paper_trading:
        log.info("[PAPER] close %s", position_id)
        return ExecResult(action="paper", tx_hash=None, raw={"paper": True, "position_id": position_id})

    resp = mcp.prepare_close(position_id=position_id)
    tx_hash = signer.sign_and_send(resp["transaction"])
    signer.wait_for_receipt(tx_hash)
    log.info("close tx %s", tx_hash)
    return ExecResult(action="close", tx_hash=tx_hash, raw=resp)


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
    tx_hash = signer.sign_and_send(resp["transaction"])
    signer.wait_for_receipt(tx_hash)
    log.info("reduce tx %s", tx_hash)
    return ExecResult(action="reduce", tx_hash=tx_hash, raw=resp)
