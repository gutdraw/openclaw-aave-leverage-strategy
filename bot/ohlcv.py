"""
OHLCV-based technical signal engine.

Fetches hourly candles from Coinbase Exchange → Kraken (fallback).
Both are free, no auth required, accessible globally including US IPs.

Computes two indicators:
  1. EMA crossover (fast=12, slow=26) — trend direction
  2. RSI (period=14) — momentum / overbought-oversold

These are combined into a TechSignal score (0–4) that maps to the same
Signal labels used by the CoinGecko 3-timeframe engine, allowing either
to drive the bot or serve as a fallback for the other.

Score → label:
  4  strong_long    (EMA bull + RSI bullish zone 40–70)
  3  moderate_long  (EMA bull only, or RSI bullish only)
  2  hold           (conflicting signals)
  1  moderate_short (EMA bear only, or RSI bearish only)
  0  strong_short   (EMA bear + RSI bearish zone 30–60)
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
    "WETH":   "ETH-USD",
    "ETH":    "ETH-USD",
    "wstETH": "ETH-USD",   # closest proxy — no wstETH spot market
    "cbBTC":  "BTC-USD",
}
KRAKEN_PAIR: dict[str, str] = {
    "WETH":   "ETHUSD",
    "ETH":    "ETHUSD",
    "wstETH": "ETHUSD",
    "cbBTC":  "XBTUSD",
}

# EMA and RSI parameters
EMA_FAST  = 12
EMA_SLOW  = 26
RSI_PERIOD = 14

# RSI thresholds
RSI_BULL_LOW  = 40   # RSI above this = bullish momentum
RSI_BULL_HIGH = 75   # RSI above this = overbought (not bullish)
RSI_BEAR_HIGH = 60   # RSI below this = bearish momentum
RSI_BEAR_LOW  = 25   # RSI below this = oversold (not bearish)


@dataclass
class TechSignal:
    score: int             # 0–4
    ema_bull: bool         # fast EMA > slow EMA
    rsi: float             # current RSI value
    source: str            # "coinbase" | "kraken"
    candles_used: int


def fetch(asset: str, timeout: int = 15) -> Optional[TechSignal]:
    """
    Fetch hourly OHLCV candles and compute EMA crossover + RSI.
    Tries Coinbase first, falls back to Kraken.
    Returns None if both sources fail.
    """
    closes = _fetch_coinbase(asset, timeout)
    source = "coinbase"
    if closes is None:
        closes = _fetch_kraken(asset, timeout)
        source = "kraken"
    if closes is None:
        log.debug("ohlcv.fetch: both sources failed for %s", asset)
        return None

    needed = EMA_SLOW + RSI_PERIOD + 5   # enough for warm-up
    if len(closes) < needed:
        log.debug("ohlcv.fetch: insufficient candles (%d < %d)", len(closes), needed)
        return None

    ema_fast = _ema(closes, EMA_FAST)
    ema_slow = _ema(closes, EMA_SLOW)
    rsi_val  = _rsi(closes, RSI_PERIOD)
    ema_bull = ema_fast > ema_slow

    # RSI zone scoring
    rsi_bull = RSI_BULL_LOW <= rsi_val <= RSI_BULL_HIGH
    rsi_bear = RSI_BEAR_LOW <= rsi_val <= RSI_BEAR_HIGH

    # Score: 0 (strong_short) → 4 (strong_long)
    if ema_bull and rsi_bull:
        score = 4
    elif ema_bull and not rsi_bear:
        score = 3
    elif not ema_bull and rsi_bear:
        score = 1
    elif not ema_bull and not rsi_bull:
        score = 0
    else:
        score = 2  # conflicting

    return TechSignal(
        score=score,
        ema_bull=ema_bull,
        rsi=round(rsi_val, 1),
        source=source,
        candles_used=len(closes),
    )


def to_signal(ts: TechSignal) -> Signal:
    """Convert a TechSignal score to the standard Signal used by the bot."""
    if ts.score == 4:
        return Signal(score=3, label="strong_long",    multiplier=1.0, direction="long")
    if ts.score == 3:
        return Signal(score=2, label="moderate_long",  multiplier=0.5, direction="long")
    if ts.score == 2:
        return Signal(score=0, label="hold",           multiplier=0.0, direction="none")
    if ts.score == 1:
        return Signal(score=1, label="moderate_short", multiplier=0.5, direction="short")
    return     Signal(score=0, label="strong_short",   multiplier=1.0, direction="short")


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_coinbase(asset: str, timeout: int) -> Optional[list[float]]:
    """Fetch ~300 hourly closes from Coinbase Exchange. Returns close prices oldest→newest."""
    pair = COINBASE_PAIR.get(asset)
    if not pair:
        return None
    try:
        r = httpx.get(
            f"https://api.exchange.coinbase.com/products/{pair}/candles",
            params={"granularity": 3600},
            timeout=timeout,
        )
        r.raise_for_status()
        # format: [time, low, high, open, close, volume] — newest first
        candles = r.json()
        if not candles:
            return None
        closes = [float(c[4]) for c in reversed(candles)]  # oldest→newest
        return closes
    except Exception as e:
        log.debug("ohlcv coinbase error for %s: %s", asset, e)
        return None


def _fetch_kraken(asset: str, timeout: int) -> Optional[list[float]]:
    """Fetch ~720 hourly closes from Kraken. Returns close prices oldest→newest."""
    pair = KRAKEN_PAIR.get(asset)
    if not pair:
        return None
    try:
        r = httpx.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": pair, "interval": 60},
            timeout=timeout,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        key = next((k for k in result if k != "last"), None)
        if not key:
            return None
        # format: [time, open, high, low, close, vwap, volume, count] — oldest first
        closes = [float(c[4]) for c in result[key]]
        return closes
    except Exception as e:
        log.debug("ohlcv kraken error for %s: %s", asset, e)
        return None


# ── Indicator math ────────────────────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> float:
    """Exponential moving average of the last `period` values (using full series for warm-up)."""
    k = 2.0 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int) -> float:
    """Wilder's RSI using the last `period+1` closes."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    # Wilder smoothing: simple average for first, then smoothed
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
