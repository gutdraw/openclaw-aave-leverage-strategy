"""
Position size calculator.

Long:
  seed_usd = total_collateral_usd * base_position_pct * signal_multiplier
  supply   = seed_usd / price          (asset units, e.g. ETH)
  borrow   = supply * (leverage - 1)   (asset-denominated USDC debt)

Short (MCP flash-loan loop creates supply=(lev+1)×seed USDC, borrow=lev×seed asset):
  seed_usd = same formula
  supply   = seed_usd                  (USDC seed passed to MCP; loop creates (lev+1)×seed on-chain)
  borrow   = seed_usd * leverage / price  (lev×seed in asset units — true Aave debt after loop)
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
    effective_collateral = (
        cfg.paper_seed_usd
        if cfg.paper_trading and cfg.paper_seed_usd > 0
        else total_collateral_usd
    )
    seed_usd = effective_collateral * cfg.base_position_pct * signal.multiplier

    if seed_usd <= 0 or price <= 0:
        return PositionSize(seed_usd=0.0, supply=0.0, borrow=0.0)

    if signal.direction == "short":
        lev = min(cfg.leverage, cfg.short_max_leverage)  # hard cap: 2x for shorts
        supply = seed_usd                          # USDC seed passed to MCP
        borrow = seed_usd * lev / price            # lev×seed in asset units (true Aave debt)
    else:
        lev = cfg.leverage
        supply = seed_usd / price                  # asset units (e.g. ETH)
        borrow = supply * (lev - 1)               # USDC to borrow

    return PositionSize(seed_usd=seed_usd, supply=supply, borrow=borrow)


def compute_increase(
    total_collateral_usd: float,
    price: float,
    signal: Signal,
    cfg: BotConfig,
    current_seed_usd: float,
) -> PositionSize:
    """
    Compute the additional size needed to top up a moderate position to full strength.
    Returns zero-size if already at full size or price is invalid.
    """
    effective_collateral = (
        cfg.paper_seed_usd
        if cfg.paper_trading and cfg.paper_seed_usd > 0
        else total_collateral_usd
    )
    target_seed = effective_collateral * cfg.base_position_pct * cfg.strong_signal_size
    increase_seed = target_seed - current_seed_usd

    if increase_seed <= 0 or price <= 0:
        return PositionSize(seed_usd=0.0, supply=0.0, borrow=0.0)

    if signal.direction == "short":
        lev = min(cfg.leverage, cfg.short_max_leverage)
        supply = increase_seed
        borrow = increase_seed * lev / price
    else:
        lev = cfg.leverage
        supply = increase_seed / price
        borrow = supply * (lev - 1)

    return PositionSize(seed_usd=increase_seed, supply=supply, borrow=borrow)
