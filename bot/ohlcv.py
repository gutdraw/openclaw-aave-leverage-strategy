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
    obv_bull: Optional[bool] = None  # OBV EMA(12)>EMA(26) on 1h; None = no volume data
    macd_bull: Optional[bool] = (
        None  # MACD histogram > 0 on 1h; None = insufficient data
    )
    adx: Optional[float] = (
        None  # Average Directional Index on 1h; <20=ranging, >25=trending
    )
    volume_ratio: Optional[float] = None  # latest 1h volume / 20-bar avg; >3 = spike


def fetch_multi(asset: str, timeout: int = 15) -> Optional[TechSignal]:
    """
    Fetch 1h, intermediate (~4-6h), and 1d candles and compute a
    multi-timeframe EMA signal.

    Falls back to 1h-only scoring if higher timeframes are unavailable.
    """
    # ── 1h (required) ────────────────────────────────────────────────────
    closes_1h, vols_1h, highs_1h, lows_1h, source = _fetch_tf(asset, 3600, 60, timeout)
    if closes_1h is None or len(closes_1h) < _MIN_CANDLES:
        log.debug("ohlcv: 1h candles unavailable or insufficient for %s", asset)
        return None

    # ── intermediate ~4-6h (optional) ────────────────────────────────────
    # Coinbase supports 6h (21600s); Kraken supports 4h (240m).
    closes_mid, _, _, _, _ = _fetch_tf(asset, 21600, 240, timeout)

    # ── 1d (optional) ────────────────────────────────────────────────────
    closes_1d, _, _, _, _ = _fetch_tf(asset, 86400, 1440, timeout)

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

    # ── Volume / momentum indicators on 1h ───────────────────────────────
    obv_b = _obv_bull(closes_1h, vols_1h) if vols_1h else None
    macd_b = (_macd_hist(closes_1h) > 0) if len(closes_1h) >= EMA_SLOW + 9 + 5 else None
    adx_v = (
        _adx(closes_1h, highs_1h, lows_1h)
        if highs_1h and lows_1h and len(closes_1h) >= 30
        else None
    )
    vol_ratio = _volume_ratio(vols_1h) if vols_1h and len(vols_1h) >= 21 else None

    score = _multi_tf_score(bull_1h, rsi_1h, bull_mid, bull_1d, obv_b, macd_b)

    log.debug(
        "ohlcv %s: 1h_bull=%s mid_bull=%s 1d_bull=%s rsi=%.1f obv_bull=%s macd_bull=%s adx=%.1f vol_ratio=%.2f → score=%d",
        asset,
        bull_1h,
        bull_mid,
        bull_1d,
        rsi_1h,
        obv_b,
        macd_b,
        adx_v or 0.0,
        vol_ratio or 1.0,
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
        obv_bull=obv_b,
        macd_bull=macd_b,
        adx=round(adx_v, 1) if adx_v is not None else None,
        volume_ratio=round(vol_ratio, 2) if vol_ratio is not None else None,
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
    obv_bull: Optional[bool] = None,
    macd_bull: Optional[bool] = None,
) -> int:
    """
    Combine three timeframe EMA directions into a score 0–4.

    Primary trend is set by the two highest available timeframes.
    They must agree for any directional signal — disagreement → hold.
    1h + RSI only affect whether the signal is moderate or strong.
    Falls back to 1h-only if neither higher TF is available.

    OBV and MACD act as a divergence gate on strong signals only:
    if BOTH contradict the direction, strong → moderate.
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
            # Both OBV and MACD bearish while EMAs are bullish = momentum divergence
            if obv_bull is False and macd_bull is False:
                return 3  # downgrade: strong → moderate
            return 4  # strong_long
        return 3  # moderate_long: higher TFs bullish, 1h not yet confirmed

    else:  # primary == "bear"
        if not bull_1h and rsi_bear:
            # Both OBV and MACD bullish while EMAs are bearish = momentum divergence
            if obv_bull is True and macd_bull is True:
                return 1  # downgrade: strong → moderate
            return 0  # strong_short
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
) -> tuple[
    Optional[list[float]],
    Optional[list[float]],
    Optional[list[float]],
    Optional[list[float]],
    str,
]:
    """
    Fetch OHLCV for a given timeframe.
    Tries Coinbase first, then Kraken.
    Returns (closes, volumes, highs, lows, source_name) oldest→newest.
    """
    result = _fetch_coinbase(asset, coinbase_granularity, timeout)
    if result is not None:
        return result[0], result[1], result[2], result[3], "coinbase"
    result = _fetch_kraken(asset, kraken_interval, timeout)
    if result is not None:
        return result[0], result[1], result[2], result[3], "kraken"
    return None, None, None, None, "unavailable"


def _fetch_coinbase(
    asset: str, granularity: int, timeout: int
) -> Optional[tuple[list[float], list[float], list[float], list[float]]]:
    """
    Fetch up to 300 candles from Coinbase Exchange at the given granularity (seconds).
    Returns (closes, volumes, highs, lows) oldest→newest.
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
        candles_asc = list(reversed(candles))
        closes = [float(c[4]) for c in candles_asc]
        volumes = [float(c[5]) for c in candles_asc]
        highs = [float(c[2]) for c in candles_asc]
        lows = [float(c[1]) for c in candles_asc]
        return closes, volumes, highs, lows
    except Exception as e:
        log.debug("ohlcv coinbase error (gran=%d) for %s: %s", granularity, asset, e)
        return None


