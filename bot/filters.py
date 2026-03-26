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
    ohlcv_rsi: Optional[float] = None,
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

    # ── Filter 4: Funding rate (crowded positioning) ──────────────────────
    # Only applied when opening new positions. If Binance is unavailable (None), skip.
    if open_trade is None and data.funding_rate is not None:
        if is_long and data.funding_rate > cfg.max_funding_rate_long:
            return FilterResult(
                blocked=True,
                triggered=["funding_rate"],
                decision="skip_funding_rate",
            )
        if is_short and data.funding_rate < -cfg.max_funding_rate_short:
            return FilterResult(
                blocked=True,
                triggered=["funding_rate"],
                decision="skip_funding_rate",
            )

    # ── Filter 5: Fear & Greed (sentiment extremes) ───────────────────────
    # Short block: only suppress when F&G is extreme fear AND RSI hasn't recovered.
    # Once RSI climbs above fear_greed_short_rsi_floor (default 35), the oversold
    # condition is gone and F&G alone is insufficient reason to avoid a short.
    if open_trade is None and data.fear_greed is not None:
        if is_long and data.fear_greed >= cfg.max_fear_greed_long:
            return FilterResult(
                blocked=True,
                triggered=["fear_greed"],
                decision="skip_fear_greed",
            )
        if is_short and data.fear_greed <= cfg.min_fear_greed_short:
            rsi_still_oversold = (
                ohlcv_rsi is None or ohlcv_rsi < cfg.fear_greed_short_rsi_floor
            )
            if rsi_still_oversold:
                return FilterResult(
                    blocked=True,
                    triggered=["fear_greed"],
                    decision="skip_fear_greed",
                )

    # ── Filter 6: Volume (low-conviction moves) ───────────────────────────
    if open_trade is None and cfg.min_volume_24h_usd > 0 and data.volume_24h is not None:
        if data.volume_24h < cfg.min_volume_24h_usd:
            return FilterResult(
                blocked=True,
                triggered=["volume"],
                decision="skip_volume",
            )

    # ── Filter 7: USDC utilization (borrow APR spike risk) ───────────────
    # Above the interest rate kink (~90%), variable borrow APR climbs steeply.
    # Suppress new entries if we're already in spike territory.
    if open_trade is None and data.usdc_utilization is not None:
        if data.usdc_utilization > cfg.max_usdc_utilization:
            return FilterResult(
                blocked=True,
                triggered=["usdc_utilization"],
                decision="skip_usdc_utilization",
            )

    # ── Filter 8: Recent liquidation cascade ──────────────────────────────
    if open_trade is None and data.recent_liquidations is not None:
        if data.recent_liquidations > cfg.max_recent_liquidations:
            return FilterResult(
                blocked=True,
                triggered=["liquidation_cascade"],
                decision="skip_liquidation_cascade",
            )

    # ── Filter 9: Position overlap ─────────────────────────────────────────
    if open_trade is not None:
        return FilterResult(
            blocked=True,
            triggered=["already_open"],
            decision="skip_already_open",
        )

    return FilterResult(blocked=False)
