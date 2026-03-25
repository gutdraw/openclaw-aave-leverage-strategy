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
import bot.pnl as pnl
import bot.signal as signal
import bot.sizing as sizing
import bot.state as state
from bot.config import BotConfig
from bot.mcp_client import MCPClient

log = logging.getLogger(__name__)


# Aave v3 Base liquidation thresholds per supply asset (basis: on-chain reserve config)
_LIQ_THRESHOLD: dict[str, float] = {
    "WETH":   0.83,
    "wstETH": 0.82,
    "cbBTC":  0.78,
    "USDC":   0.78,   # used as supply asset in short positions
}


def _paper_health_factor(open_trade: Optional[dict], price: float, cfg: BotConfig) -> float:
    """
    Compute a simulated health factor for a paper position.
    Returns 999.0 when no position is open (no debt).

    sizing.py stores supply/borrow at seed level. The actual Aave flash-loan
    position supplies leverage × seed as collateral, so we must scale by leverage.

    Long (supply asset, borrow USDC):
      real_collateral_usd = leverage * supply * price
      debt_usd            = borrow * price   (borrow stored in asset units = supply*(lev-1))
      HF = (leverage * supply * lt) / borrow   (price cancels)

    Short (supply USDC, borrow asset):
      real_collateral_usd = leverage * supply   (USDC is stable)
      debt_usd            = borrow * price
      HF = (leverage * supply * lt) / (borrow * price)
    """
    if open_trade is None:
        return 999.0
    direction = open_trade.get("direction", "long")
    supply   = float(open_trade.get("supply", 0))
    borrow   = float(open_trade.get("borrow", 0))
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
        return (leverage * supply * lt) / borrow


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


# ── Single cycle ──────────────────────────────────────────────────────────────

