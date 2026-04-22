"""
OHLCV-based multi-timeframe technical signal engine.

Fetches candles at three timeframes from Coinbase Exchange → Kraken (fallback).
Both are free, no auth required, accessible globally including US IPs.

Timeframes:
  1h  — Coinbase 3600s  → Kraken 60m   (entry timing)
  mid — Coinbase 21600s → Kraken 240m  (intermediate trend; ~4-6h)
  1d  — Coinbase 86400s → Kraken 1440m (primary trend)

Indicators per timeframe:
  EMA crossover (fast=12, slow=26) — trend direction

Scoring (requires higher TFs to agree):
  4  strong_long    1d bull + mid bull + 1h bull + RSI bullish
  3  moderate_long  1d bull + mid bull  (1h hasn't confirmed yet)
  2  hold           1d and mid disagree, or only 1h available
  1  moderate_short 1d bear + mid bear  (1h hasn't confirmed yet)
  0  strong_short   1d bear + mid bear + 1h bear + RSI bearish

Key property: a 1h wick while 1d and mid are still bullish → hold, not reversal.
This prevents whipsawing out of a valid trend on short-term noise.

RSI (period=14) on 1h candles only — used to confirm strength, not direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from bot.signal import Signal

log = logging.getLogger(__name__)

# ── Exchange symbol maps ──────────────────────────────────────────────────────

COINBASE_PAIR: dict[str, str] = {
    "WETH": "ETH-USD",
    "ETH": "ETH-USD",
    "wstETH": "ETH-USD",
    "cbBTC": "BTC-USD",
}
KRAKEN_PAIR: dict[str, str] = {
    "WETH": "ETHUSD",
    "ETH": "ETHUSD",
    "wstETH": "ETHUSD",
    "cbBTC": "XBTUSD",
}

# EMA and RSI parameters (period = candles, same across all timeframes)
EMA_FAST = 12
EMA_SLOW = 26
RSI_PERIOD = 14

# RSI thresholds (1h only — used for strength confirmation)
RSI_BULL_LOW = 40  # RSI above this = bullish momentum
RSI_BULL_HIGH = 75  # RSI above this = overbought (not bullish)
RSI_BEAR_HIGH = 60  # RSI below this = bearish momentum
RSI_BEAR_LOW = 25  # RSI below this = oversold (not bearish)

# Minimum candles needed per timeframe for reliable EMA warm-up
_MIN_CANDLES = EMA_SLOW + RSI_PERIOD + 5  # 45


@dataclass
class TechSignal:
    score: int  # 0–4 (maps to Signal via to_signal())
    ema_bull: bool  # 1h EMA direction
    rsi: float  # 1h RSI value
    source: str  # data source for 1h candles
    candles_used: int  # 1h candles used
    tf_mid_bull: Optional[bool] = (
        None  # intermediate TF EMA (~4-6h); None = unavailable
    )
    tf_1d_bull: Optional[bool] = None  # daily EMA direction; None = unavailable


def fetch_multi(asset: str, timeout: int = 15) -> Optional[TechSignal]:
    """
    Fetch 1h, intermediate (~4-6h), and 1d candles and compute a
    multi-timeframe EMA signal.

    Falls back to 1h-only scoring if higher timeframes are unavailable.
    """
    # ── 1h (required) ────────────────────────────────────────────────────
    closes_1h, source = _fetch_tf(asset, 3600, 60, timeout)
    if closes_1h is None or len(closes_1h) < _MIN_CANDLES:
        log.debug("ohlcv: 1h candles unavailable or insufficient for %s", asset)
        return None

    # ── intermediate ~4-6h (optional) ────────────────────────────────────
    # Coinbase supports 6h (21600s); Kraken supports 4h (240m).
    closes_mid, _ = _fetch_tf(asset, 21600, 240, timeout)

    # ── 1d (optional) ────────────────────────────────────────────────────
    closes_1d, _ = _fetch_tf(asset, 86400, 1440, timeout)

    # ── Compute indicators ────────────────────────────────────────────────
    bull_1h = _ema_bull(closes_1h)
    rsi_1h = _rsi(closes_1h, RSI_PERIOD)

    bull_mid = (
        _ema_bull(closes_mid)
        if closes_mid and len(closes_mid) >= EMA_SLOW + 5
        else None
    )
    bull_1d = (
        _ema_bull(closes_1d) if closes_1d and len(closes_1d) >= EMA_SLOW + 5 else None
    )

    score = _multi_tf_score(bull_1h, rsi_1h, bull_mid, bull_1d)

    log.debug(
        "ohlcv %s: 1h_bull=%s mid_bull=%s 1d_bull=%s rsi=%.1f → score=%d",
        asset,
        bull_1h,
        bull_mid,
        bull_1d,
        rsi_1h,
        score,
    )

    return TechSignal(
        score=score,
        ema_bull=bull_1h,
        rsi=round(rsi_1h, 1),
        source=source,
        candles_used=len(closes_1h),
        tf_mid_bull=bull_mid,
        tf_1d_bull=bull_1d,
    )


# Keep fetch() as an alias so any external callers don't break.
fetch = fetch_multi


def to_signal(ts: TechSignal) -> Signal:
    """Convert a TechSignal score (0–4) to the standard Signal used by the bot."""
    if ts.score == 4:
        return Signal(score=3, label="strong_long", multiplier=1.0, direction="long")
    if ts.score == 3:
        return Signal(score=2, label="moderate_long", multiplier=0.5, direction="long")
    if ts.score == 2:
        return Signal(score=0, label="hold", multiplier=0.0, direction="none")
    if ts.score == 1:
        return Signal(
            score=1, label="moderate_short", multiplier=0.5, direction="short"
        )
    return Signal(score=0, label="strong_short", multiplier=1.0, direction="short")


# ── Multi-timeframe scoring ───────────────────────────────────────────────────


def _multi_tf_score(
    bull_1h: bool,
    rsi_1h: float,
    bull_mid: Optional[bool],
    bull_1d: Optional[bool],
) -> int:
    """
    Combine three timeframe EMA directions into a score 0–4.

    Primary trend is set by the two highest available timeframes.
    They must agree for any directional signal — disagreement → hold.
    1h + RSI only affect whether the signal is moderate or strong.
    Falls back to 1h-only if neither higher TF is available.
    """
    rsi_bull = RSI_BULL_LOW <= rsi_1h <= RSI_BULL_HIGH
    rsi_bear = RSI_BEAR_LOW <= rsi_1h <= RSI_BEAR_HIGH

    # ── Determine primary trend from highest available TFs ────────────────
    if bull_1d is not None and bull_mid is not None:
        # Both higher TFs available — require agreement
        if bull_1d and bull_mid:
            primary = "bull"
        elif not bull_1d and not bull_mid:
            primary = "bear"
        else:
            return 2  # 1d and mid disagree → hold regardless of 1h

    elif bull_1d is not None:
        # Only daily available
        primary = "bull" if bull_1d else "bear"

    elif bull_mid is not None:
        # Only intermediate available
        primary = "bull" if bull_mid else "bear"

    else:
        # Only 1h — fall back to single-TF scoring
        return _single_tf_score(bull_1h, rsi_1h)

    # ── Combine primary trend with 1h confirmation ────────────────────────
    if primary == "bull":
        if bull_1h and rsi_bull:
            return 4  # strong_long: all timeframes aligned + RSI bullish
        return 3  # moderate_long: higher TFs bullish, 1h not yet confirmed

    else:  # primary == "bear"
        if not bull_1h and rsi_bear:
            return 0  # strong_short: all timeframes aligned + RSI bearish
        return 1  # moderate_short: higher TFs bearish, 1h not yet confirmed


def _single_tf_score(ema_bull: bool, rsi: float) -> int:
    """1h-only fallback scoring (original logic)."""
    overbought = rsi > RSI_BULL_HIGH
    oversold = rsi < RSI_BEAR_LOW
    rsi_bull = RSI_BULL_LOW <= rsi <= RSI_BULL_HIGH
    rsi_bear = RSI_BEAR_LOW <= rsi <= RSI_BEAR_HIGH

    if ema_bull and rsi_bull:
        return 4
    if ema_bull and not rsi_bear and not overbought:
        return 3
    if not ema_bull and not rsi_bull and not oversold:
        return 0
    if not ema_bull and rsi_bear and not oversold:
        return 1
    return 2


# ── Data fetchers ─────────────────────────────────────────────────────────────


def _fetch_tf(
    asset: str,
    coinbase_granularity: int,
    kraken_interval: int,
    timeout: int,
) -> tuple[Optional[list[float]], str]:
    """
    Fetch close prices for a given timeframe.
    Tries Coinbase first (granularity in seconds), then Kraken (interval in minutes).
    Returns (closes oldest→newest, source_name).
    """
    closes = _fetch_coinbase(asset, coinbase_granularity, timeout)
    if closes is not None:
        return closes, "coinbase"
    closes = _fetch_kraken(asset, kraken_interval, timeout)
    if closes is not None:
        return closes, "kraken"
    return None, "unavailable"


def _fetch_coinbase(
    asset: str, granularity: int, timeout: int
) -> Optional[list[float]]:
    """
    Fetch up to 300 candles from Coinbase Exchange at the given granularity (seconds).
    Supported: 60, 300, 900, 3600, 21600, 86400.
    Returns close prices oldest→newest.
    """
    pair = COINBASE_PAIR.get(asset)
    if not pair:
        return None
    try:
        r = httpx.get(
            f"https://api.exchange.coinbase.com/products/{pair}/candles",
            params={"granularity": granularity},
            timeout=timeout,
        )
        r.raise_for_status()
        candles = r.json()
        if not candles:
            return None
        # format: [time, low, high, open, close, volume] — newest first
        return [float(c[4]) for c in reversed(candles)]
    except Exception as e:
        log.debug("ohlcv coinbase error (gran=%d) for %s: %s", granularity, asset, e)
        return None


def _fetch_kraken(asset: str, interval: int, timeout: int) -> Optional[list[float]]:
    """
    Fetch up to 720 candles from Kraken at the given interval (minutes).
    Supported: 1, 5, 15, 30, 60, 240, 1440, 10080, 21600.
    Returns close prices oldest→newest.
    """
    pair = KRAKEN_PAIR.get(asset)
    if not pair:
        return None
    try:
        r = httpx.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": pair, "interval": interval},
            timeout=timeout,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        key = next((k for k in result if k != "last"), None)
        if not key:
            return None
        # format: [time, open, high, low, close, vwap, volume, count] — oldest first
        return [float(c[4]) for c in result[key]]
    except Exception as e:
        log.debug("ohlcv kraken error (interval=%d) for %s: %s", interval, asset, e)
        return None


# ── Indicator math ────────────────────────────────────────────────────────────


def _ema_bull(closes: list[float]) -> bool:
    """True if fast EMA > slow EMA (bullish crossover)."""
    return _ema(closes, EMA_FAST) > _ema(closes, EMA_SLOW)


def _ema(closes: list[float], period: int) -> float:
    """Exponential moving average — uses full series for warm-up."""
    k = 2.0 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int) -> float:
    """Wilder's RSI using the full series for smoothing warm-up."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))
