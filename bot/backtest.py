"""
Parameter replay backtester.

Uses the price series already recorded in trades.jsonl cycle entries
to simulate how different TP/SL/filter/sizing parameters would have performed.

No external data needed — replays the actual price history the bot observed.
Returns a comparison of simulated vs actual results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import bot.signal as signal_mod
import bot.state as state


@dataclass
class BacktestParams:
    """Parameters to test. None = use value from the cycle entries as-is."""
    take_profit_pct: Optional[float] = None   # e.g. 7.0
    stop_loss_pct: Optional[float] = None     # e.g. 4.0
    leverage: Optional[float] = None          # e.g. 2.5
    base_position_pct: Optional[float] = None # e.g. 0.15
    max_volatility_1h: Optional[float] = None # e.g. 3.0
    max_borrow_apr: Optional[float] = None    # e.g. 6.0
    btc_dominance_rise_threshold: Optional[float] = None  # e.g. 1.5


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


@dataclass
class BacktestResult:
    params: dict                    # what was tested
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


def run(
    params: BacktestParams,
    trades_file: str = "trades.jsonl",
    seed_usd: Optional[float] = None,
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
    """
    entries = state.load_entries(trades_file)
    cycles  = [e for e in entries if e.get("type") == "cycle"]

    if not cycles:
        return BacktestResult(
            params=_params_dict(params), total_cycles=0,
            simulated_trades=0, wins=0, losses=0,
            win_rate=0.0, total_pnl_usd=0.0, avg_pnl_usd=0.0,
            best_trade_usd=0.0, worst_trade_usd=0.0, max_drawdown_usd=0.0,
        )

    seed = seed_usd or 1000.0
    tp   = params.take_profit_pct
    sl   = params.stop_loss_pct
    lev  = params.leverage or 3.0
    vol  = params.max_volatility_1h or 5.0
    apr  = params.max_borrow_apr or 8.0
    dom_thresh = params.btc_dominance_rise_threshold or 2.0

    sim_trades: list[SimTrade] = []
    open_trade: Optional[dict] = None   # keys: direction, entry_price, entry_ts, signal, seed_usd
    prev_dom: Optional[float] = None

    for cycle in cycles:
        price      = float(cycle.get("price", 0))
        change_1h  = float(cycle.get("change_1h", 0))
        change_24h = float(cycle.get("change_24h", 0))
        change_7d  = float(cycle.get("change_7d", 0))
        borrow_apr = float(cycle.get("borrow_apr", 0))
        btc_dom    = float(cycle.get("btc_dominance_pct", 0))
        ts         = cycle.get("ts", "")

        if price <= 0:
            prev_dom = btc_dom
            continue

        # ── Check exit on open trade ──────────────────────────────────────
        if open_trade is not None:
            entry   = open_trade["entry_price"]
            dirn    = open_trade["direction"]
            _tp     = tp if tp is not None else float(cycle.get("take_profit_pct_used", 5.0))
            _sl     = sl if sl is not None else float(cycle.get("stop_loss_pct_used", 3.0))

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
                realised = _compute_pnl(open_trade, price)
                sim_trades.append(SimTrade(
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
                ))
                open_trade = None

        if open_trade is not None:
            prev_dom = btc_dom
            continue  # already in a trade

        # ── Signal ────────────────────────────────────────────────────────
        sig = signal_mod.compute(change_1h, change_24h, change_7d)
        if sig.multiplier == 0:
            prev_dom = btc_dom
            continue

        # ── Filters ───────────────────────────────────────────────────────
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
        }
        prev_dom = btc_dom

    # ── Compute stats ─────────────────────────────────────────────────────
    pnls = [t.realised_usd for t in sim_trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl  = sum(pnls)
    avg_pnl    = total_pnl / len(pnls) if pnls else 0.0
    best       = max(pnls) if pnls else 0.0
    worst      = min(pnls) if pnls else 0.0
    max_dd     = _max_drawdown(pnls)
    win_rate   = len(wins) / len(pnls) if pnls else 0.0

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
    )


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
            "max_drawdown_usd": round(result_b.max_drawdown_usd - result_a.max_drawdown_usd, 2),
            "verdict": "improvement" if result_b.total_pnl_usd > result_a.total_pnl_usd else "regression",
        },
    }


def _compute_pnl(open_trade: dict, close_price: float) -> float:
    entry  = open_trade["entry_price"]
    seed   = open_trade["seed_usd"]
    lev    = open_trade["leverage"]
    dirn   = open_trade["direction"]
    if dirn == "short":
        borrow_units = seed * (lev - 1) / entry
        return borrow_units * (entry - close_price)
    supply_units = seed / entry
    return supply_units * (close_price - entry) * lev


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
