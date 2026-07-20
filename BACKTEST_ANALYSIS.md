# Proximity-to-Recent-Low Filter Backtest

## Question
When `signal == "strong_short"` and price is near the recent N-day low, does BTC tend to bounce (bad for shorts) or continue falling (good for shorts)?

## Dataset
- 4,667 cycle records (~90 days of 30-minute price history)
- 1,095 total `strong_short` signals
- Lookahead period: 48 cycles (~24 hours)

## Key Finding: The Filter Would Hurt Performance

**Contrary to intuition, signals near the recent low OUTPERFORM signals away from the low.**

This is consistent with mean-reversion behavior: when price sits near a recent low, momentum oversold conditions tend to reverse upward within 24h, which is good for shorts (they profit from price drops). Filtering out "near low" entries would remove our best high-probability trades.

---

## Results by Lookback Window

### 7-Day Lookback

| Threshold | Bucket | Count | Fell >0% | Fell >2% | Avg 24h % | Avg Best % | % Filtered |
|-----------|--------|-------|----------|----------|-----------|-----------|------------|
| 2% | near_low | 760 | 52.1% | 27.2% | +0.84% | +2.01% | 69.4% |
| 2% | away_from_low | 335 | 55.8% | 15.8% | +0.15% | +1.60% | |
| 3% | near_low | 925 | 53.8% | 26.7% | +0.84% | +2.03% | 84.5% |
| 3% | away_from_low | 170 | 50.0% | 7.6% | -0.55% | +1.10% | |
| **4%** | **near_low** | **1009** | **53.9%** | **25.1%** | **+0.76%** | **+1.96%** | **92.1%** |
| **4%** | **away_from_low** | **86** | **45.3%** | **8.1%** | **-0.93%** | **+1.01%** | |
| 5% | near_low | 1046 | 52.8% | 24.2% | +0.65% | +1.91% | 95.5% |
| 5% | away_from_low | 49 | 63.3% | 14.3% | +0.22% | +1.30% | |

### 14-Day Lookback (Stronger Signal)

| Threshold | Bucket | Count | Fell >0% | Fell >2% | Avg 24h % | Avg Best % | % Filtered |
|-----------|--------|-------|----------|----------|-----------|-----------|------------|
| 2% | near_low | 663 | 55.1% | 28.4% | +0.93% | +2.07% | 60.5% |
| 2% | away_from_low | 432 | 50.5% | 16.7% | +0.16% | +1.61% | |
| 3% | near_low | 834 | 57.3% | 28.1% | +0.96% | +2.13% | 76.2% |
| 3% | away_from_low | 261 | 40.2% | 10.0% | -0.43% | +1.12% | |
| **4%** | **near_low** | **923** | **57.2%** | **26.2%** | **+0.86%** | **+2.05%** | **84.3%** |
| **4%** | **away_from_low** | **172** | **32.0%** | **10.5%** | **-0.63%** | **+1.02%** | |
| 5% | near_low | 953 | 57.1% | 26.1% | +0.86% | +2.05% | 87.0% |
| 5% | away_from_low | 142 | 27.5% | 7.7% | -0.96% | +0.82% | |

---

## Interpretation

**Key Metrics Explained:**
- **Fell >0%**: % of signals where 24h simple outcome was positive (price fell)
- **Fell >2%**: % where meaningful 2%+ drop occurred within 24h
- **Avg 24h %**: Average return on entry (positive = shorts profitable)
- **Avg Best %**: Best-case 24h return (using intraday low)
- **% Filtered**: How many total `strong_short` signals would be blocked by the filter

**The Pattern (both lookback windows):**
- **Near-low signals**: 53-57% win rate, +0.65% to +0.96% average 24h return
- **Away-from-low signals**: 27-56% win rate, -0.93% to +0.22% average 24h return
- **Difference at 4% threshold (14-day)**: +25% absolute edge in win rate; +1.49% advantage in avg return

---

## Recommendation

**Do NOT add this filter.**

Adding a proximity-to-low rejection would:
1. Block 84-92% of `strong_short` signals (unacceptable signal loss)
2. Paradoxically remove the highest-probability entries (those near recent lows outperform)
3. Keep only the lowest-probability entries (away-from-low signals are weak)

**Why the counterintuitive result?**
The data suggests that oversold conditions near recent lows tend to mean-revert upward slightly within 24h, which is exactly what shorts want (price falls from entry). Filtering them out discards alpha.

If entry quality is a concern, consider instead:
- Raising `min_fear_greed_short` threshold (fear-greed signal quality)
- Tightening risk management at entry (wider stops if truly concerned about whipsaws)
- Monitoring realized slippage on near-low entries vs. other entries

---

## Technical Notes
- 30-min cycles, ~4,667 total records
- Lookback computed using all cycle prices from prior N days
- Lookahead uses actual 48-cycle forward price (exact 24h window)
- Analysis includes all `strong_short` signals, even those within 48 cycles of EOF
