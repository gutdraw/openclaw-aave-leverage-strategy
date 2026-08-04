"""
Aave leverage strategy bot — main entry point.

Usage:
    python -m bot.main                   # single cycle (cron-friendly)
    python -m bot.main --loop 3600       # loop every N seconds
    python -m bot.main --config path/to/config.yml

Cycle logic (per run):
  1. Load config + state
  2. Fetch market data (3 sources, require ≥2)
  3. Compute trend signal (1h/24h/7d)
  4. Run health-factor defense checks on any open position
  5. Check TP/SL exit on open position
  6. Run no-trade filters
  7. Open long or short — or hold
  8. Append cycle + trade entries to trades.jsonl
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import yaml

import bot.executor as executor
import bot.filters as filters
import bot.market as market
import bot.ohlcv as ohlcv
import bot.pnl as pnl
import bot.signal as signal
import bot.sizing as sizing
import bot.state as state
from bot.config import BotConfig
from bot.mcp_client import MCPClient
from bot.swaps import inject_swap_approve

log = logging.getLogger(__name__)

# Aave v3 Base liquidation thresholds per supply asset (basis: on-chain reserve config)
_LIQ_THRESHOLD: dict[str, float] = {
    "WETH": 0.83,
    "wstETH": 0.82,
    "cbBTC": 0.78,
    "USDC": 0.78,  # used as supply asset in short positions
}


def _paper_health_factor(
    open_trade: Optional[dict],
    price: float,
    cfg: BotConfig,
    eff_supply: float = 0.0,
    eff_borrow: float = 0.0,
) -> float:
    """
    Compute a simulated health factor for a paper position.
    Returns 999.0 when no position is open (no debt).

    Pass eff_supply/eff_borrow to account for any position increases;
    falls back to open_trade values if not provided.

    Long (supply asset, borrow USDC):
      True Aave supply = leverage×seed asset; supply stored as 1×seed.
      HF = (leverage * supply * price * lt) / borrow_usdc

    Short (supply USDC seed, borrow (lev-1)×seed asset):
      True Aave supply = leverage×seed USDC (flash loan loop); supply stored as 1×seed.
      HF = (leverage * supply * lt) / (borrow * price)
    """
    if open_trade is None:
        return 999.0
    direction = open_trade.get("direction", "long")
    supply = eff_supply if eff_supply > 0 else float(open_trade.get("supply", 0))
    borrow = eff_borrow if eff_borrow > 0 else float(open_trade.get("borrow", 0))
    leverage = float(open_trade.get("leverage", 2.0))
    if direction == "short":
        lt = _LIQ_THRESHOLD.get("USDC", 0.78)
        debt_usd = borrow * price
        if debt_usd <= 0:
            return 999.0
        return (leverage * supply * lt) / debt_usd
    else:
        lt = _LIQ_THRESHOLD.get(cfg.asset, 0.80)
        if borrow <= 0:
            return 999.0
        # True Aave supply = leverage×seed; supply stored as 1×seed, so multiply back.
        # HF = (leverage * supply_asset * price * lt) / borrow_usdc
        return (leverage * supply * price * lt) / borrow


def _projected_health_factor(
    direction: str, price: float, size, cfg: BotConfig
) -> float:
    """Estimate the opening HF from the requested seed and effective leverage."""
    if price <= 0 or size.supply <= 0 or size.borrow <= 0:
        return 0.0
    return _paper_health_factor(
        {
            "direction": direction,
            "supply": size.supply,
            "borrow": size.borrow,
            "leverage": cfg.leverage_for(direction),
        },
        price,
        cfg,
    )


def _position_id_for(direction: str, cfg: BotConfig, raw_cfg: dict) -> str:
    """Return the Aave position_id string for the given direction."""
    if direction == "short":
        return raw_cfg.get("short_position_id", f"USDC/{cfg.short_borrow_asset}")
    return raw_cfg.get("position_id", f"{cfg.asset}/USDC")


def _aave_positions(position_data: dict) -> list[dict]:
    raw_positions = (position_data.get("aavePositions") or {}).get("positions")
    if not isinstance(raw_positions, list):
        return []
    return [position for position in raw_positions if isinstance(position, dict)]


def _find_chain_position(
    position_data: dict,
    expected_position_id: str,
    direction: str,
    cfg: BotConfig,
) -> Optional[dict]:
    """Match a local position to one chain position; never guess among many."""
    positions = _aave_positions(position_data)
    if not positions:
        return None

    for position in positions:
        for key in ("positionId", "position_id", "id"):
            value = position.get(key)
            if isinstance(value, str) and value == expected_position_id:
                return position

    expected_supply = "USDC" if direction == "short" else cfg.asset
    expected_borrow = (
        cfg.short_borrow_asset if direction == "short" else cfg.borrow_asset
    )
    if len(positions) == 1:
        position = positions[0]
        declared_supply = str(
            position.get("supplyAsset")
            or position.get("supply_asset")
            or position.get("collateralAsset")
            or ""
        )
        declared_borrow = str(
            position.get("borrowAsset")
            or position.get("borrow_asset")
            or position.get("debtAsset")
            or ""
        )
        if (declared_supply and declared_supply.lower() != expected_supply.lower()) or (
            declared_borrow and declared_borrow.lower() != expected_borrow.lower()
        ):
            return None
        return position

    matches: list[dict] = []
    for position in positions:
        supply = str(
            position.get("supplyAsset")
            or position.get("supply_asset")
            or position.get("collateralAsset")
            or ""
        )
        borrow = str(
            position.get("borrowAsset")
            or position.get("borrow_asset")
            or position.get("debtAsset")
            or ""
        )
        if (
            supply.lower() == expected_supply.lower()
            and borrow.lower() == expected_borrow.lower()
        ):
            matches.append(position)
    return matches[0] if len(matches) == 1 else None


def _chain_position_size(
    position: Optional[dict],
    direction: str,
    cfg: BotConfig,
    leverage: Optional[float] = None,
) -> tuple[float, float]:
    if position is None:
        return 0.0, 0.0
    try:
        atoken_balance = float(position.get("aTokenBalance", 0) or 0)
        variable_debt = float(position.get("variableDebt", 0) or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if atoken_balance <= 0 or variable_debt <= 0:
        return 0.0, 0.0
    effective_leverage = leverage or cfg.leverage_for(direction)
    if effective_leverage <= 0:
        return 0.0, 0.0
    if direction == "long":
        return atoken_balance / effective_leverage, variable_debt
    return atoken_balance, variable_debt


def _build_signer(cfg: BotConfig):
    if cfg.paper_trading:
        return None
    pk = cfg.private_key or os.environ.get("PRIVATE_KEY", "")
    if not pk:
        raise RuntimeError("PRIVATE_KEY is required in live mode")
    from bot.signer import Signer

    rpc = os.environ.get("RPC_URL", cfg.rpc_url)
    signer = Signer(rpc_url=rpc, private_key=pk)
    if signer.address.lower() != cfg.user_address.lower():
        raise RuntimeError(
            "PRIVATE_KEY address does not match configured user_address; refusing live mode"
        )
    return signer


# ── Pre-open token swap ───────────────────────────────────────────────────────


def _ensure_wallet_token(
    direction: str,
    seed_usd: float,
    data,
    cfg: BotConfig,
    mcp: MCPClient,
    signer,
    cycle_entry: dict,
) -> bool | None:
    """
    Ensure the wallet holds the correct token before opening a position.

    - Short: needs USDC as seed. If wallet only has the borrow asset (or cfg.asset),
      or has a mixture of USDC and that asset, swap the asset portion to USDC.
    - Long: needs the supply asset. If wallet only has USDC, swap USDC → asset.

    Returns:
        True  — correct token already present (no swap needed)
        None  — swap executed successfully
        False — insufficient wallet funds; caller should skip and log cycle_entry

    Adds a 0.2% input buffer to swap amounts to cover DEX fees and drift.
    """
    wb = (
        data.position_data.get("tokenBalances")
        or data.position_data.get("wallet_balances")
        or {}
    )
    _SLIPPAGE = (
        1.002  # 0.2% buffer — covers 0.05% Uniswap v3 fee + ~0.1% execution price drift
    )

    def _swap_asset_to_usdc(tok: str, qty: float) -> None:
        swap_hash = signer.execute_steps(
            inject_swap_approve(mcp.swap(tok, "USDC", qty))
        )
        cycle_entry["pre_swap"] = f"{qty:.6f} {tok} → USDC (tx={swap_hash})"
        log.info("waiting for swap confirmation: %s", swap_hash)
        signer.wait_for_receipt(swap_hash)
        time.sleep(
            3
        )  # brief pause for RPC propagation before prepare_open reads balance
        log.info("swap confirmed — proceeding to open")

    if direction == "short":
        # Short flash-loan loop needs USDC as collateral seed.
        # Prefer spending the asset (cbBTC/WETH) first so USDC stays as the
        # standing reserve, but only swap the amount needed for this position.
        # Do not liquidate unrelated wallet balances just to make the bot flat.
        usdc_bal = float(wb.get("USDC", 0) or 0)

        asset_tokens = tuple(dict.fromkeys((cfg.short_borrow_asset, cfg.asset)))
        asset_balances = {
            tok: max(float(wb.get(tok, 0) or 0), 0.0) for tok in asset_tokens
        }
        total_wallet_usd = usdc_bal + sum(
            float(wb.get(tok, 0) or 0) * data.price
            for tok in asset_tokens
            if tok != "USDC"
        )

        if total_wallet_usd < seed_usd * 0.995:
            log.warning(
                "Insufficient wallet funds for short: need %.2f USD, wallet=%.2f",
                seed_usd,
                total_wallet_usd,
            )
            cycle_entry["decision"] = "skip_insufficient_funds"
            return False

        shortfall_usd = max(seed_usd - usdc_bal, 0.0)
        if shortfall_usd <= seed_usd * 0.005:
            return True

        swapped = False
        for tok in asset_tokens:
            tok_bal = asset_balances[tok]
            if tok == "USDC" or tok_bal <= 0 or shortfall_usd <= 0:
                continue
            available_usd = tok_bal * data.price
            # Add a small input buffer for DEX fees/price movement, while
            # never requesting more than the required shortfall.
            swap_usd = min(available_usd, shortfall_usd * _SLIPPAGE)
            if swap_usd <= 0:
                continue
            _swap_asset_to_usdc(tok, swap_usd / data.price)
            swapped = True
            shortfall_usd = max(0.0, shortfall_usd - swap_usd / _SLIPPAGE)

        if swapped and shortfall_usd <= seed_usd * 0.005:
            return None

        log.warning(
            "Wallet funding swaps did not cover short seed: need %.2f USD, "
            "remaining shortfall=%.2f — skip",
            seed_usd,
            shortfall_usd,
        )
        cycle_entry["decision"] = "skip_insufficient_funds"
        return False

    else:
        # Long flash-loan loop needs the supply asset
        asset_bal = float(wb.get(cfg.asset, 0) or 0)
        asset_val_usd = asset_bal * data.price
        supply_needed_usd = seed_usd  # seed_usd = supply × price
        if asset_val_usd >= supply_needed_usd * 0.95:
            return True  # already have enough of the asset

        # Top up with USDC — swap only the shortfall (or full amount if no asset)
        usdc_bal = float(wb.get("USDC", 0) or 0)
        shortfall_usd = supply_needed_usd - asset_val_usd
        if usdc_bal >= shortfall_usd * 0.95:
            swap_usd = min(shortfall_usd * _SLIPPAGE, usdc_bal)
            log.info(
                "Swapping %.2f USDC → %s (have %.2f USD of asset, need %.2f, topping up shortfall)",
                swap_usd,
                cfg.asset,
                asset_val_usd,
                supply_needed_usd,
            )
            swap_hash = signer.execute_steps(
                inject_swap_approve(mcp.swap("USDC", cfg.asset, swap_usd))
            )
            cycle_entry["pre_swap"] = (
                f"{swap_usd:.2f} USDC → {cfg.asset} (tx={swap_hash})"
            )
            log.info("waiting for swap confirmation: %s", swap_hash)
            signer.wait_for_receipt(swap_hash)
            time.sleep(
                3
            )  # brief pause for RPC propagation before prepare_open reads balance
            log.info("swap confirmed — proceeding to open")
            return None

        log.warning(
            "Insufficient wallet funds for long: need ~%.2f USD of %s, "
            "wallet %s=%.6f (%.2f USD) USDC=%.2f — skip",
            supply_needed_usd,
            cfg.asset,
            cfg.asset,
            asset_bal,
            asset_val_usd,
            usdc_bal,
        )
        cycle_entry["decision"] = "skip_insufficient_funds"
        return False


# ── Single cycle ──────────────────────────────────────────────────────────────


def run_cycle(
    cfg: BotConfig, raw_cfg: dict, signer=None, mcp: MCPClient = None
) -> dict:
    """Run one full strategy cycle. Returns the cycle log entry."""
    if mcp is None:
        mcp = MCPClient(
            base_url=cfg.mcp_url,
            session_token=cfg.mcp_session_token,
            wallet_address=cfg.user_address,
            private_key=cfg.private_key or os.environ.get("PRIVATE_KEY", ""),
            config_path=cfg._config_path,
            session_duration=cfg.mcp_session_duration,
        )
    if signer is None:
        signer = _build_signer(cfg)

    # ── 1. State ──────────────────────────────────────────────────────────
    entries = state.load_entries(cfg.trades_file)
    open_trade: Optional[dict] = state.get_open_trade(entries)
    btc_dom_prev: Optional[float] = state.get_last_btc_dominance(entries)
    eff_supply, eff_borrow, eff_entry_price = state.get_effective_size(
        open_trade, entries
    )
    already_increased: bool = state.has_been_increased(open_trade, entries)

    # ── 2. Market data ────────────────────────────────────────────────────
    data, sources_failed = market.fetch(
        cfg.asset,
        mcp,
        rpc_url=cfg.rpc_url,
        onchain_lookback_blocks=cfg.onchain_lookback_blocks,
        short_borrow_asset=cfg.short_borrow_asset,
    )

    # In paper mode, replace on-chain HF with a simulated value derived from
    # the paper position — real wallet HF belongs to whatever is live on-chain
    # and should not influence paper trading decisions.
    if cfg.paper_trading:
        data.health_factor = _paper_health_factor(
            open_trade, data.price, cfg, eff_supply, eff_borrow
        )

    # ── 3. Signal ─────────────────────────────────────────────────────────
    # Primary: OHLCV-based EMA crossover + RSI (Coinbase → Kraken fallback)
    # Last resort: CoinGecko 3-timeframe momentum (only if all OHLCV sources fail)
    cg_sig = signal.compute(data.change_1h, data.change_24h, data.change_7d)
    tech = ohlcv.fetch(cfg.asset)
    tech_sig = ohlcv.to_signal(tech) if tech is not None else None

    if tech_sig is not None:
        sig = tech_sig  # OHLCV available — use it exclusively
    else:
        sig = cg_sig  # all OHLCV sources failed — fall back to CoinGecko

    cycle_entry: dict = {
        "type": "cycle",
        "ts": state.now_iso(),
        "asset": cfg.asset,
        "price": data.price,
        "change_1h": data.change_1h,
        "change_24h": data.change_24h,
        "change_7d": data.change_7d,
        "signal": sig.label,
        "direction": sig.direction,
        "score": sig.score,
        "borrow_apr": data.borrow_apr,
        "short_borrow_apr": data.short_borrow_apr,
        "health_factor": data.health_factor,
        "btc_dominance_pct": data.btc_dominance,
        "funding_rate": data.funding_rate,
        "fear_greed": data.fear_greed,
        "volume_24h": data.volume_24h,
        "usdc_utilization": round(data.usdc_utilization, 4)
        if data.usdc_utilization is not None
        else None,
        "asset_utilization": round(data.asset_utilization, 4)
        if data.asset_utilization is not None
        else None,
        "short_asset_utilization": round(data.short_asset_utilization, 4)
        if data.short_asset_utilization is not None
        else None,
        "recent_liquidations": data.recent_liquidations,
        "usdc_supply_apy": data.usdc_supply_apy,
        "asset_borrow_apy": data.asset_borrow_apy,
        # Flash-loan loop creates supply=lev×seed USDC, borrow=(lev−1)×seed asset.
        # carry = usdc_supply_apy × lev − asset_borrow_apy × (lev−1)
        "short_carry_apr": (
            round(
                data.usdc_supply_apy * cfg.leverage_for("short")
                - data.asset_borrow_apy * (cfg.leverage_for("short") - 1),
                4,
            )
            if data.usdc_supply_apy is not None and data.asset_borrow_apy is not None
            else None
        ),
        "wallet_collateral_usd": round(data.wallet_collateral_usd, 2),
        "position_data_available": data.position_available,
        "onchain_data_available": data.onchain_available,
        "sources_failed": sources_failed,
        "paper_trading": cfg.paper_trading,
        "cg_signal": cg_sig.label,
        "tech_signal": tech_sig.label if tech_sig is not None else None,
        "tech_ema_bull": tech.ema_bull if tech is not None else None,
        "tech_rsi": tech.rsi if tech is not None else None,
        "tech_source": tech.source if tech is not None else None,
        "tech_mid_bull": tech.tf_mid_bull if tech is not None else None,
        "tech_1d_bull": tech.tf_1d_bull if tech is not None else None,
        "tech_obv_bull": tech.obv_bull if tech is not None else None,
        "tech_macd_bull": tech.macd_bull if tech is not None else None,
        "tech_adx": tech.adx if tech is not None else None,
        "tech_volume_ratio": tech.volume_ratio if tech is not None else None,
    }

    if not cfg.paper_trading and (
        not data.position_available or not data.onchain_available
    ):
        log.warning(
            "Live safety snapshot incomplete (position=%s onchain=%s) — skipping cycle",
            data.position_available,
            data.onchain_available,
        )
        cycle_entry["decision"] = "skip_safety_data_unavailable"
        state.append_entry(cfg.trades_file, cycle_entry)
        return cycle_entry

    chain_position: Optional[dict] = None
    if not cfg.paper_trading:
        if open_trade is not None:
            expected_id = str(open_trade.get("position_id") or "")
            chain_position = _find_chain_position(
                data.position_data,
                expected_id,
                open_trade.get("direction", "long"),
                cfg,
            )
            if chain_position is None:
                log.error(
                    "Local open trade does not match exactly one on-chain position — "
                    "skipping without acting"
                )
                cycle_entry["decision"] = "skip_state_reconciliation"
                state.append_entry(cfg.trades_file, cycle_entry)
                return cycle_entry
            chain_supply, chain_borrow = _chain_position_size(
                chain_position,
                open_trade.get("direction", "long"),
                cfg,
                float(
                    open_trade.get("leverage")
                    or cfg.leverage_for(open_trade.get("direction", "long"))
                ),
            )
            if chain_supply > 0 and chain_borrow > 0:
                eff_supply, eff_borrow = chain_supply, chain_borrow
        elif _aave_positions(data.position_data):
            log.error(
                "On-chain Aave position exists while local state is flat — skipping"
            )
            cycle_entry["decision"] = "skip_state_reconciliation"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

    # Derive the position_id for the current open trade (if any)
    open_direction = (
        open_trade.get("direction", "long") if open_trade else sig.direction
    )
    pos_id = _position_id_for(open_direction, cfg, raw_cfg)

    # ── 4. Health-factor defense ──────────────────────────────────────────
    if open_trade is not None:
        hf = data.health_factor

        # Use direction-aware thresholds: 2x short opens at HF ~1.17, so short
        # thresholds must be below that to avoid triggering immediately after open.
        is_short_pos = open_direction == "short"
        hf_close = cfg.short_hf_defense_close if is_short_pos else cfg.hf_defense_close
        hf_reduce = (
            cfg.short_hf_defense_reduce if is_short_pos else cfg.hf_defense_reduce
        )

        if hf < hf_close:
            log.warning("HF %.3f < %.3f — force close", hf, hf_close)
            res = executor.close_position(
                pos_id,
                open_direction,
                eff_supply,
                cfg,
                mcp,
                signer,
            )
            trade_entry = _close_trade_entry(
                open_trade,
                data.price,
                cfg,
                "hf_close",
                res,
                eff_supply,
                eff_borrow,
                eff_entry_price,
            )
            state.append_entry(cfg.trades_file, cycle_entry | {"decision": "hf_close"})
            state.append_entry(cfg.trades_file, trade_entry)
            return cycle_entry

        if hf < hf_reduce:
            log.warning("HF %.3f < %.3f — reduce", hf, hf_reduce)
            target_lev = max(cfg.leverage_for(open_direction) / 2, 1.5)
            res = executor.reduce_position(
                pos_id, open_direction, target_lev, cfg, mcp, signer
            )
            reduce_entry = {
                "type": "trade",
                "action": "reduce",
                "ts": state.now_iso(),
                "asset": cfg.asset,
                "direction": open_direction,
                "position_id": pos_id,
                "target_leverage": target_lev,
                "price": data.price,
                "paper": cfg.paper_trading,
                "tx_hash": res.tx_hash,
            }
            if not cfg.paper_trading:
                try:
                    reduced_data = mcp.get_position()
                    reduced_position = _find_chain_position(
                        reduced_data, pos_id, open_direction, cfg
                    )
                    reduced_supply, reduced_borrow = _chain_position_size(
                        reduced_position, open_direction, cfg, target_lev
                    )
                    if reduced_supply > 0 and reduced_borrow > 0:
                        reduce_entry.update(
                            supply=reduced_supply,
                            borrow=reduced_borrow,
                            reconciled=True,
                        )
                except Exception as e:
                    log.warning("post-reduce reconciliation failed: %s", e)
            cycle_entry["decision"] = "hf_reduce"
            state.append_entry(cfg.trades_file, cycle_entry)
            state.append_entry(cfg.trades_file, reduce_entry)
            return cycle_entry

    # ── 4a. Liquidity escape ──────────────────────────────────────────────
    # Close an open position proactively when pool liquidity is drying up.
    # We rely on flash loans to close — if the flash-loan asset's pool hits
    # 100% utilization we can't close at all. Exit well before that point.
    #
    # For longs  (flash USDC):       watch usdc_utilization
    # For shorts (flash borrow asset): watch asset_utilization
    #
    # Also close immediately on Aave governance freeze/pause of any involved
    # asset (e.g. KelpDAO-style incident).
    if open_trade is not None and not cfg.paper_trading:
        prev_usdc_util, _ = state.get_last_utilizations(entries)
        prev_short_asset_util = state.get_last_short_asset_utilization(entries)

        if open_direction == "long":
            flash_util = data.usdc_utilization
            prev_flash_util = prev_usdc_util
            flash_frozen = data.borrow_asset_frozen  # USDC frozen → no flash loan
            flash_paused = data.borrow_asset_paused  # USDC paused → nothing works
            supply_paused = data.asset_paused  # supply asset paused → can't withdraw
        else:
            flash_util = data.short_asset_utilization
            prev_flash_util = prev_short_asset_util
            flash_frozen = (
                data.short_asset_frozen
            )  # borrowed asset frozen → no flash loan
            flash_paused = (
                data.short_asset_paused
            )  # borrowed asset paused → nothing works
            supply_paused = data.borrow_asset_paused  # USDC paused → can't withdraw

        escape_reason: Optional[str] = None

        if flash_paused or supply_paused:
            escape_reason = "liquidity_escape_paused"
            log.critical(
                "EMERGENCY: asset paused on Aave (flash_paused=%s supply_paused=%s) "
                "— closing %s position immediately",
                flash_paused,
                supply_paused,
                open_direction,
            )
        elif flash_frozen:
            escape_reason = "liquidity_escape_frozen"
            log.warning(
                "Flash-loan asset frozen on Aave — closing %s position immediately",
                open_direction,
            )
        elif flash_util is not None and flash_util > cfg.liquidity_escape_utilization:
            escape_reason = "liquidity_escape_utilization"
            log.warning(
                "Flash-asset pool utilization %.1f%% > %.1f%% threshold "
                "— closing %s position before liquidity dries up",
                flash_util * 100,
                cfg.liquidity_escape_utilization * 100,
                open_direction,
            )
        elif (
            flash_util is not None
            and prev_flash_util is not None
            and (flash_util - prev_flash_util) > cfg.liquidity_escape_velocity
        ):
            escape_reason = "liquidity_escape_velocity"
            log.warning(
                "Flash-asset pool utilization jumped %.1f→%.1f%% (delta=%.1f%%) "
                "in one cycle — closing %s position before cascade",
                prev_flash_util * 100,
                flash_util * 100,
                (flash_util - prev_flash_util) * 100,
                open_direction,
            )

        if escape_reason:
            res = executor.close_position(
                pos_id,
                open_direction,
                eff_supply,
                cfg,
                mcp,
                signer,
            )
            trade_entry = _close_trade_entry(
                open_trade,
                data.price,
                cfg,
                escape_reason,
                res,
                eff_supply,
                eff_borrow,
                eff_entry_price,
            )
            state.append_entry(
                cfg.trades_file, cycle_entry | {"decision": escape_reason}
            )
            state.append_entry(cfg.trades_file, trade_entry)
            return cycle_entry

    # ── 5. Exit check (TP / SL) on open position ──────────────────────────
    # Runs before signal reversal — price-based stops are deterministic and
    # should always take priority over signal-based exits.
    if open_trade is not None:
        # Use borrow-weighted avg entry price so increases don't inflate P&L
        entry_price = (
            eff_entry_price
            if eff_entry_price > 0
            else float(open_trade.get("entry_price", 0))
        )
        supply_units = (
            eff_supply if eff_supply > 0 else float(open_trade.get("supply", 0))
        )
        borrow_units = (
            eff_borrow if eff_borrow > 0 else float(open_trade.get("borrow", 0))
        )
        trade_lev = float(open_trade.get("leverage", cfg.leverage))
        p = pnl.compute_unrealised(
            entry_price=entry_price,
            current_price=data.price,
            supply=supply_units,
            borrow=borrow_units,
            leverage=trade_lev,
            take_profit_pct=cfg.tp_for(open_direction),
            stop_loss_pct=cfg.sl_for(open_direction),
            direction=open_direction,
        )
        cycle_entry["unrealised_usd"] = round(p.unrealised_usd, 2)
        cycle_entry["unrealised_pct"] = round(p.unrealised_pct, 4)

        exit_reason = pnl.should_exit(p)
        # Suppress TP (but not SL) when signal is still at maximum strength in the
        # trade direction — trend-following: let winners ride until the signal fades.
        if (
            exit_reason == "take_profit"
            and not cfg.tp_on_strong_signal
            and (
                (open_direction == "long" and sig.score == 3)
                or (open_direction == "short" and sig.score == 0)
            )
        ):
            log.info(
                "TP reached (%.2f%%) but signal still strong (%s) — holding",
                p.unrealised_pct,
                sig.label,
            )
            exit_reason = None
        if exit_reason:
            log.info("Exit triggered: %s %.2f%%", exit_reason, p.unrealised_pct)
            res = executor.close_position(
                pos_id, open_direction, supply_units, cfg, mcp, signer
            )
            trade_entry = _close_trade_entry(
                open_trade,
                data.price,
                cfg,
                exit_reason,
                res,
                eff_supply,
                eff_borrow,
                eff_entry_price,
            )
            cycle_entry["decision"] = exit_reason
            state.append_entry(cfg.trades_file, cycle_entry)
            state.append_entry(cfg.trades_file, trade_entry)
            return cycle_entry

    # ── 5a. Trailing stop ─────────────────────────────────────────────
    # Close if price retreats trailing_stop_pct% from the highest (long) or
    # lowest (short) price since the position was opened.
    # Respects min_hold_hours so it doesn't fire on the very first candle.
    _trail_pct = (
        cfg.long_trailing_stop_pct
        if open_direction == "long" and cfg.long_trailing_stop_pct > 0
        else cfg.short_trailing_stop_pct
        if open_direction == "short" and cfg.short_trailing_stop_pct > 0
        else cfg.trailing_stop_pct
    )
    if open_trade is not None and _trail_pct > 0:
        peak = state.get_position_peak(entries)
        if peak > 0:
            if open_direction == "long":
                trail_drawdown = 100.0 * (peak - data.price) / peak
                trail_triggered = trail_drawdown >= _trail_pct
            else:  # short: price rising from trough is bad
                trail_drawdown = 100.0 * (data.price - peak) / peak
                trail_triggered = trail_drawdown >= _trail_pct

            if trail_triggered:
                hold_ok = True
                if cfg.min_hold_hours > 0:
                    open_ts = open_trade.get("ts", "")
                    try:
                        opened = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
                        age_hours = (
                            datetime.now(timezone.utc) - opened
                        ).total_seconds() / 3600
                        if age_hours < cfg.min_hold_hours:
                            hold_ok = False
                    except (ValueError, TypeError):
                        pass

                if hold_ok:
                    log.info(
                        "Trailing stop: peak=%.2f current=%.2f drawdown=%.2f%% >= %.2f%%",
                        peak,
                        data.price,
                        trail_drawdown,
                        _trail_pct,
                    )
                    res = executor.close_position(
                        pos_id, open_direction, supply_units, cfg, mcp, signer
                    )
                    trade_entry = _close_trade_entry(
                        open_trade,
                        data.price,
                        cfg,
                        "trailing_stop",
                        res,
                        eff_supply,
                        eff_borrow,
                        eff_entry_price,
                    )
                    cycle_entry["decision"] = "trailing_stop"
                    state.append_entry(cfg.trades_file, cycle_entry)
                    state.append_entry(cfg.trades_file, trade_entry)
                    return cycle_entry

    # ── 5b. Signal reversal exit ──────────────────────────────────────
    # Requires an actively opposing signal direction — "hold" (score=0, direction="none")
    # does not count as a reversal even though it shares score=0 with strong_short.
    if open_trade is not None and cfg.signal_reversal_exit:
        is_long_pos = open_direction == "long"
        is_short_pos = open_direction == "short"
        reversal = (
            is_long_pos
            and sig.direction == "short"
            and sig.score <= cfg.signal_reversal_min_score
        ) or (
            is_short_pos
            and sig.direction == "long"
            and sig.score >= (3 - cfg.signal_reversal_min_score)
        )
        if reversal:
            hold_ok = True
            if cfg.min_hold_hours > 0:
                open_ts = open_trade.get("ts", "")
                try:
                    opened = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
                    age_hours = (
                        datetime.now(timezone.utc) - opened
                    ).total_seconds() / 3600
                    if age_hours < cfg.min_hold_hours:
                        log.info(
                            "Signal reversal (score=%d) but hold %.1fh < min %.1fh — hold",
                            sig.score,
                            age_hours,
                            cfg.min_hold_hours,
                        )
                        hold_ok = False
                except (ValueError, TypeError):
                    pass
            if hold_ok:
                log.info(
                    "Signal reversal exit: %s position, signal=%s score=%d",
                    open_direction,
                    sig.label,
                    sig.score,
                )
                res = executor.close_position(
                    pos_id, open_direction, supply_units, cfg, mcp, signer
                )
                trade_entry = _close_trade_entry(
                    open_trade,
                    data.price,
                    cfg,
                    "signal_reversal",
                    res,
                    eff_supply,
                    eff_borrow,
                    eff_entry_price,
                )
                cycle_entry["decision"] = "signal_reversal"
                state.append_entry(cfg.trades_file, cycle_entry)
                state.append_entry(cfg.trades_file, trade_entry)
                return cycle_entry

    # ── 5b. Time-based exit ───────────────────────────────────────────
    _max_hold = (
        cfg.long_max_hold_days
        if open_direction == "long" and cfg.long_max_hold_days > 0
        else cfg.max_hold_days
    )
    if open_trade is not None and _max_hold > 0:
        open_ts = open_trade.get("ts", "")
        if open_ts:
            try:
                opened = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - opened).total_seconds() / 86400
                still_strong = (
                    open_direction == "short"
                    and sig.score == 0
                    or open_direction == "long"
                    and sig.score >= 3
                )
                if age_days >= _max_hold and still_strong:
                    log.info(
                        "Time-based exit skipped: age %.1fd >= %.1fd but signal still strong (%s)",
                        age_days,
                        _max_hold,
                        sig.label,
                    )
                elif age_days >= _max_hold:
                    log.info(
                        "Time-based exit: position age %.1fd >= max_hold_days %.1fd",
                        age_days,
                        _max_hold,
                    )
                    res = executor.close_position(
                        pos_id, open_direction, supply_units, cfg, mcp, signer
                    )
                    trade_entry = _close_trade_entry(
                        open_trade,
                        data.price,
                        cfg,
                        "max_hold_days",
                        res,
                        eff_supply,
                        eff_borrow,
                        eff_entry_price,
                    )
                    cycle_entry["decision"] = "max_hold_days"
                    state.append_entry(cfg.trades_file, cycle_entry)
                    state.append_entry(cfg.trades_file, trade_entry)
                    return cycle_entry
            except (ValueError, TypeError):
                pass  # malformed ts — skip time exit this cycle

    # ── 5d. Increase position (moderate → strong signal upgrade) ─────────
    if (
        open_trade is not None
        and sig.direction == open_direction
        and not already_increased
    ):
        open_signal = open_trade.get("signal", "")
        # Shorts always open at full size (sizing.py), so mid-position increases
        # are longs-only. prepare_increase for shorts targets leverage, not seed.
        signal_upgraded = (
            open_direction == "long" and sig.score == 3 and open_signal != "strong_long"
        )

        if signal_upgraded:
            current_seed = float(open_trade.get("seed_usd", 0))
            eff_collateral = data.total_collateral_usd or data.wallet_collateral_usd
            delta = sizing.compute_increase(
                eff_collateral, data.price, sig, cfg, current_seed
            )
            if delta.supply > 0:
                log.info(
                    "Signal upgraded to %s — increasing position by seed_usd=%.2f",
                    sig.label,
                    delta.seed_usd,
                )
                res = executor.increase_position(
                    delta, open_direction, pos_id, cfg, mcp, signer
                )
                increase_entry = {
                    "type": "trade",
                    "action": "increase",
                    "ts": state.now_iso(),
                    "asset": cfg.asset,
                    "direction": open_direction,
                    "position_id": pos_id,
                    "signal": sig.label,
                    "price": data.price,
                    "add_supply": delta.supply,
                    "add_borrow": delta.borrow,
                    "add_seed_usd": round(delta.seed_usd, 2),
                    "paper": cfg.paper_trading,
                    "tx_hash": res.tx_hash,
                }
                cycle_entry["decision"] = f"increase_{open_direction}"
                state.append_entry(cfg.trades_file, cycle_entry)
                state.append_entry(cfg.trades_file, increase_entry)
                return cycle_entry

    # ── 6. No-trade filters ───────────────────────────────────────────────
    filt = filters.apply_all(
        data,
        sig.label,
        sig.direction,
        open_trade,
        btc_dom_prev,
        cfg,
        ohlcv_rsi=tech.rsi if tech is not None else None,
    )
    if filt.blocked:
        log.info("Filtered: %s", filt.decision)
        cycle_entry["decision"] = filt.decision
        state.append_entry(cfg.trades_file, cycle_entry)
        return cycle_entry

    # ── 7. Open new position ──────────────────────────────────────────────
    # Post-TP consistency gate: if the last close was a TP in this same direction,
    # only allow a same-direction reopen if the signal is at maximum strength —
    # the same condition that would have suppressed the TP.  This prevents the
    # awkward sequence of TP → immediate reopen at moderate signal conviction.
    # Gate is skipped when tp_on_strong_signal=True (TP always fires, no suppression).
    if open_trade is None and sig.multiplier > 0 and not cfg.tp_on_strong_signal:
        last_close = state.get_last_close(entries)
        if (
            last_close is not None
            and last_close.get("reason") == "take_profit"
            and last_close.get("direction") == sig.direction
        ):
            # Gate expires after post_tp_gate_hours — prevents indefinite blocking
            # in range-bound markets where strong signal never fires.
            gate_active = True
            if cfg.post_tp_gate_hours > 0:
                try:
                    tp_time = datetime.fromisoformat(
                        last_close["ts"].replace("Z", "+00:00")
                    )
                    hours_since = (
                        datetime.now(timezone.utc) - tp_time
                    ).total_seconds() / 3600
                    if hours_since >= cfg.post_tp_gate_hours:
                        gate_active = False
                        log.info(
                            "Post-TP gate expired (%.1fh > %.1fh) — allowing reopen",
                            hours_since,
                            cfg.post_tp_gate_hours,
                        )
                except (KeyError, ValueError, TypeError):
                    pass
            if gate_active:
                is_max_strength = (sig.direction == "long" and sig.score == 3) or (
                    sig.direction == "short" and sig.score == 0
                )
                if not is_max_strength:
                    log.info(
                        "Post-TP gate: last %s close was TP, signal %s not at max strength — skip",
                        sig.direction,
                        sig.label,
                    )
                    cycle_entry["decision"] = "skip_post_tp"
                    state.append_entry(cfg.trades_file, cycle_entry)
                    return cycle_entry

    # Post-trailing-stop gate: after a stop-out, require strong signal to reopen
    # same direction. Prevents immediately re-entering a failing move on moderate conviction.
    if (
        open_trade is None
        and sig.multiplier > 0
        and cfg.post_trailing_stop_gate_hours >= 0
    ):
        last_close = state.get_last_close(entries)
        if (
            last_close is not None
            and last_close.get("reason") == "trailing_stop"
            and last_close.get("direction") == sig.direction
        ):
            gate_active = True
            if cfg.post_trailing_stop_gate_hours > 0:
                try:
                    stop_time = datetime.fromisoformat(
                        last_close["ts"].replace("Z", "+00:00")
                    )
                    hours_since = (
                        datetime.now(timezone.utc) - stop_time
                    ).total_seconds() / 3600
                    if hours_since >= cfg.post_trailing_stop_gate_hours:
                        gate_active = False
                        log.info(
                            "Post-trailing-stop gate expired (%.1fh > %.1fh) — allowing reopen",
                            hours_since,
                            cfg.post_trailing_stop_gate_hours,
                        )
                except (KeyError, ValueError, TypeError):
                    pass
            if gate_active:
                is_max_strength = (sig.direction == "long" and sig.score == 3) or (
                    sig.direction == "short" and sig.score == 0
                )
                if not is_max_strength:
                    log.info(
                        "Post-trailing-stop gate: last %s close was stop-out, signal %s not at max strength — skip",
                        sig.direction,
                        sig.label,
                    )
                    cycle_entry["decision"] = "skip_post_trailing_stop"
                    state.append_entry(cfg.trades_file, cycle_entry)
                    return cycle_entry

    # Post-time-exit gate: after a max_hold_days close, block same-direction reopen
    # for post_max_hold_gate_hours to prevent immediately re-entering on the same
    # stalled signal that just triggered the time exit.
    if open_trade is None and sig.multiplier > 0 and cfg.post_max_hold_gate_hours > 0:
        last_close = state.get_last_close(entries)
        if (
            last_close is not None
            and last_close.get("reason") == "max_hold_days"
            and last_close.get("direction") == sig.direction
        ):
            try:
                close_time = datetime.fromisoformat(
                    last_close["ts"].replace("Z", "+00:00")
                )
                hours_since = (
                    datetime.now(timezone.utc) - close_time
                ).total_seconds() / 3600
                if hours_since < cfg.post_max_hold_gate_hours:
                    log.info(
                        "Post-time-exit gate: %.1fh since max_hold_days close (gate=%.1fh) — skip",
                        hours_since,
                        cfg.post_max_hold_gate_hours,
                    )
                    cycle_entry["decision"] = "skip_post_max_hold"
                    state.append_entry(cfg.trades_file, cycle_entry)
                    return cycle_entry
            except (KeyError, ValueError, TypeError):
                pass

    # Moderate-short filter: regime-aware gate or legacy strong-only block.
    if open_trade is None and sig.direction == "short" and sig.score != 0:
        if cfg.moderate_short_min_7d_change < 0:
            # Regime filter: allow moderate_short only in confirmed downtrends.
            # Backtest: change_7d < -2% AND change_24h < 0 → 100% WR.
            regime_ok = (
                data.change_7d < cfg.moderate_short_min_7d_change
                and data.change_24h < 0
            )
            if not regime_ok:
                log.info(
                    "moderate_short regime filter: 7d=%.1f%% (need <%.1f%%) "
                    "24h=%.1f%% (need <0) — skip",
                    data.change_7d,
                    cfg.moderate_short_min_7d_change,
                    data.change_24h,
                )
                cycle_entry["decision"] = "skip_moderate_short"
                state.append_entry(cfg.trades_file, cycle_entry)
                return cycle_entry
        elif cfg.require_strong_short:
            log.info(
                "require_strong_short: signal %s (score=%d) not strong_short — skip",
                sig.label,
                sig.score,
            )
            cycle_entry["decision"] = "skip_moderate_short"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

    # EMA bull gate: block long entries when price is below the EMA (downtrend).
    # Backtest: EMA_bull=True → 67% WR vs 17% WR when False (+50pp edge).
    if (
        open_trade is None
        and sig.direction == "long"
        and cfg.require_ema_bull_long
        and tech is not None
        and not tech.ema_bull
    ):
        log.info("require_ema_bull_long: EMA bearish — skip long entry")
        cycle_entry["decision"] = "skip_ema_bearish"
        state.append_entry(cfg.trades_file, cycle_entry)
        return cycle_entry

    # RSI gate: block long entries below minimum RSI threshold.
    # Backtest: long winners avg RSI 55.9 vs losers 37.1 — avoid catching falling knives.
    if (
        open_trade is None
        and sig.direction == "long"
        and cfg.min_rsi_long > 0
        and tech is not None
        and tech.rsi is not None
        and tech.rsi < cfg.min_rsi_long
    ):
        log.info(
            "min_rsi_long: RSI %.1f < %.1f — skip long entry",
            tech.rsi,
            cfg.min_rsi_long,
        )
        cycle_entry["decision"] = "skip_rsi_low"
        state.append_entry(cfg.trades_file, cycle_entry)
        return cycle_entry

    if open_trade is None and sig.multiplier > 0:
        eff_collateral = data.total_collateral_usd or data.wallet_collateral_usd
        size = sizing.compute(eff_collateral, data.price, sig, cfg)
        if size.supply <= 0:
            cycle_entry["decision"] = "skip_zero_size"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

        # In live mode: ensure the wallet holds the right token for this position type.
        # Shorts need USDC as seed; longs need the supply asset.
        # For shorts: always run — wallet may have cbBTC from a previous long close even
        # when Aave still has a balance (e.g. between close and new open).
        # For longs: only needed when Aave is empty (total_collateral_usd == 0).
        if not cfg.paper_trading and (
            sig.direction == "short" or data.total_collateral_usd == 0
        ):
            swapped = _ensure_wallet_token(
                sig.direction, size.seed_usd, data, cfg, mcp, signer, cycle_entry
            )
            if swapped is False:
                # Insufficient funds — already appended cycle entry
                state.append_entry(cfg.trades_file, cycle_entry)
                return cycle_entry
            if swapped is True and sig.direction == "long":
                # No swap was needed — wallet already holds the asset.
                # Cap size.supply to actual balance: CoinGecko price vs swap execution price
                # can differ by tiny fractions, causing prepare_open's balance check to reject.
                wb = (
                    data.position_data.get("tokenBalances")
                    or data.position_data.get("wallet_balances")
                    or {}
                )
                actual_bal = float(wb.get(cfg.asset, 0) or 0)
                if actual_bal < size.supply:
                    from bot.sizing import PositionSize

                    size = PositionSize(
                        seed_usd=size.seed_usd, supply=actual_bal, borrow=size.borrow
                    )

        min_hf = cfg.short_min_open_hf if sig.direction == "short" else cfg.min_open_hf
        projected_hf = _projected_health_factor(sig.direction, data.price, size, cfg)
        cycle_entry["projected_health_factor"] = round(projected_hf, 4)
        if projected_hf < min_hf:
            log.info(
                "projected HF %.3f below min_open_hf %.3f — skip",
                projected_hf,
                min_hf,
            )
            cycle_entry["decision"] = "skip_min_hf"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

        new_pos_id = _position_id_for(sig.direction, cfg, raw_cfg)
        log.info(
            "Opening %s: signal=%s supply=%.4f borrow=%.4f",
            sig.direction,
            sig.label,
            size.supply,
            size.borrow,
        )
        res = executor.open_position(size, sig.direction, new_pos_id, cfg, mcp, signer)

        # Reconcile logged supply/borrow against on-chain actuals.
        # The MCP vault may adjust the seed (e.g. gas rounding, existing Aave balance)
        # so the computed size.supply/borrow can diverge from what was actually opened.
        # Retry up to 3 times with a delay — the tx may have just landed and the
        # RPC node may not reflect the new Aave state immediately.
        actual_supply = size.supply
        actual_borrow = size.borrow
        if not cfg.paper_trading:
            import time as _time

            for _attempt in range(3):
                try:
                    if _attempt > 0:
                        _time.sleep(4)
                    pos = mcp.get_position()
                    p = _find_chain_position(pos, new_pos_id, sig.direction, cfg)
                    if p is not None:
                        atoken_bal = float(p.get("aTokenBalance", size.supply))
                        # P&L formula uses supply as 1×seed (equity portion).
                        # For longs the aToken is leverage×seed — divide back to seed.
                        # For shorts the aToken IS the seed (USDC supply = seed×lev,
                        # but P&L uses borrow not supply, so supply stored as-is).
                        _lev = cfg.leverage_for(sig.direction)
                        actual_supply = (
                            atoken_bal / _lev if sig.direction == "long" else atoken_bal
                        )
                        actual_borrow = float(p.get("variableDebt", size.borrow))
                        if (
                            abs(actual_supply - size.supply) / max(size.supply, 1e-9)
                            > 0.01
                        ):
                            log.info(
                                "on-chain supply %.6f differs from computed %.6f — using on-chain",
                                actual_supply,
                                size.supply,
                            )
                        if (
                            abs(actual_borrow - size.borrow) / max(size.borrow, 1e-9)
                            > 0.01
                        ):
                            log.info(
                                "on-chain borrow %.6f differs from computed %.6f — using on-chain",
                                actual_borrow,
                                size.borrow,
                            )
                        break  # got a valid position — done
                    if _aave_positions(pos):
                        log.warning(
                            "post-open reconciliation found ambiguous positions; "
                            "keeping computed size"
                        )
                    # positions empty — tx may not be indexed yet, retry
                    log.debug(
                        "post-open reconciliation: empty positions on attempt %d",
                        _attempt + 1,
                    )
                except Exception as e:
                    log.warning(
                        "post-open on-chain reconciliation failed (attempt %d) — %s",
                        _attempt + 1,
                        e,
                    )
                    break

        trade_entry = {
            "type": "trade",
            "action": "open",
            "ts": state.now_iso(),
            "asset": cfg.asset,
            "direction": sig.direction,
            "position_id": new_pos_id,
            "signal": sig.label,
            "entry_price": data.price,
            "supply": actual_supply,
            "borrow": actual_borrow,
            "seed_usd": size.seed_usd,
            "leverage": cfg.leverage_for(sig.direction),
            "paper": cfg.paper_trading,
            "tx_hash": res.tx_hash,
        }
        cycle_entry["decision"] = f"open_{sig.direction}"
        state.append_entry(cfg.trades_file, cycle_entry)
        state.append_entry(cfg.trades_file, trade_entry)
        return cycle_entry

    # ── 8. Hold ───────────────────────────────────────────────────────────
    cycle_entry["decision"] = "hold"
    state.append_entry(cfg.trades_file, cycle_entry)
    return cycle_entry


def _close_trade_entry(
    open_trade: dict,
    close_price: float,
    cfg: BotConfig,
    reason: str,
    res,
    eff_supply: float = 0.0,
    eff_borrow: float = 0.0,
    eff_entry_price: float = 0.0,
) -> dict:
    # Use effective totals (including increases) for accurate P&L
    effective = dict(open_trade)
    if eff_supply > 0:
        effective["supply"] = eff_supply
    if eff_borrow > 0:
        effective["borrow"] = eff_borrow
    # Use borrow-weighted avg entry price when position was increased
    if eff_entry_price > 0:
        effective["entry_price"] = eff_entry_price
    realised = pnl.compute_realised(effective, close_price)
    entry = {
        "type": "trade",
        "action": "close",
        "ts": state.now_iso(),
        "asset": open_trade.get("asset"),
        "direction": open_trade.get("direction", "long"),
        "position_id": open_trade.get("position_id"),
        "close_price": close_price,
        "entry_price": effective.get("entry_price"),
        "supply": effective.get("supply"),
        "borrow": effective.get("borrow"),
        "leverage": open_trade.get("leverage"),
        "realised_usd": round(realised, 2),
        "reason": reason,
        "paper": cfg.paper_trading,
        "tx_hash": res.tx_hash,
    }
    if isinstance(res.raw, dict) and res.raw.get("post_close_swap_error"):
        entry["post_close_swap_error"] = res.raw["post_close_swap_error"]
    return entry


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Aave leverage strategy bot")
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Run continuously, sleeping SECONDS between cycles (0 = single run)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    cfg = BotConfig.load(args.config)
    raw_cfg = yaml.safe_load(open(args.config).read())
    _instance_lock = state.acquire_process_lock(cfg.trades_file)

    mode = "PAPER" if cfg.paper_trading else "LIVE"
    log.info(
        "Bot starting — asset=%s short_borrow=%s mode=%s",
        cfg.asset,
        cfg.short_borrow_asset,
        mode,
    )

    # Build signer and MCP client once outside the loop.
    # Signer: nonce state must persist so pending txs don't race.
    # MCPClient: session_token is updated in-place on renewal; recreating it
    # every cycle from cfg.mcp_session_token loses the renewed token and
    # triggers a fresh $4 payment each cycle.
    signer = _build_signer(cfg)
    mcp = MCPClient(
        base_url=cfg.mcp_url,
        session_token=cfg.mcp_session_token,
        wallet_address=cfg.user_address,
        private_key=cfg.private_key or os.environ.get("PRIVATE_KEY", ""),
        config_path=cfg._config_path,
        session_duration=cfg.mcp_session_duration,
    )

    if args.loop > 0:
        while True:
            try:
                result = run_cycle(cfg, raw_cfg, signer, mcp)
                log.info(
                    "Cycle done — decision=%s direction=%s price=%.2f",
                    result.get("decision"),
                    result.get("direction"),
                    result.get("price", 0),
                )
            except Exception as e:
                log.error("Cycle error: %s", e, exc_info=True)
                if signer:
                    signer.reset_nonce()  # force re-fetch after any error
            log.info("Sleeping %ds…", args.loop)
            time.sleep(args.loop)
    else:
        try:
            result = run_cycle(cfg, raw_cfg, signer, mcp)
            log.info(
                "Cycle done — decision=%s direction=%s price=%.2f",
                result.get("decision"),
                result.get("direction"),
                result.get("price", 0),
            )
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
