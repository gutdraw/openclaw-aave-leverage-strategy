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

log = logging.getLogger(__name__)

# Uniswap V3 SwapRouter02 on Base — used for client-side approve injection
_SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
_UINT256_MAX = str(2**256 - 1)


def _inject_swap_approve(resp: dict) -> dict:
    """Ensure an MCP swap response includes an ERC20 approve step.

    If the server already returns an approve step targeting the swap router,
    this is a no-op.  Otherwise it prepends one so *execute_steps* can check
    the on-chain allowance and send the approval when needed.
    """
    steps = resp.get("transaction_steps", [])
    if not steps:
        return resp

    swap_step = next((s for s in steps if s.get("type") == "swap"), None)
    if swap_step is None:
        return resp
    # Native ETH swaps don't need approval
    if swap_step.get("use_eth"):
        return resp

    has_approve = False
    for s in steps:
        if (
            s.get("type") == "approve"
            and s.get("args", [None])[0]
            and s["args"][0].lower() == _SWAP_ROUTER.lower()
        ):
            has_approve = True
            # Ensure the gas limit is sufficient for proxy tokens like cbBTC
            if s.get("gas", 0) < 100_000:
                s["gas"] = 100_000
    if has_approve:
        return resp

    # Derive tokenIn address from the swap step args (first element of the tuple)
    swap_args = swap_step.get("args", [[]])
    token_in = (
        swap_args[0][0]
        if swap_args and isinstance(swap_args[0], list) and swap_args[0]
        else None
    )
    if not token_in:
        return resp

    approve_step = {
        "step": 0,
        "title": "Approve token for Swap",
        "type": "approve",
        "contract": token_in,
        "abi_fn": "approve(address,uint256)",
        "args": [_SWAP_ROUTER, _UINT256_MAX],
        "gas": 100_000,
    }
    resp["transaction_steps"] = [approve_step] + steps
    return resp


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
      HF = (leverage * supply * lt) / borrow   (price cancels)

    Short (supply USDC seed, borrow lev×seed asset):
      True Aave supply = (leverage+1)×seed USDC (flash loan loop).
      HF = ((leverage+1) * supply * lt) / (borrow * price)
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
        return ((leverage + 1) * supply * lt) / debt_usd
    else:
        lt = _LIQ_THRESHOLD.get(cfg.asset, 0.80)
        if borrow <= 0:
            return 999.0
        # HF = (supply_asset * price * lt) / borrow_usdc
        return (supply * price * lt) / borrow


def _position_id_for(direction: str, cfg: BotConfig, raw_cfg: dict) -> str:
    """Return the Aave position_id string for the given direction."""
    if direction == "short":
        return raw_cfg.get("short_position_id", f"USDC/{cfg.short_borrow_asset}")
    return raw_cfg.get("position_id", f"{cfg.asset}/USDC")