def run_cycle(cfg: BotConfig, raw_cfg: dict) -> dict:
    """Run one full strategy cycle. Returns the cycle log entry."""
    mcp = MCPClient(
        base_url=cfg.mcp_url,
        session_token=cfg.mcp_session_token,
        wallet_address=cfg.user_address,
    )
    signer = _build_signer(cfg)

    # ── 1. State ──────────────────────────────────────────────────────────
    entries = state.load_entries(cfg.trades_file)
    open_trade: Optional[dict] = state.get_open_trade(entries)
    btc_dom_prev: Optional[float] = state.get_last_btc_dominance(entries)

    # ── 2. Market data ────────────────────────────────────────────────────
    data, sources_failed = market.fetch(cfg.asset, mcp, rpc_url=cfg.rpc_url)

    # In paper mode, replace on-chain HF with a simulated value derived from
    # the paper position — real wallet HF belongs to whatever is live on-chain
    # and should not influence paper trading decisions.
    if cfg.paper_trading:
        data.health_factor = _paper_health_factor(open_trade, data.price, cfg)

    # ── 3. Signal ─────────────────────────────────────────────────────────
    sig = signal.compute(data.change_1h, data.change_24h, data.change_7d)

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
        "usdc_utilization": round(data.usdc_utilization, 4) if data.usdc_utilization is not None else None,
        "asset_utilization": round(data.asset_utilization, 4) if data.asset_utilization is not None else None,
        "recent_liquidations": data.recent_liquidations,
        "sources_failed": sources_failed,
        "paper_trading": cfg.paper_trading,
    }

    # Derive the position_id for the current open trade (if any)
    open_direction = open_trade.get("direction", "long") if open_trade else sig.direction
    pos_id = _position_id_for(open_direction, cfg, raw_cfg)

    # ── 4. Health-factor defense ──────────────────────────────────────────
    if open_trade is not None:
        hf = data.health_factor

        # Use direction-aware thresholds: 2x short opens at HF ~1.17, so short
        # thresholds must be below that to avoid triggering immediately after open.
        is_short_pos = open_direction == "short"
        hf_close  = cfg.short_hf_defense_close  if is_short_pos else cfg.hf_defense_close
        hf_reduce = cfg.short_hf_defense_reduce if is_short_pos else cfg.hf_defense_reduce

        if hf < hf_close:
            log.warning("HF %.3f < %.3f — force close", hf, hf_close)
            res = executor.close_position(pos_id, open_direction, float(open_trade.get("supply", 0)), cfg, mcp, signer)
            trade_entry = _close_trade_entry(open_trade, data.price, cfg, "hf_close", res)
            state.append_entry(cfg.trades_file, cycle_entry | {"decision": "hf_close"})
            state.append_entry(cfg.trades_file, trade_entry)
            return cycle_entry

        if hf < hf_reduce:
            log.warning("HF %.3f < %.3f — reduce", hf, hf_reduce)
            target_lev = max(cfg.leverage / 2, 1.5)
            executor.reduce_position(pos_id, open_direction, target_lev, cfg, mcp, signer)
            cycle_entry["decision"] = "hf_reduce"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

    # ── 5a. Signal reversal exit ──────────────────────────────────────
    if open_trade is not None and cfg.signal_reversal_exit:
        is_long_pos  = open_direction == "long"
        is_short_pos = open_direction == "short"
        reversal = (
            (is_long_pos  and sig.score <= cfg.signal_reversal_min_score)
            or
            (is_short_pos and sig.score >= (3 - cfg.signal_reversal_min_score))
        )
        if reversal:
            hold_ok = True
            if cfg.min_hold_hours > 0:
                open_ts = open_trade.get("ts", "")
                try:
                    opened = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
                    if age_hours < cfg.min_hold_hours:
                        log.info(
                            "Signal reversal (score=%d) but hold %.1fh < min %.1fh — hold",
                            sig.score, age_hours, cfg.min_hold_hours,
                        )
                        hold_ok = False
                except (ValueError, TypeError):
                    pass
            if hold_ok:
                log.info(
                    "Signal reversal exit: %s position, signal=%s score=%d",
                    open_direction, sig.label, sig.score,
                )
                res = executor.close_position(pos_id, open_direction, float(open_trade.get("supply", 0)), cfg, mcp, signer)
                trade_entry = _close_trade_entry(open_trade, data.price, cfg, "signal_reversal", res)
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
                        age_days, cfg.max_hold_days,
                    )
                    res = executor.close_position(pos_id, open_direction, float(open_trade.get("supply", 0)), cfg, mcp, signer)
                    trade_entry = _close_trade_entry(open_trade, data.price, cfg, "max_hold_days", res)
                    cycle_entry["decision"] = "max_hold_days"
                    state.append_entry(cfg.trades_file, cycle_entry)
                    state.append_entry(cfg.trades_file, trade_entry)
                    return cycle_entry
            except (ValueError, TypeError):
                pass  # malformed ts — skip time exit this cycle

    # ── 5. Exit check (TP / SL) on open position ──────────────────────────
    if open_trade is not None:
        entry_price  = float(open_trade.get("entry_price", 0))
        supply_units = float(open_trade.get("supply", 0))
        borrow_units = float(open_trade.get("borrow", 0))
        trade_lev    = float(open_trade.get("leverage", cfg.leverage))
        p = pnl.compute_unrealised(
            entry_price=entry_price,
            current_price=data.price,
            supply=supply_units,
            borrow=borrow_units,
            leverage=trade_lev,
            take_profit_pct=cfg.take_profit_pct,
            stop_loss_pct=cfg.stop_loss_pct,
            direction=open_direction,
        )
        cycle_entry["unrealised_usd"] = round(p.unrealised_usd, 2)
        cycle_entry["unrealised_pct"] = round(p.unrealised_pct, 4)

        exit_reason = pnl.should_exit(p)
        # Suppress TP (but not SL) when signal is still at maximum strength in the
        # trade direction — trend-following: let winners ride until the signal fades.
        if (exit_reason == "take_profit"
                and not cfg.tp_on_strong_signal
                and ((open_direction == "long"  and sig.score == 3)
                     or (open_direction == "short" and sig.score == 0))):
            log.info(
                "TP reached (%.2f%%) but signal still strong (%s) — holding",
                p.unrealised_pct, sig.label,
            )
            exit_reason = None
        if exit_reason:
            log.info("Exit triggered: %s %.2f%%", exit_reason, p.unrealised_pct)
            res = executor.close_position(pos_id, open_direction, supply_units, cfg, mcp, signer)
            trade_entry = _close_trade_entry(open_trade, data.price, cfg, exit_reason, res)
            cycle_entry["decision"] = exit_reason
            state.append_entry(cfg.trades_file, cycle_entry)
            state.append_entry(cfg.trades_file, trade_entry)
            return cycle_entry

    # ── 6. No-trade filters ───────────────────────────────────────────────
    filt = filters.apply_all(data, sig.label, sig.direction, open_trade, btc_dom_prev, cfg)
    if filt.blocked:
        log.info("Filtered: %s", filt.decision)
        cycle_entry["decision"] = filt.decision
        state.append_entry(cfg.trades_file, cycle_entry)
        return cycle_entry

    # ── 7. Open new position ──────────────────────────────────────────────
    if open_trade is None and sig.multiplier > 0:
        size = sizing.compute(data.total_collateral_usd, data.price, sig, cfg)
        if size.supply <= 0:
            cycle_entry["decision"] = "skip_zero_size"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

        min_hf = cfg.short_min_open_hf if sig.direction == "short" else cfg.min_open_hf
        if data.health_factor < min_hf and data.health_factor != 999.0:
            log.info("HF %.3f below min_open_hf %.3f — skip", data.health_factor, min_hf)
            cycle_entry["decision"] = "skip_min_hf"
            state.append_entry(cfg.trades_file, cycle_entry)
            return cycle_entry

        new_pos_id = _position_id_for(sig.direction, cfg, raw_cfg)
        log.info(
            "Opening %s: signal=%s supply=%.4f borrow=%.4f",
            sig.direction, sig.label, size.supply, size.borrow,
        )
        res = executor.open_position(size, sig.direction, new_pos_id, cfg, mcp, signer)
        trade_entry = {
            "type": "trade",
            "action": "open",
            "ts": state.now_iso(),
            "asset": cfg.asset,
            "direction": sig.direction,
            "position_id": new_pos_id,
            "signal": sig.label,
            "entry_price": data.price,
            "supply": size.supply,
            "borrow": size.borrow,
            "seed_usd": size.seed_usd,
            "leverage": min(cfg.leverage, cfg.short_max_leverage) if sig.direction == "short" else cfg.leverage,
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
) -> dict:
    realised = pnl.compute_realised(open_trade, close_price)
    return {
        "type": "trade",
        "action": "close",
        "ts": state.now_iso(),
        "asset": open_trade.get("asset"),
        "direction": open_trade.get("direction", "long"),
        "position_id": open_trade.get("position_id"),
        "close_price": close_price,
        "entry_price": open_trade.get("entry_price"),
        "supply": open_trade.get("supply"),
        "borrow": open_trade.get("borrow"),
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
    log.info("Bot starting — asset=%s short_borrow=%s mode=%s", cfg.asset, cfg.short_borrow_asset, mode)

    if args.loop > 0:
        while True:
            try:
                result = run_cycle(cfg, raw_cfg)
                log.info(
                    "Cycle done — decision=%s direction=%s price=%.2f",
                    result.get("decision"), result.get("direction"), result.get("price", 0),
                )
            except Exception as e:
                log.error("Cycle error: %s", e, exc_info=True)
            log.info("Sleeping %ds…", args.loop)
            time.sleep(args.loop)
    else:
        try:
            result = run_cycle(cfg, raw_cfg)
            log.info(
                "Cycle done — decision=%s direction=%s price=%.2f",
                result.get("decision"), result.get("direction"), result.get("price", 0),
            )
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
