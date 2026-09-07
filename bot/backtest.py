"""
Parameter replay backtester.

Uses the price series already recorded in trades.jsonl cycle entries
to simulate how different TP/SL/filter/sizing parameters would have performed.

No external data needed — replays the actual price history the bot observed.
Returns a comparison of simulated vs actual results.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional

import bot.filters as filters_mod
from bot.config import BotConfig
from bot.market import MarketData
import bot.signal as signal_mod
import bot.state as state


@dataclass
class BacktestParams:
    """Parameters to test. None = use value from the cycle entries as-is."""

    take_profit_pct: Optional[float] = None  # e.g. 7.0
    stop_loss_pct: Optional[float] = None  # e.g. 4.0
    leverage: Optional[float] = None  # e.g. 2.5
    base_position_pct: Optional[float] = None  # e.g. 0.15
    max_volatility_1h: Optional[float] = None  # e.g. 3.0
    max_borrow_apr: Optional[float] = None  # e.g. 6.0
    btc_dominance_rise_threshold: Optional[float] = None  # e.g. 1.5
    round_trip_fee_bps: float = 0.0
    gas_usd_per_trade: float = 0.0
    include_borrow_cost: bool = False
    require_complete_snapshots: bool = False


@dataclass
class SimTrade:
    direction: str
    signal: str
    entry_price: float
    entry_ts: str
    exit_price: float
    exit_ts: str
    exit_reason: str
    seed_usd: float
    leverage: float
    realised_usd: float
    realised_pct: float
    cost_usd: float = 0.0


@dataclass
class BacktestResult:
    params: dict  # what was tested
    total_cycles: int
    simulated_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_usd: float
    avg_pnl_usd: float
    best_trade_usd: float
    worst_trade_usd: float
    max_drawdown_usd: float
    trades: list[SimTrade] = field(default_factory=list)
    vs_actual: Optional[dict] = None  # comparison to actual if actual data present
    gross_pnl_usd: float = 0.0
    total_cost_usd: float = 0.0
    incomplete_snapshot_cycles: int = 0
    faithful_replay: bool = False


def run(
    params: BacktestParams,
    trades_file: str = "trades.jsonl",
    seed_usd: Optional[float] = None,
    faithful: bool = False,
) -> BacktestResult:
    """
    Replay the price history in trades.jsonl with the given parameters.

    For each cycle:
    1. Re-evaluate the signal from recorded price changes
    2. Apply filters (volatility, borrow APR, BTC dominance)
    3. Simulate open/close with new TP/SL thresholds
    4. Track P&L

    seed_usd_override: fixed seed per trade (ignores collateral-based sizing).
    Useful when you want apples-to-apples comparison without collateral data.
    Default: use 1000 USD per trade as a neutral baseline.

    When ``faithful`` is true, use the recorded live signal and the shared live
    filter pipeline where the cycle snapshot contains the required fields. Cycles
    without those fields are counted as incomplete and only fall back to the old
    price-change signal when ``require_complete_snapshots`` is false.
    """
    entries = state.load_entries(trades_file)
    cycles = [e for e in entries if e.get("type") == "cycle"]

    if not cycles:
        return BacktestResult(
            params=_params_dict(params),
            total_cycles=0,
            simulated_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            total_pnl_usd=0.0,
            avg_pnl_usd=0.0,
            best_trade_usd=0.0,
            worst_trade_usd=0.0,
            max_drawdown_usd=0.0,
            faithful_replay=faithful,
        )

    if faithful:
        return _run_faithful(cycles, params, seed_usd or 1000.0)

    seed = seed_usd or 1000.0
    tp = params.take_profit_pct
    sl = params.stop_loss_pct
    lev = params.leverage or 3.0
    vol = params.max_volatility_1h or 5.0
    apr = params.max_borrow_apr or 8.0
    dom_thresh = params.btc_dominance_rise_threshold or 2.0

    replay_cfg = BotConfig(
        take_profit_pct=tp or 5.0,
        stop_loss_pct=sl or 3.0,
        max_volatility_1h=vol,
        max_borrow_apr=apr,
        btc_dominance_rise_threshold=dom_thresh,
    )

    sim_trades: list[SimTrade] = []
    open_trade: Optional[dict] = (
        None  # keys: direction, entry_price, entry_ts, signal, seed_usd
    )
    prev_dom: Optional[float] = None
    incomplete_snapshots = 0

    for cycle in cycles:
        price = float(cycle.get("price", 0))
        change_1h = float(cycle.get("change_1h", 0))
        change_24h = float(cycle.get("change_24h", 0))
        change_7d = float(cycle.get("change_7d", 0))
        borrow_apr = float(cycle.get("borrow_apr", 0))
        btc_dom = float(cycle.get("btc_dominance_pct", 0))
        ts = cycle.get("ts", "")

        if price <= 0:
            prev_dom = btc_dom
            continue

        # ── Check exit on open trade ──────────────────────────────────────
        if open_trade is not None:
            entry = open_trade["entry_price"]
            dirn = open_trade["direction"]
            _tp = (
                tp if tp is not None else float(cycle.get("take_profit_pct_used", 5.0))
            )
            _sl = sl if sl is not None else float(cycle.get("stop_loss_pct_used", 3.0))

            if dirn == "long":
                pct = (price - entry) / entry * 100
            else:
                pct = (entry - price) / entry * 100

            exit_reason = None
            if pct <= -_sl:
                exit_reason = "stop_loss"
            elif pct >= _tp:
                exit_reason = "take_profit"

            if exit_reason:
                gross_realised = _compute_pnl(open_trade, price)
                cost = _estimate_cost(open_trade, ts, params)
                realised = gross_realised - cost
                sim_trades.append(
                    SimTrade(
                        direction=dirn,
                        signal=open_trade["signal"],
                        entry_price=entry,
                        entry_ts=open_trade["entry_ts"],
                        exit_price=price,
                        exit_ts=ts,
                        exit_reason=exit_reason,
                        seed_usd=open_trade["seed_usd"],
                        leverage=lev,
                        realised_usd=round(realised, 2),
                        realised_pct=round(pct, 4),
                        cost_usd=round(cost, 2),
                    )
                )
                open_trade = None

        if open_trade is not None:
            prev_dom = btc_dom
            continue  # already in a trade

        # ── Signal ────────────────────────────────────────────────────────
        sig = _recorded_signal(cycle) if faithful else None
        if sig is None:
            incomplete_snapshots += 1 if faithful else 0
            if faithful and params.require_complete_snapshots:
                prev_dom = btc_dom
                continue
            sig = signal_mod.compute(change_1h, change_24h, change_7d)
        if sig.multiplier == 0:
            prev_dom = btc_dom
            continue

        # ── Filters ───────────────────────────────────────────────────────
        if faithful:
            filter_result = filters_mod.apply_all(
                _market_data(cycle),
                sig.label,
                sig.direction,
                open_trade,
                prev_dom,
                replay_cfg,
                ohlcv_rsi=_number(cycle.get("tech_rsi")),
            )
            if filter_result.blocked:
                prev_dom = btc_dom
                continue
        else:
            if abs(change_1h) > vol:
                prev_dom = btc_dom
                continue
            if borrow_apr > apr:
                prev_dom = btc_dom
                continue
            if prev_dom is not None:
                dom_change = btc_dom - prev_dom
                if sig.direction == "long" and dom_change > dom_thresh:
                    prev_dom = btc_dom
                    continue
                if sig.direction == "short" and dom_change < -dom_thresh:
                    prev_dom = btc_dom
                    continue

        # ── Open ──────────────────────────────────────────────────────────
        trade_seed = seed * sig.multiplier
        open_trade = {
            "direction": sig.direction,
            "entry_price": price,
            "entry_ts": ts,
            "signal": sig.label,
            "seed_usd": trade_seed,
            "leverage": lev,
            "borrow_apr": borrow_apr,
        }
        prev_dom = btc_dom

    # ── Compute stats ─────────────────────────────────────────────────────
    pnls = [t.realised_usd for t in sim_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(pnls) if pnls else 0.0
    best = max(pnls) if pnls else 0.0
    worst = min(pnls) if pnls else 0.0
    max_dd = _max_drawdown(pnls)
    win_rate = len(wins) / len(pnls) if pnls else 0.0

    return BacktestResult(
        params=_params_dict(params),
        total_cycles=len(cycles),
        simulated_trades=len(sim_trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(win_rate, 4),
        total_pnl_usd=round(total_pnl, 2),
        avg_pnl_usd=round(avg_pnl, 2),
        best_trade_usd=round(best, 2),
        worst_trade_usd=round(worst, 2),
        max_drawdown_usd=round(max_dd, 2),
        trades=sim_trades,
        gross_pnl_usd=round(sum(t.realised_usd + t.cost_usd for t in sim_trades), 2),
        total_cost_usd=round(sum(t.cost_usd for t in sim_trades), 2),
        incomplete_snapshot_cycles=incomplete_snapshots,
        faithful_replay=faithful,
    )


def _run_faithful(
    cycles: list[dict], params: BacktestParams, seed: float
) -> BacktestResult:
    """Replay the decision order used by ``main.run_cycle``.

    The original backtester is deliberately kept as a small price-series study.
    This path is for operational reports: it uses the signal selected by the
    live cycle, the recorded filter inputs, and the same safety/exit ordering.
    It never invents a quote or an on-chain state when a required cycle field is
    absent; incomplete snapshots are reported and, by default, skipped.
    """
    defaults = BotConfig(
        take_profit_pct=params.take_profit_pct or 5.0,
        stop_loss_pct=params.stop_loss_pct or 3.0,
        max_volatility_1h=params.max_volatility_1h or 5.0,
        max_borrow_apr=params.max_borrow_apr or 8.0,
        btc_dominance_rise_threshold=params.btc_dominance_rise_threshold or 2.0,
        leverage=params.leverage or 3.0,
    )
    sim_trades: list[SimTrade] = []
    open_trade: Optional[dict] = None
    last_close: Optional[dict] = None
    prev_dom: Optional[float] = None
    prev_cycle: Optional[dict] = None
    incomplete_snapshots = 0

    def close_trade(reason: str, cycle: dict, price: float) -> None:
        nonlocal open_trade, last_close
        if open_trade is None:
            return
        ts = str(cycle.get("ts", ""))
        gross = _compute_pnl(open_trade, price)
        cost = _estimate_cost(open_trade, ts, params)
        realised = gross - cost
        entry = float(open_trade.get("entry_price", 0) or 0)
        direction = str(open_trade.get("direction", "long"))
        pct = (
            (
                (price - entry) / entry * 100
                if direction == "long"
                else (entry - price) / entry * 100
            )
            if entry > 0
            else 0.0
        )
        sim_trades.append(
            SimTrade(
                direction=direction,
                signal=str(open_trade.get("signal", "")),
                entry_price=entry,
                entry_ts=str(open_trade.get("entry_ts", "")),
                exit_price=price,
                exit_ts=ts,
                exit_reason=reason,
                seed_usd=float(open_trade.get("seed_usd", 0) or 0),
                leverage=float(open_trade.get("leverage", 1) or 1),
                realised_usd=round(realised, 2),
                realised_pct=round(pct, 4),
                cost_usd=round(cost, 2),
            )
        )
        last_close = {"ts": ts, "direction": direction, "reason": reason}
        open_trade = None

    for cycle in cycles:
        missing = _missing_snapshot_fields(cycle)
        if missing:
            incomplete_snapshots += 1
            if params.require_complete_snapshots:
                prev_dom = _number(cycle.get("btc_dominance_pct"))
                prev_cycle = cycle
                continue

        price = _number(cycle.get("price")) or 0.0
        if price <= 0:
            prev_dom = _number(cycle.get("btc_dominance_pct"))
            prev_cycle = cycle
            continue

        sig = _recorded_signal(cycle)
        if sig is None:
            if params.require_complete_snapshots:
                incomplete_snapshots += 1 if not missing else 0
                prev_dom = _number(cycle.get("btc_dominance_pct"))
                prev_cycle = cycle
                continue
            sig = signal_mod.compute(
                float(cycle.get("change_1h", 0) or 0),
                float(cycle.get("change_24h", 0) or 0),
                float(cycle.get("change_7d", 0) or 0),
            )

        cfg = _faithful_config(cycle, params, defaults)
        data = _market_data(cycle)
        ts = str(cycle.get("ts", ""))
        btc_dom = _number(cycle.get("btc_dominance_pct")) or 0.0
        paper = bool(cycle.get("paper_trading", True))

        # A live cycle refuses to act when its safety snapshot is incomplete.
        # Paper cycles can still be replayed with the recorded market fields.
        if (
            open_trade is not None
            and not paper
            and (
                cycle.get("position_data_available") is not True
                or cycle.get("onchain_data_available") is not True
            )
        ):
            prev_dom = btc_dom
            prev_cycle = cycle
            continue

        if open_trade is not None:
            direction = str(open_trade.get("direction", "long"))
            escape = _faithful_liquidity_escape(cycle, prev_cycle, direction, cfg)
            if not paper and escape:
                close_trade(escape, cycle, price)
                prev_dom = btc_dom
                prev_cycle = cycle
                continue

            hf = _number(cycle.get("health_factor"))
            if not paper and hf is not None:
                hf_close = (
                    cfg.short_hf_defense_close
                    if direction == "short"
                    else cfg.hf_defense_close
                )
                hf_reduce = (
                    cfg.short_hf_defense_reduce
                    if direction == "short"
                    else cfg.hf_defense_reduce
                )
                if hf < hf_close:
                    close_trade("hf_close", cycle, price)
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue
                if hf < hf_reduce:
                    # Reduce exposure proportionally. The live log records the
                    # post-reduction position on the next snapshot; retaining
                    # the original leverage keeps the P&L formula consistent
                    # with bot.pnl for the remaining seed exposure.
                    current_lev = float(open_trade.get("leverage", cfg.leverage))
                    target_lev = max(cfg.leverage_for(direction) / 2, 1.5)
                    if current_lev > 0 and target_lev < current_lev:
                        factor = target_lev / current_lev
                        open_trade["supply"] = float(open_trade["supply"]) * factor
                        open_trade["borrow"] = float(open_trade["borrow"]) * factor
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue

            entry = float(open_trade.get("entry_price", 0) or 0)
            if entry <= 0:
                open_trade = None
            else:
                pct = (
                    (price - entry) / entry * 100
                    if direction == "long"
                    else (entry - price) / entry * 100
                )
                tp = cfg.tp_for(direction)
                sl = cfg.sl_for(direction)
                exit_reason: Optional[str] = None
                if pct <= -sl:
                    exit_reason = "stop_loss"
                elif pct >= tp:
                    exit_reason = "take_profit"
                if (
                    exit_reason == "take_profit"
                    and not cfg.tp_on_strong_signal
                    and (
                        (direction == "long" and sig.score == 3)
                        or (direction == "short" and sig.score == 0)
                    )
                ):
                    exit_reason = None
                if exit_reason:
                    close_trade(exit_reason, cycle, price)
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue

                # The live trailing-stop reference is the peak/trough observed
                # before the current cycle. Update it only after this decision.
                trail_pct = _trailing_pct(cfg, direction)
                peak = _number(open_trade.get("peak_price")) or entry
                drawdown = (
                    (peak - price) / peak * 100
                    if direction == "long"
                    else (price - peak) / peak * 100
                )
                hold_hours = _elapsed_hours(open_trade.get("entry_ts"), ts)
                if (
                    trail_pct > 0
                    and drawdown >= trail_pct
                    and (hold_hours is None or hold_hours >= cfg.min_hold_hours)
                ):
                    close_trade("trailing_stop", cycle, price)
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue

                reversal = (
                    direction == "long"
                    and sig.direction == "short"
                    and sig.score <= cfg.signal_reversal_min_score
                ) or (
                    direction == "short"
                    and sig.direction == "long"
                    and sig.score >= 3 - cfg.signal_reversal_min_score
                )
                if (
                    cfg.signal_reversal_exit
                    and reversal
                    and (hold_hours is None or hold_hours >= cfg.min_hold_hours)
                ):
                    close_trade("signal_reversal", cycle, price)
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue

                max_hold = (
                    cfg.long_max_hold_days
                    if direction == "long" and cfg.long_max_hold_days > 0
                    else cfg.max_hold_days
                )
                age_days = hold_hours / 24 if hold_hours is not None else None
                still_strong = (direction == "long" and sig.score >= 3) or (
                    direction == "short" and sig.score == 0
                )
                if (
                    max_hold > 0
                    and age_days is not None
                    and age_days >= max_hold
                    and not still_strong
                ):
                    close_trade("max_hold_days", cycle, price)
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue

                if (
                    direction == "long"
                    and sig.direction == "long"
                    and sig.score == 3
                    and open_trade.get("signal") != "strong_long"
                    and not open_trade.get("increased", False)
                ):
                    add_seed = max(
                        seed
                        * (
                            cfg.strong_signal_size
                            - float(open_trade.get("signal_multiplier", 0.5))
                        ),
                        0.0,
                    )
                    if add_seed > 0:
                        lev = float(open_trade.get("leverage", cfg.leverage))
                        open_trade["seed_usd"] = (
                            float(open_trade["seed_usd"]) + add_seed
                        )
                        open_trade["supply"] += add_seed / price
                        open_trade["borrow"] += add_seed * max(lev - 1, 0)
                        open_trade["signal"] = sig.label
                        open_trade["increased"] = True
                    prev_dom = btc_dom
                    prev_cycle = cycle
                    continue

                open_trade["peak_price"] = (
                    max(peak, price) if direction == "long" else min(peak, price)
                )
                prev_dom = btc_dom
                prev_cycle = cycle
                continue

        if sig.multiplier <= 0:
            prev_dom = btc_dom
            prev_cycle = cycle
            continue

        filt = filters_mod.apply_all(
            data,
            sig.label,
            sig.direction,
            None,
            prev_dom,
            cfg,
            ohlcv_rsi=_number(cycle.get("tech_rsi")),
        )
        if filt.blocked or _faithful_entry_gate(cycle, sig, last_close, cfg, ts):
            prev_dom = btc_dom
            prev_cycle = cycle
            continue
        if not paper and not data.risk_data_available:
            prev_dom = btc_dom
            prev_cycle = cycle
            continue

        multiplier = (
            cfg.strong_signal_size if sig.direction == "short" else sig.multiplier
        )
        trade_seed = seed * multiplier
        lev = cfg.leverage_for(sig.direction)
        if trade_seed <= 0 or lev <= 0:
            prev_dom = btc_dom
            prev_cycle = cycle
            continue
        if sig.direction == "short":
            supply = trade_seed
            borrow = trade_seed * max(lev - 1, 0) / price
        else:
            supply = trade_seed / price
            borrow = trade_seed * max(lev - 1, 0)
        open_trade = {
            "direction": sig.direction,
            "entry_price": price,
            "entry_ts": ts,
            "signal": sig.label,
            "signal_multiplier": sig.multiplier,
            "seed_usd": trade_seed,
            "leverage": lev,
            "borrow_apr": _number(cycle.get("borrow_apr")) or 0.0,
            "supply": supply,
            "borrow": borrow,
            "peak_price": price,
            "increased": False,
        }
        prev_dom = btc_dom
        prev_cycle = cycle

    return _result_from_trades(
        cycles, params, sim_trades, incomplete_snapshots, faithful=True
    )


def run_live(
    trades_file: str = "trades.jsonl",
    params: Optional[BacktestParams] = None,
    seed_usd: Optional[float] = None,
    require_complete_snapshots: bool = True,
) -> BacktestResult:
    """Replay the live-selected signal and shared filter pipeline.

    This is intentionally separate from ``run`` so existing parameter studies
    retain their legacy behavior while new reports can opt into a data-quality
    gate. A complete faithful replay requires cycle records containing the
    selected signal plus the live filter inputs.
    """
    replay_params = replace(
        params or BacktestParams(),
        require_complete_snapshots=require_complete_snapshots,
    )
    return run(replay_params, trades_file, seed_usd, faithful=True)


def compare(
    params_a: BacktestParams,
    params_b: BacktestParams,
    trades_file: str = "trades.jsonl",
    seed_usd: float = 1000.0,
) -> dict:
    """
    Run two backtests and return a side-by-side comparison.
    Useful for Hermes to evaluate a proposed change against the current config.
    """
    result_a = run(params_a, trades_file, seed_usd)
    result_b = run(params_b, trades_file, seed_usd)

    def _row(r: BacktestResult) -> dict:
        return {
            "params": r.params,
            "trades": r.simulated_trades,
            "win_rate": r.win_rate,
            "total_pnl_usd": r.total_pnl_usd,
            "avg_pnl_usd": r.avg_pnl_usd,
            "max_drawdown_usd": r.max_drawdown_usd,
        }

    return {
        "baseline": _row(result_a),
        "proposed": _row(result_b),
        "delta": {
            "win_rate": round(result_b.win_rate - result_a.win_rate, 4),
            "total_pnl_usd": round(result_b.total_pnl_usd - result_a.total_pnl_usd, 2),
            "avg_pnl_usd": round(result_b.avg_pnl_usd - result_a.avg_pnl_usd, 2),
            "max_drawdown_usd": round(
                result_b.max_drawdown_usd - result_a.max_drawdown_usd, 2
            ),
            "verdict": "improvement"
            if result_b.total_pnl_usd > result_a.total_pnl_usd
            else "regression",
        },
    }


def _compute_pnl(open_trade: dict, close_price: float) -> float:
    entry = open_trade["entry_price"]
    seed = open_trade["seed_usd"]
    lev = open_trade["leverage"]
    dirn = open_trade["direction"]
    if dirn == "short":
        borrow_units = seed * (lev - 1) / entry
        return borrow_units * (entry - close_price)
    supply_units = seed / entry
    return supply_units * (close_price - entry) * lev


_FAITHFUL_CORE_FIELDS = (
    "price",
    "change_1h",
    "change_24h",
    "change_7d",
    "borrow_apr",
    "btc_dominance_pct",
    "health_factor",
    "signal",
)
_FAITHFUL_OBSERVED_FIELDS = (
    # These may legitimately be null when an upstream source was unavailable;
    # the key itself must still be present so replay can distinguish null from
    # an old log that never recorded the input.
    "short_borrow_apr",
    "funding_rate",
    "fear_greed",
    "volume_24h",
    "usdc_utilization",
    "asset_utilization",
    "short_asset_utilization",
    "recent_liquidations",
    "position_data_available",
    "onchain_data_available",
    "risk_data_available",
    "risk_fetched_at",
    "paper_trading",
    "asset_frozen",
    "asset_paused",
    "borrow_asset_frozen",
    "borrow_asset_paused",
    "short_asset_frozen",
    "short_asset_paused",
    "tech_source",
    "tech_ema_bull",
    "tech_rsi",
)


def _missing_snapshot_fields(cycle: dict) -> list[str]:
    missing = [
        key for key in _FAITHFUL_CORE_FIELDS if key not in cycle or cycle[key] is None
    ]
    missing.extend(key for key in _FAITHFUL_OBSERVED_FIELDS if key not in cycle)
    if cycle.get("signal") not in {
        "strong_long",
        "moderate_long",
        "moderate_short",
        "strong_short",
        "hold",
    }:
        if "signal" not in missing:
            missing.append("signal")
    return missing


def _faithful_config(
    cycle: dict, params: BacktestParams, defaults: BotConfig
) -> BotConfig:
    """Use the cycle's recorded config, with explicit study params winning."""
    recorded = cycle.get("strategy_config")
    config_fields = {
        "take_profit_pct",
        "stop_loss_pct",
        "max_volatility_1h",
        "max_borrow_apr",
        "btc_dominance_rise_threshold",
        "max_usdc_utilization",
        "max_recent_liquidations",
        "signal_reversal_exit",
        "signal_reversal_min_score",
        "min_hold_hours",
        "tp_on_strong_signal",
        "require_strong_short",
        "moderate_short_min_7d_change",
        "require_ema_bull_long",
        "min_rsi_long",
        "max_hold_days",
        "long_max_hold_days",
        "trailing_stop_pct",
        "long_trailing_stop_pct",
        "short_trailing_stop_pct",
        "post_tp_gate_hours",
        "post_trailing_stop_gate_hours",
        "post_max_hold_gate_hours",
        "liquidity_escape_utilization",
        "liquidity_escape_velocity",
        "max_funding_rate_long",
        "max_funding_rate_short",
        "max_fear_greed_long",
        "min_fear_greed_short",
        "fear_greed_short_rsi_floor",
        "min_volume_24h_usd",
    }
    recorded_values = (
        {
            key: value
            for key, value in recorded.items()
            if key in config_fields and value is not None
        }
        if isinstance(recorded, dict)
        else {}
    )
    cfg = replace(defaults, **recorded_values)
    explicit = {
        "take_profit_pct": params.take_profit_pct,
        "stop_loss_pct": params.stop_loss_pct,
        "leverage": params.leverage,
        "max_volatility_1h": params.max_volatility_1h,
        "max_borrow_apr": params.max_borrow_apr,
        "btc_dominance_rise_threshold": params.btc_dominance_rise_threshold,
    }
    return replace(
        cfg, **{key: value for key, value in explicit.items() if value is not None}
    )


