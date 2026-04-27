"""
Position size calculator.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LEVERAGE SEMANTICS — READ THIS CAREFULLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"leverage" = BALANCE-SHEET leverage = total_assets / equity.

The seed token is the key difference between longs and shorts:

  LONG  — seed is BTC (cbBTC). BTC exposure = leverage (same as balance-sheet).
    long_leverage=3 → supply 3×seed cbBTC, borrow 2×seed USDC → 3x BTC exposure
    Rule: want Nx long exposure → set long_leverage = N

  SHORT — seed is USDC. BTC exposure = leverage - 1 (seed is NOT BTC).
    short_leverage=3 → supply 3×seed USDC, borrow 2×seed cbBTC → 2x BTC short exposure
    Rule: want Nx short exposure → set short_leverage = N+1

Examples at BTC=$70,000, seed=$50:
  long_leverage=3:  supply 0.00214 cbBTC, borrow $100 USDC → 3x BTC long exposure
  short_leverage=3: supply $150 USDC, borrow 0.00143 cbBTC → 2x BTC short exposure

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Long:
  seed_usd = total_collateral_usd * base_position_pct * signal_multiplier
  supply   = seed_usd / price          (cbBTC units supplied to Aave)
  borrow   = seed_usd * (leverage - 1) (USDC borrowed; BTC long exposure = leverage)

Short (flash-loan loop: supply lev×seed USDC to Aave, borrow (lev-1)×seed asset):
  seed_usd = same formula
  supply   = seed_usd                  (USDC seed; vault flash-loops to lev×seed on-chain)
  borrow   = seed_usd * (leverage-1) / price  (true Aave variableDebt in asset units)
"""

from dataclasses import dataclass

from bot.config import BotConfig
from bot.signal import Signal


@dataclass
class PositionSize:
    seed_usd: float  # collateral contribution in USD
    supply: float  # long: asset units; short: USDC units
    borrow: float  # long: USDC amount; short: asset units being shorted


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

    lev = cfg.leverage_for(signal.direction)
    if signal.direction == "short":
        supply = seed_usd  # USDC seed passed to MCP
        borrow = (
            seed_usd * (lev - 1) / price
        )  # (lev-1)×seed in asset units (true Aave variableDebt)
    else:
        supply = seed_usd / price  # asset units (e.g. ETH)
        borrow = seed_usd * (lev - 1)  # USDC to borrow

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

    lev = cfg.leverage_for(signal.direction)
    if signal.direction == "short":
        supply = increase_seed
        borrow = increase_seed * (lev - 1) / price  # (lev-1)×seed in asset units
    else:
        supply = increase_seed / price
        borrow = increase_seed * (lev - 1)  # USDC to borrow

    return PositionSize(seed_usd=increase_seed, supply=supply, borrow=borrow)
