"""
3-timeframe trend score engine.
Counts how many of (1h, 24h, 7d) price changes are positive
and maps the result to a labeled signal with a position-size multiplier.
"""
from dataclasses import dataclass


@dataclass
class Signal:
    score: int          # 0–3: number of positive timeframes
    label: str          # "strong_long" | "moderate_long" | "moderate_short" | "strong_short"
    multiplier: float   # position size multiplier (0.0 = no trade this cycle)
    direction: str      # "long" | "short" | "none"


def compute(change_1h: float, change_24h: float, change_7d: float) -> Signal:
    """
    Count positive timeframes and return the corresponding Signal.

    Score → label → direction:
      3 → strong_long    → long   (full size)
      2 → moderate_long  → long   (half size)
      1 → moderate_short → short  (half size)
      0 → strong_short   → short  (full size)
    """
    changes = [change_1h, change_24h, change_7d]
    positives = sum(c > 0 for c in changes)
    negatives = sum(c < 0 for c in changes)

    # All changes are exactly zero — data missing or market genuinely flat → hold
    if positives == 0 and negatives == 0:
        return Signal(score=0, label="hold", multiplier=0.0, direction="none")

    if positives == 3:
        return Signal(score=3, label="strong_long",    multiplier=1.0, direction="long")
    if positives == 2:
        return Signal(score=2, label="moderate_long",  multiplier=0.5, direction="long")
    if positives == 1:
        return Signal(score=1, label="moderate_short", multiplier=0.5, direction="short")
    return     Signal(score=0, label="strong_short",   multiplier=1.0, direction="short")