def _faithful_liquidity_escape(
    cycle: dict, previous: Optional[dict], direction: str, cfg: BotConfig
) -> Optional[str]:
    if direction == "long":
        utilization = _number(cycle.get("usdc_utilization"))
        previous_utilization = _number(
            previous.get("usdc_utilization") if previous else None
        )
        frozen = cycle.get("borrow_asset_frozen")
        paused = cycle.get("borrow_asset_paused")
        supply_paused = cycle.get("asset_paused")
    else:
        utilization = _number(
            cycle.get("short_asset_utilization")
            if cycle.get("short_asset_utilization") is not None
            else cycle.get("asset_utilization")
        )
        previous_utilization = _number(
            (
                previous.get("short_asset_utilization")
                if previous and previous.get("short_asset_utilization") is not None
                else previous.get("asset_utilization")
                if previous
                else None
            )
        )
        frozen = cycle.get("short_asset_frozen")
        paused = cycle.get("short_asset_paused")
        supply_paused = cycle.get("borrow_asset_paused")

    if paused or supply_paused:
        return "liquidity_escape_paused"
    if frozen:
        return "liquidity_escape_frozen"
    if utilization is not None and utilization > cfg.liquidity_escape_utilization:
        return "liquidity_escape_utilization"
    if (
        utilization is not None
        and previous_utilization is not None
        and utilization - previous_utilization > cfg.liquidity_escape_velocity
    ):
        return "liquidity_escape_velocity"
    return None


