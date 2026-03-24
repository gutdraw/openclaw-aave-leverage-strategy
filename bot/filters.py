"""
No-trade filter pipeline.
Filters are evaluated in priority order; the first triggered blocks the cycle.
"""
from dataclasses import dataclass, field
from typing import Optional

from bot.config import BotConfig
from bot.market import MarketData


@dataclass
class FilterResult:
    blocked: bool
    triggered: list[str] = field(default_factory=list)
    decision: Optional[str] = None   # logged to trades.jsonl cycle entry


def apply_all(
    data: MarketData,
    signal_label: str,
    signal_direction: str,
    open_trade: Optional[dict],
    btc_dominance_prev: Optional[float],
    cfg: BotConfig,
) -> FilterResult:
    """
    Run all filters in priority order.

    Filter 1 — Volatility spike (hard block, exits cycle immediately).
    Filter 2 — Borrow cost too high (suppresses new entries only).
    Filter 3 — BTC dominance (suppresses based on direction):
                Rising dom → suppress longs (money fleeing alts)
                Falling dom → suppress shorts (alts rallying against BTC)
    Filter 4 — Position overlap (any position already open).

    Filters 2-4 only block *opening* new positions; they do not affect
    managing an existing position (close / reduce / health-factor defense).
    """
    is_long  = signal_direction == "long"
    is_short = signal_direction == "short"

    # ── Filter 1: Volatility spike ────────────────────────────────────────
    if abs(data.change_1h) > cfg.max_volatility_1h:
        return FilterResult(
            blocked=True,
            triggered=["volatility"],
            decision="skip_volatility",
        )

    # ── Filter 2: Borrow cost ─────────────────────────────────────────────
    if open_trade is None and data.borrow_apr > cfg.max_borrow_apr:
        return FilterResult(
            blocked=True,
            triggered=["borrow_apr"],
            decision="skip_borrow_apr",
        )

    # ── Filter 3: BTC dominance ───────────────────────────────────────────
    # Skipped on the first cycle (no previous reading) and when no position would open.
    if open_trade is None and btc_dominance_prev is not None:
        dom_change = data.btc_dominance - btc_dominance_prev

        # Rising dom = BTC taking share from alts → bad for long alts
        if is_long and dom_change > cfg.btc_dominance_rise_threshold:
            return FilterResult(
                blocked=True,
                triggered=["btc_dominance"],
                decision="skip_btc_dominance",
            )

        # Falling dom = alts taking share from BTC → bad for short alts
        if is_short and dom_change < -cfg.btc_dominance_rise_threshold:
            return FilterResult(
                blocked=True,
                triggered=["btc_dominance"],
                decision="skip_btc_dominance",
            )

    # ── Filter 4: Position overlap ────────────────────────────────────────
    if open_trade is not None:
        return FilterResult(
            blocked=True,
            triggered=["already_open"],
            decision="skip_already_open",
        )

    return FilterResult(blocked=False)
