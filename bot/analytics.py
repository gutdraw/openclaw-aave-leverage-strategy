"""
Performance analytics — reads trades.jsonl and returns structured metrics.

Designed to be called by Hermes or any agent that wants to evaluate
strategy performance before proposing config changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import bot.state as state


@dataclass
class SignalMetrics:
    signal: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl_usd: float
    total_pnl_usd: float


@dataclass
class PerformanceReport:
    # ── Overview ──────────────────────────────────────────────────────────
    total_cycles: int
    total_trades: int
    open_trades: int
    closed_trades: int

    # ── P&L ───────────────────────────────────────────────────────────────
    win_rate: float                  # closed trades only
    total_pnl_usd: float
    avg_pnl_usd: float
    best_trade_usd: float
    worst_trade_usd: float
    max_drawdown_usd: float          # largest peak→trough in cumulative P&L

    # ── By signal ─────────────────────────────────────────────────────────
    by_signal: dict[str, SignalMetrics]

    # ── Filters ───────────────────────────────────────────────────────────
    filter_counts: dict[str, int]    # decision → number of cycles with that decision

    # ── Hold time ─────────────────────────────────────────────────────────
    avg_hold_cycles: float           # cycles between open and close

    # ── Exit reasons ──────────────────────────────────────────────────────
    exit_reasons: dict[str, int]     # reason → count

    # ── Open position snapshot ────────────────────────────────────────────
    open_position: Optional[dict]    # the current open trade entry, or None

    # ── Recommendation hints ──────────────────────────────────────────────
    hints: list[str] = field(default_factory=list)


def analyze(trades_file: str = "trades.jsonl") -> PerformanceReport:
    """
    Read trades.jsonl and compute a full performance report.
    """
    entries = state.load_entries(trades_file)
    cycles  = [e for e in entries if e.get("type") == "cycle"]
    trades  = [e for e in entries if e.get("type") == "trade"]
    opens   = [t for t in trades if t.get("action") == "open"]
    closes  = [t for t in trades if t.get("action") == "close"]

    open_position = state.get_open_trade(entries)

    # ── Pair opens with closes ────────────────────────────────────────────
    pairs: list[tuple[dict, dict]] = []
    open_stack: list[dict] = []
    for t in trades:
        if t.get("action") == "open":
            open_stack.append(t)
        elif t.get("action") == "close" and open_stack:
            pairs.append((open_stack.pop(), t))

    # ── P&L per closed trade ──────────────────────────────────────────────
    pnls: list[float] = []
    for o, c in pairs:
        pnl = float(c.get("realised_usd", 0))
        pnls.append(pnl)

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate      = len(wins) / len(pnls) if pnls else 0.0
    total_pnl     = sum(pnls)
    avg_pnl       = total_pnl / len(pnls) if pnls else 0.0
    best_trade    = max(pnls) if pnls else 0.0
    worst_trade   = min(pnls) if pnls else 0.0

    # Max drawdown (peak → trough in cumulative P&L curve)
    max_drawdown  = _max_drawdown(pnls)

    # ── By signal ─────────────────────────────────────────────────────────
    signal_map: dict[str, list[float]] = {}
    for (o, c), pnl in zip(pairs, pnls):
        sig = o.get("signal", "unknown")
        signal_map.setdefault(sig, []).append(pnl)

    by_signal: dict[str, SignalMetrics] = {}
    for sig, sig_pnls in signal_map.items():
        sig_wins = [p for p in sig_pnls if p > 0]
        by_signal[sig] = SignalMetrics(
            signal=sig,
            trades=len(sig_pnls),
            wins=len(sig_wins),
            losses=len(sig_pnls) - len(sig_wins),
            win_rate=len(sig_wins) / len(sig_pnls) if sig_pnls else 0.0,
            avg_pnl_usd=sum(sig_pnls) / len(sig_pnls) if sig_pnls else 0.0,
            total_pnl_usd=sum(sig_pnls),
        )

    # ── Filter / decision counts ──────────────────────────────────────────
    filter_counts: dict[str, int] = {}
    for c in cycles:
        decision = c.get("decision", "unknown")
        filter_counts[decision] = filter_counts.get(decision, 0) + 1

    # ── Exit reasons ──────────────────────────────────────────────────────
    exit_reasons: dict[str, int] = {}
    for _, c in pairs:
        reason = c.get("reason", c.get("exit_reason", "unknown"))
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # ── Avg hold time (cycles between open and close timestamps) ──────────
    hold_cycles = _avg_hold_cycles(pairs, cycles)

    # ── Recommendation hints ──────────────────────────────────────────────
    hints = _generate_hints(pnls, by_signal, filter_counts, exit_reasons, pairs)

    return PerformanceReport(
        total_cycles=len(cycles),
        total_trades=len(trades),
        open_trades=1 if open_position else 0,
        closed_trades=len(closes),
        win_rate=round(win_rate, 4),
        total_pnl_usd=round(total_pnl, 2),
        avg_pnl_usd=round(avg_pnl, 2),
        best_trade_usd=round(best_trade, 2),
        worst_trade_usd=round(worst_trade, 2),
        max_drawdown_usd=round(max_drawdown, 2),
        by_signal=by_signal,
        filter_counts=filter_counts,
        avg_hold_cycles=round(hold_cycles, 1),
        exit_reasons=exit_reasons,
        open_position=open_position,
        hints=hints,
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


def _avg_hold_cycles(
    pairs: list[tuple[dict, dict]],
    cycles: list[dict],
) -> float:
    """Estimate hold time in cycles using timestamps."""
    if not pairs or not cycles:
        return 0.0

    holds: list[int] = []
    cycle_ts = [c.get("ts", "") for c in cycles]

    for o, c in pairs:
        open_ts  = o.get("ts", "")
        close_ts = c.get("ts", "")
        try:
            open_idx  = next(i for i, t in enumerate(cycle_ts) if t >= open_ts)
            close_idx = next(i for i, t in enumerate(cycle_ts) if t >= close_ts)
            holds.append(max(1, close_idx - open_idx))
        except StopIteration:
            pass

    return sum(holds) / len(holds) if holds else 0.0


def _generate_hints(
    pnls: list[float],
    by_signal: dict[str, SignalMetrics],
    filter_counts: dict[str, int],
    exit_reasons: dict[str, int],
    pairs: list[tuple[dict, dict]],
) -> list[str]:
    """Generate actionable improvement hints based on the data."""
    hints: list[str] = []
    if len(pnls) < 5:
        hints.append("Not enough closed trades for reliable analysis (need ≥5). Keep running paper cycles.")
        return hints

    overall_wr = len([p for p in pnls if p > 0]) / len(pnls)

    # Win rate by signal
    for sig, m in by_signal.items():
        if m.trades >= 3 and m.win_rate < 0.40:
            hints.append(
                f"{sig} has low win rate ({m.win_rate:.0%} over {m.trades} trades). "
                f"Consider increasing stop_loss_pct to give trades more room, "
                f"or disabling this signal with a lower moderate_signal_size."
            )
        if m.trades >= 3 and m.win_rate > 0.70:
            hints.append(
                f"{sig} has strong win rate ({m.win_rate:.0%}). "
                f"Consider increasing take_profit_pct to capture more upside."
            )

    # Stop-loss dominated exits
    sl_count = exit_reasons.get("stop_loss", 0)
    tp_count = exit_reasons.get("take_profit", 0)
    total_exits = sl_count + tp_count
    if total_exits >= 4 and sl_count / total_exits > 0.65:
        hints.append(
            f"Stop-losses represent {sl_count}/{total_exits} exits ({sl_count/total_exits:.0%}). "
            f"stop_loss_pct may be too tight — consider widening it."
        )
    if total_exits >= 4 and tp_count / total_exits > 0.70:
        hints.append(
            f"Take-profits represent {tp_count}/{total_exits} exits ({tp_count/total_exits:.0%}). "
            f"take_profit_pct may be too low — consider raising it to let winners run."
        )

    # Overactive filters
    total_cycles = sum(filter_counts.values())
    for decision, count in filter_counts.items():
        if decision.startswith("skip_") and total_cycles > 0:
            pct = count / total_cycles
            if pct > 0.40:
                hints.append(
                    f"Filter '{decision}' is blocking {pct:.0%} of cycles. "
                    f"This may be too aggressive — review its threshold."
                )

    # Overall P&L
    if overall_wr >= 0.55 and sum(pnls) > 0:
        hints.append(
            f"Strategy is profitable ({overall_wr:.0%} win rate, "
            f"${sum(pnls):.2f} total). Current parameters look reasonable."
        )

    return hints


def report_to_dict(r: PerformanceReport) -> dict:
    """Serialize PerformanceReport to a JSON-serializable dict."""
    return {
        "total_cycles": r.total_cycles,
        "total_trades": r.total_trades,
        "open_trades": r.open_trades,
        "closed_trades": r.closed_trades,
        "win_rate": r.win_rate,
        "total_pnl_usd": r.total_pnl_usd,
        "avg_pnl_usd": r.avg_pnl_usd,
        "best_trade_usd": r.best_trade_usd,
        "worst_trade_usd": r.worst_trade_usd,
        "max_drawdown_usd": r.max_drawdown_usd,
        "avg_hold_cycles": r.avg_hold_cycles,
        "by_signal": {
            k: {
                "trades": v.trades, "wins": v.wins, "losses": v.losses,
                "win_rate": v.win_rate, "avg_pnl_usd": v.avg_pnl_usd,
                "total_pnl_usd": v.total_pnl_usd,
            }
            for k, v in r.by_signal.items()
        },
        "filter_counts": r.filter_counts,
        "exit_reasons": r.exit_reasons,
        "open_position": r.open_position,
        "hints": r.hints,
    }
