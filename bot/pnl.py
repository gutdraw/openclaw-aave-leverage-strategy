"""
P&L calculator — computes unrealised and realised P&L from trade log entries.

All returns are in USD.  Leverage amplifies both gains and losses.

Long P&L:
  unrealised_pct = (current - entry) / entry * 100
  unrealised_usd = supply * (current - entry) * leverage
  Profit when price rises.

Short P&L:
  unrealised_pct = (entry - current) / entry * 100   (raw price %; used for TP/SL)
  unrealised_usd = borrow * (entry - current)
  Profit when price falls.
  (borrow = lev×seed/entry_price asset units — true Aave debt after flash-loan loop;
   leverage amplification is embedded in borrow, so no separate multiplier needed)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PnL:
    entry_price: float
    current_price: float
    direction: str         # "long" | "short"
    unrealised_usd: float
    unrealised_pct: float  # % move from entry, sign = profitable direction
    is_tp: bool
    is_sl: bool


def compute_unrealised(
    entry_price: float,
    current_price: float,
    supply: float,
    borrow: float,
    leverage: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    direction: str = "long",
) -> PnL:
    """
    Calculate unrealised P&L for an open leveraged position.

    supply — long: asset units (1×seed); short: USDC seed (not used in short P&L)
    borrow — long: asset-denominated debt (not used in long P&L); short: lev×seed/price asset units
    """
    if entry_price <= 0:
        return PnL(
            entry_price=entry_price, current_price=current_price,
            direction=direction, unrealised_usd=0.0, unrealised_pct=0.0,
            is_tp=False, is_sl=False,
        )

    if direction == "short":
        pct = (entry_price - current_price) / entry_price * 100
        usd = borrow * (entry_price - current_price)
    else:
        pct = (current_price - entry_price) / entry_price * 100
        usd = supply * (current_price - entry_price) * leverage

    return PnL(
        entry_price=entry_price,
        current_price=current_price,
        direction=direction,
        unrealised_usd=usd,
        unrealised_pct=pct,
        is_tp=pct >= take_profit_pct,
        is_sl=pct <= -stop_loss_pct,
    )


def compute_realised(open_entry: dict, close_price: float) -> float:
    """
    Compute realised P&L in USD from a closed trade log entry.

    Returns realised_usd (positive = profit, negative = loss).
    """
    entry_price: float = float(open_entry.get("entry_price", 0))
    supply: float = float(open_entry.get("supply", 0))
    borrow: float = float(open_entry.get("borrow", 0))
    leverage: float = float(open_entry.get("leverage", 1))
    direction: str = open_entry.get("direction", "long")

    if direction == "short":
        return borrow * (entry_price - close_price)
    return supply * (close_price - entry_price) * leverage


def should_exit(p: PnL) -> Optional[str]:
    """Return 'take_profit', 'stop_loss', or None."""
    if p.is_sl:
        return "stop_loss"
    if p.is_tp:
        return "take_profit"
    return None