def _build_signer(cfg: BotConfig):
    if cfg.paper_trading:
        return None
    pk = cfg.private_key or os.environ.get("PRIVATE_KEY", "")
    if not pk:
        raise RuntimeError("PRIVATE_KEY is required in live mode")
    from bot.signer import Signer

    rpc = os.environ.get("RPC_URL", "https://mainnet.base.org")
    return Signer(rpc_url=rpc, private_key=pk)


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
      swap just enough asset → USDC to cover seed_usd.
    - Long: needs the supply asset. If wallet only has USDC, swap USDC → asset.

    Returns:
        True  — correct token already present (no swap needed)
        None  — swap executed successfully
        False — insufficient wallet funds; caller should skip and log cycle_entry

    Adds 1.5% to swap amount to cover DEX slippage.
    """
    wb = (
        data.position_data.get("tokenBalances")
        or data.position_data.get("wallet_balances")
        or {}
    )
    _SLIPPAGE = (
        1.002  # 0.2% buffer — covers 0.05% Uniswap v3 fee + ~0.1% execution price drift
    )

    if direction == "short":
        # Short flash-loan loop needs USDC as collateral seed.
        # Prefer spending the asset (cbBTC/WETH) first so USDC stays as the
        # standing reserve. Only use USDC directly if asset balance is insufficient.
        usdc_bal = float(wb.get("USDC", 0) or 0)

        for tok in (cfg.short_borrow_asset, cfg.asset):
            tok_bal = float(wb.get(tok, 0) or 0)
            tok_val_usd = tok_bal * data.price
            if tok_val_usd >= seed_usd * 0.95:
                # Swap ALL the asset to USDC — a short means we want zero
                # long exposure in the wallet, not just enough for the seed.
                swap_qty = tok_bal
                log.info(
                    "Swapping %.6f %s → USDC (all, seed=%.2f, eliminating long exposure)",
                    swap_qty,
                    tok,
                    seed_usd,
                )
                swap_hash = signer.execute_steps(
                    _inject_swap_approve(mcp.swap(tok, "USDC", swap_qty))
                )
                cycle_entry["pre_swap"] = (
                    f"{swap_qty:.6f} {tok} → USDC (tx={swap_hash})"
                )
                log.info("waiting for swap confirmation: %s", swap_hash)
                signer.wait_for_receipt(swap_hash)
                time.sleep(
                    3
                )  # brief pause for RPC propagation before prepare_open reads balance
                log.info("swap confirmed — proceeding to open")
                return None

        # Asset alone not enough — fall back to USDC if it covers the seed
        if usdc_bal >= seed_usd * 0.95:
            return True  # use USDC directly, no swap needed

        log.warning(
            "Insufficient wallet funds for short: need %.2f USDC seed, "
            "wallet USDC=%.2f %s=%.6f — skip",
            seed_usd,
            usdc_bal,
            cfg.short_borrow_asset,
            float(wb.get(cfg.short_borrow_asset, 0) or 0),
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
                _inject_swap_approve(mcp.swap("USDC", cfg.asset, swap_usd))
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


def run_cycle(cfg: BotConfig, raw_cfg: dict, signer=None) -> dict:
    """Run one full strategy cycle. Returns the cycle log entry."""
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
        "sources_failed": sources_failed,
        "paper_trading": cfg.paper_trading,
        "cg_signal": cg_sig.label,
        "tech_signal": tech_sig.label if tech_sig is not None else None,
        "tech_ema_bull": tech.ema_bull if tech is not None else None,
        "tech_rsi": tech.rsi if tech is not None else None,
        "tech_source": tech.source if tech is not None else None,
    }

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
                float(open_trade.get("supply", 0)),
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
            target_lev = max(cfg.leverage / 2, 1.5)
            executor.reduce_position(
                pos_id, open_direction, target_lev, cfg, mcp, signer
            )
            cycle_entry["decision"] = "hf_reduce"
            state.append_entry(cfg.trades_file, cycle_entry)
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
        prev_usdc_util, prev_asset_util = state.get_last_utilizations(entries)

        if open_direction == "long":
            flash_util = data.usdc_utilization
            prev_flash_util = prev_usdc_util
            flash_frozen = data.borrow_asset_frozen  # USDC frozen → no flash loan
            flash_paused = data.borrow_asset_paused  # USDC paused → nothing works
            supply_paused = data.asset_paused  # supply asset paused → can't withdraw
        else:
            flash_util = data.asset_utilization
            prev_flash_util = prev_asset_util
            flash_frozen = data.asset_frozen  # cbBTC frozen → no flash loan
            flash_paused = data.asset_paused  # cbBTC paused → nothing works
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
                float(open_trade.get("supply", 0)),
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

    # ── 5a. Signal reversal exit ──────────────────────────────────────
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
    if open_trade is not None and cfg.max_hold_days > 0:
        open_ts = open_trade.get("ts", "")
        if open_ts:
            try:
                opened = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - opened).total_seconds() / 86400
                if age_days >= cfg.max_hold_days:
                    log.info(
                        "Time-based exit: position age %.1fd >= max_hold_days %.1fd",
                        age_days,
                        cfg.max_hold_days,
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
        signal_upgraded = (
            open_direction == "long" and sig.score == 3 and open_signal != "strong_long"
        ) or (
            open_direction == "short"
            and sig.score == 0
            and open_signal != "strong_short"
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
        if data.health_factor < min_hf and data.health_factor != 999.0:
            log.info(
                "HF %.3f below min_open_hf %.3f — skip", data.health_factor, min_hf
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
        actual_supply = size.supply
        actual_borrow = size.borrow
        if not cfg.paper_trading:
            try:
                pos = mcp.get_position()
                aave_pos = (pos.get("aavePositions") or {}).get("positions") or []
                if aave_pos:
                    p = aave_pos[0]
                    if sig.direction == "short":
                        actual_supply = float(p.get("aTokenBalance", size.supply))
                        actual_borrow = float(p.get("variableDebt", size.borrow))
                    else:
                        actual_supply = float(p.get("aTokenBalance", size.supply))
                        actual_borrow = float(p.get("variableDebt", size.borrow))
                    if abs(actual_supply - size.supply) / max(size.supply, 1e-9) > 0.01:
                        log.info(
                            "on-chain supply %.6f differs from computed %.6f — using on-chain",
                            actual_supply,
                            size.supply,
                        )
                    if abs(actual_borrow - size.borrow) / max(size.borrow, 1e-9) > 0.01:
                        log.info(
                            "on-chain borrow %.6f differs from computed %.6f — using on-chain",
                            actual_borrow,
                            size.borrow,
                        )
            except Exception as e:
                log.warning(
                    "post-open on-chain reconciliation failed — using computed values: %s",
                    e,
                )

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
    return {
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

    mode = "PAPER" if cfg.paper_trading else "LIVE"
    log.info(
        "Bot starting — asset=%s short_borrow=%s mode=%s",
        cfg.asset,
        cfg.short_borrow_asset,
        mode,
    )

    # Build signer once outside the loop so nonce state persists across cycles.
    # Each new Signer re-fetches nonce from chain, creating a race if the previous
    # cycle's tx is still pending. One Signer per process avoids this entirely.
    signer = _build_signer(cfg)

    if args.loop > 0:
        while True:
            try:
                result = run_cycle(cfg, raw_cfg, signer)
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
            result = run_cycle(cfg, raw_cfg, signer)
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