def _trailing_pct(cfg: BotConfig, direction: str) -> float:
    if direction == "long" and cfg.long_trailing_stop_pct > 0:
        return cfg.long_trailing_stop_pct
    if direction == "short" and cfg.short_trailing_stop_pct > 0:
        return cfg.short_trailing_stop_pct
    return cfg.trailing_stop_pct


def _elapsed_hours(start: object, end: object) -> Optional[float]:
    if not start or not end:
        return None
    try:
        opened = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        if opened.tzinfo is None and current.tzinfo is not None:
            opened = opened.replace(tzinfo=current.tzinfo)
        if current.tzinfo is None and opened.tzinfo is not None:
            current = current.replace(tzinfo=opened.tzinfo)
        return max((current - opened).total_seconds(), 0.0) / 3600
    except (TypeError, ValueError):
        return None


def _faithful_entry_gate(
    cycle: dict,
    sig: signal_mod.Signal,
    last_close: Optional[dict],
    cfg: BotConfig,
    ts: str,
) -> bool:
    """Return whether a recorded live entry gate should suppress this entry."""
    if last_close and last_close.get("direction") == sig.direction:
        reason = last_close.get("reason")
        gate_hours = {
            "take_profit": cfg.post_tp_gate_hours,
            "trailing_stop": cfg.post_trailing_stop_gate_hours,
            "max_hold_days": cfg.post_max_hold_gate_hours,
        }.get(reason)
        if gate_hours is not None:
            age = _elapsed_hours(last_close.get("ts"), ts)
            active = gate_hours == 0 or age is None or age < gate_hours
            is_max_strength = (sig.direction == "long" and sig.score == 3) or (
                sig.direction == "short" and sig.score == 0
            )
            if (
                active
                and reason in {"take_profit", "trailing_stop"}
                and not is_max_strength
            ):
                return True
            if active and reason == "max_hold_days":
                return True

    if sig.direction == "short" and sig.score != 0:
        if cfg.moderate_short_min_7d_change < 0:
            if not (
                float(cycle.get("change_7d", 0) or 0) < cfg.moderate_short_min_7d_change
                and float(cycle.get("change_24h", 0) or 0) < 0
            ):
                return True
        elif cfg.require_strong_short:
            return True

    if (
        sig.direction == "long"
        and cfg.require_ema_bull_long
        and cycle.get("tech_source") is not None
        and cycle.get("tech_ema_bull") is False
    ):
        return True
    rsi = _number(cycle.get("tech_rsi"))
    if (
        sig.direction == "long"
        and cfg.min_rsi_long > 0
        and rsi is not None
        and rsi < cfg.min_rsi_long
    ):
        return True

    # A reserve that is paused/frozen is not a valid new-position route.
    if sig.direction == "long" and any(
        cycle.get(key) is True
        for key in (
            "asset_frozen",
            "asset_paused",
            "borrow_asset_frozen",
            "borrow_asset_paused",
        )
    ):
        return True
    if sig.direction == "short" and any(
        cycle.get(key) is True
        for key in (
            "short_asset_frozen",
            "short_asset_paused",
            "borrow_asset_frozen",
            "borrow_asset_paused",
        )
    ):
        return True
    return False


