"""
Position size calculator.

Long:
  seed_usd = total_collateral_usd * base_position_pct * signal_multiplier
  supply   = seed_usd / price          (asset units, e.g. ETH)
  borrow   = supply * (leverage - 1)   (USDC)

Short:
  seed_usd = same formula
  supply   = seed_usd                  (USDC units — stable collateral)
  borrow   = seed_usd * (leverage-1) / price  (asset units to short, e.g. WETH)
"""
from dataclasses import dataclass

from bot.config import BotConfig
from bot.signal import Signal


@dataclass
class PositionSize:
    seed_usd: float     # collateral contribution in USD
    supply: float       # long: asset units; short: USDC units
    borrow: float       # long: USDC amount; short: asset units being shorted


def compute(
    total_collateral_usd: float,
    price: float,
    signal: Signal,
    cfg: BotConfig,
) -> PositionSize:
    """
    Compute position size from collateral balance, current price, and signal.

    Returns a zero-size PositionSize when signal.multiplier == 0 (no-trade signal).
    """
    seed_usd = total_collateral_usd * cfg.base_position_pct * signal.multiplier

    if seed_usd <= 0 or price <= 0:
        return PositionSize(seed_usd=0.0, supply=0.0, borrow=0.0)

    if signal.direction == "short":
        supply = seed_usd                          # USDC collateral (stable, 1:1 USD)
        borrow = seed_usd * (cfg.leverage - 1) / price  # asset units to borrow/short
    else:
        supply = seed_usd / price                  # asset units (e.g. ETH)
        borrow = supply * (cfg.leverage - 1)       # USDC to borrow

    return PositionSize(seed_usd=seed_usd, supply=supply, borrow=borrow)