def _fetch_kraken(
    asset: str, interval: int, timeout: int
) -> Optional[tuple[list[float], list[float], list[float], list[float]]]:
    """
    Fetch up to 720 candles from Kraken at the given interval (minutes).
    Returns (closes, volumes, highs, lows) oldest→newest.
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
        closes = [float(c[4]) for c in result[key]]
        volumes = [float(c[6]) for c in result[key]]
        highs = [float(c[2]) for c in result[key]]
        lows = [float(c[3]) for c in result[key]]
        return closes, volumes, highs, lows
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


def _ema_series(values: list[float], period: int) -> list[float]:
    """EMA series across all values — needed for MACD signal line."""
    k = 2.0 / (period + 1)
    ema = values[0]
    result = [ema]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
        result.append(ema)
    return result


def _macd_hist(closes: list[float]) -> float:
    """
    MACD histogram = MACD line - signal line.
    MACD line = EMA(12) - EMA(26). Signal line = EMA(9) of MACD line.
    Positive histogram = bullish momentum; negative = fading/bearish.
    """
    ema_fast = _ema_series(closes, EMA_FAST)
    ema_slow = _ema_series(closes, EMA_SLOW)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, 9)
    return macd_line[-1] - signal_line


def _obv_bull(closes: list[float], volumes: list[float]) -> bool:
    """
    On-Balance Volume trend: True if OBV EMA(12) > OBV EMA(26).
    OBV accumulates volume on up closes and subtracts on down closes.
    Divergence from price (e.g. OBV falling while price rising) signals
    weakening conviction behind the move.
    """
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return _ema(obv, EMA_FAST) > _ema(obv, EMA_SLOW)


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


def _adx(
    closes: list[float], highs: list[float], lows: list[float], period: int = 14
) -> float:
    """
    Average Directional Index (Wilder smoothing).
    Measures trend strength regardless of direction.
    <20 = ranging, 20-25 = transition, >25 = strong trend.
    """
    if len(closes) < period * 2 + 1:
        return 0.0
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm_list.append(up if up > down and up > 0 else 0.0)
        ndm_list.append(down if down > up and down > 0 else 0.0)

    def _wilder(values: list[float], n: int) -> list[float]:
        out = [sum(values[:n])]
        for v in values[n:]:
            out.append(out[-1] - out[-1] / n + v)
        return out

    atr = _wilder(tr_list, period)
    pdm_s = _wilder(pdm_list, period)
    ndm_s = _wilder(ndm_list, period)

    dx_list = []
    for a, p, n in zip(atr, pdm_s, ndm_s):
        if a == 0:
            dx_list.append(0.0)
            continue
        pdi = 100 * p / a
        ndi = 100 * n / a
        dx_list.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)

    if len(dx_list) < period:
        return 0.0
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _volume_ratio(volumes: list[float], lookback: int = 20) -> float:
    """Ratio of latest 1h volume to the prior N-bar average.
    >3 = potential capitulation spike; used as a counter-trend entry gate.
    """
    if len(volumes) < lookback + 1:
        return 1.0
    avg = sum(volumes[-lookback - 1 : -1]) / lookback
    return volumes[-1] / avg if avg > 0 else 1.0