def _result_from_trades(
    cycles: list[dict],
    params: BacktestParams,
    sim_trades: list[SimTrade],
    incomplete_snapshots: int,
    faithful: bool,
) -> BacktestResult:
    pnls = [trade.realised_usd for trade in sim_trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl <= 0]
    total_pnl = sum(pnls)
    return BacktestResult(
        params=_params_dict(params),
        total_cycles=len(cycles),
        simulated_trades=len(sim_trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(pnls), 4) if pnls else 0.0,
        total_pnl_usd=round(total_pnl, 2),
        avg_pnl_usd=round(total_pnl / len(pnls), 2) if pnls else 0.0,
        best_trade_usd=round(max(pnls), 2) if pnls else 0.0,
        worst_trade_usd=round(min(pnls), 2) if pnls else 0.0,
        max_drawdown_usd=round(_max_drawdown(pnls), 2),
        trades=sim_trades,
        gross_pnl_usd=round(
            sum(trade.realised_usd + trade.cost_usd for trade in sim_trades), 2
        ),
        total_cost_usd=round(sum(trade.cost_usd for trade in sim_trades), 2),
        incomplete_snapshot_cycles=incomplete_snapshots,
        faithful_replay=faithful,
    )


def _estimate_cost(open_trade: dict, close_ts: str, params: BacktestParams) -> float:
    """Estimate explicit trading costs without pretending they are exact."""
    seed = float(open_trade.get("seed_usd", 0) or 0)
    leverage = float(open_trade.get("leverage", 0) or 0)
    cost = seed * leverage * 2 * params.round_trip_fee_bps / 10_000
    cost += params.gas_usd_per_trade
    if params.include_borrow_cost:
        try:
            opened = datetime.fromisoformat(
                str(open_trade.get("entry_ts", "")).replace("Z", "+00:00")
            )
            closed = datetime.fromisoformat(str(close_ts).replace("Z", "+00:00"))
            days = max((closed - opened).total_seconds(), 0.0) / 86_400
            borrow_apr = float(open_trade.get("borrow_apr", 0) or 0)
            debt_usd = seed * max(leverage - 1, 0)
            cost += debt_usd * (borrow_apr / 100) * days / 365
        except (TypeError, ValueError):
            pass
    return max(cost, 0.0)


def _number(value: object) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _recorded_signal(cycle: dict) -> Optional[signal_mod.Signal]:
    label = cycle.get("signal")
    mapping = {
        "strong_long": (3, 1.0, "long"),
        "moderate_long": (2, 0.5, "long"),
        "moderate_short": (1, 0.5, "short"),
        "strong_short": (0, 1.0, "short"),
        "hold": (0, 0.0, "none"),
    }
    if label not in mapping:
        return None
    score, multiplier, direction = mapping[label]
    return signal_mod.Signal(score, label, multiplier, direction)


def _market_data(cycle: dict) -> MarketData:
    """Build the same filter input shape from a recorded live cycle."""
    return MarketData(
        price=float(cycle.get("price", 0) or 0),
        change_1h=float(cycle.get("change_1h", 0) or 0),
        change_24h=float(cycle.get("change_24h", 0) or 0),
        change_7d=float(cycle.get("change_7d", 0) or 0),
        borrow_apr=float(cycle.get("borrow_apr", 0) or 0),
        btc_dominance=float(cycle.get("btc_dominance_pct", 0) or 0),
        health_factor=float(cycle.get("health_factor", 999) or 999),
        total_collateral_usd=float(cycle.get("wallet_collateral_usd", 0) or 0),
        position_data={},
        position_available=cycle.get("position_data_available", True) is not False,
        onchain_available=cycle.get("onchain_data_available", True) is not False,
        volume_24h=_number(cycle.get("volume_24h")),
        funding_rate=_number(cycle.get("funding_rate")),
        fear_greed=(
            int(cycle["fear_greed"]) if cycle.get("fear_greed") is not None else None
        ),
        usdc_utilization=_number(cycle.get("usdc_utilization")),
        asset_utilization=_number(cycle.get("asset_utilization")),
        short_asset_utilization=_number(cycle.get("short_asset_utilization")),
        recent_liquidations=(
            int(cycle["recent_liquidations"])
            if cycle.get("recent_liquidations") is not None
            else None
        ),
        asset_frozen=cycle.get("asset_frozen"),
        asset_paused=cycle.get("asset_paused"),
        borrow_asset_frozen=cycle.get("borrow_asset_frozen"),
        borrow_asset_paused=cycle.get("borrow_asset_paused"),
        short_asset_frozen=cycle.get("short_asset_frozen"),
        short_asset_paused=cycle.get("short_asset_paused"),
        risk_data_available=cycle.get("risk_data_available") is True,
        short_borrow_apr=_number(cycle.get("short_borrow_apr")),
    )


def _max_drawdown(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    peak = cumulative = max_dd = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _params_dict(p: BacktestParams) -> dict:
    return {k: v for k, v in vars(p).items() if v is not None}
