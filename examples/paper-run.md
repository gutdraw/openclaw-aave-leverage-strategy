# Example: One Paper Trading Cycle

This shows a complete cycle run with WETH on Base mainnet in paper mode.
Numbers are realistic but illustrative.

---

## Config (before the run)

```yaml
paper_trading: true
asset: "WETH"
position_id: "WETH/USDC"
user_address: "0xAbCd...1234"
max_leverage: 3.0
base_position_pct: 0.20
strong_signal_size: 1.0
moderate_signal_size: 0.5
take_profit_pct: 0.05
stop_loss_pct: 0.03
max_usdc_supply_apy: 3.0
max_volatility_1h: 5.0
btc_dominance_rise_threshold: 2.0
hf_reduce_threshold: 1.35
hf_close_threshold: 1.20
min_open_hf: 1.30
```

---

## Step 1 — Read state from trades.jsonl

No prior entries — this is the first run. No open position. No prior BTC dominance to compare.
BTC dominance filter will be skipped this cycle.

---

## Step 2 — Fetch market data

**CoinGecko prices:**
```
GET https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=ethereum&price_change_percentage=1h,24h,7d
```

Response (relevant fields):
```json
{
  "current_price": 3150.00,
  "price_change_percentage_1h_in_currency": 0.82,
  "price_change_percentage_24h_in_currency": 1.15,
  "price_change_percentage_7d_in_currency": 3.40
}
```

**DeFi Llama USDC supply APY:**
```
GET https://yields.llama.fi/pools
→ filter: project=aave-v3, chain=Base, symbol=USDC
→ apyBase: 2.58
```

**CoinGecko BTC dominance:**
```
GET https://api.coingecko.com/api/v3/global
→ data.market_cap_percentage.btc: 56.3
```

All 3 sources succeeded.

---

## Step 3 — Compute trend score

| Timeframe | Change | Positive? |
|-----------|--------|-----------|
| 1h | +0.82% | Yes |
| 24h | +1.15% | Yes |
| 7d | +3.40% | Yes |

3 out of 3 positive → **trend_score: `strong_long`**

---

## Step 4 — Apply no-trade filters

| Filter | Check | Result |
|--------|-------|--------|
| Volatility spike | abs(0.82%) > 5.0%? | No |
| Borrow cost | 2.58% > 3.0%? | No |
| BTC dominance rising | No prior cycle | Skipped |
| Position already open | No open position | No |

No filters triggered. Signal proceeds.

---

## Step 5 — No open position — compute sizing

```
get_position("0xAbCd...1234")
→ No Aave position open
→ WETH balance: 0.05 WETH (~$157.50)
→ wallet_balance_usd = $157.50

seed_usd = $157.50 * 0.20 * 1.0  (strong signal multiplier)
         = $31.50
```

Prepare call returns projected HF 1.52 for 3x leverage on $31.50 seed → above `min_open_hf` (1.30). Proceed.

---

## Step 6 — Paper mode: record open, skip execution

No `prepare_open` call made. Trade entry written as if executed at $3150.00.

**Trade entry written to trades.jsonl:**
```json
{"type":"trade","ts":"2026-03-22T14:00:00Z","paper":true,"action":"open","asset":"WETH","direction":"long","leverage":3.0,"seed_usd":31.50,"entry_price":3150.00,"position_id":"WETH/USDC","hf_after":1.52,"liquidation_price":2100.00,"signal":"strong_long"}
```

---

## Step 7 — Write cycle entry

```json
{"type":"cycle","ts":"2026-03-22T14:00:00Z","paper":true,"asset":"WETH","current_price":3150.00,"price_change_1h":0.82,"price_change_24h":1.15,"price_change_7d":3.40,"trend_score":"strong_long","usdc_supply_apy":2.58,"btc_dominance_pct":56.3,"btc_dominance_prev":null,"volatility_1h_abs":0.82,"filters_triggered":[],"sources_failed":[],"decision":"open_long","reason":"all 3 timeframes positive, no filters triggered","position_open":true}
```

---

## Step 8 — P&L summary (after first open)

```
=== Strategy P&L Summary ===
Mode:           paper
Asset:          WETH
Total cycles:   1
Total trades:   1
  Open:         1 (WETH long @ $3150.00, unrealized: $0.00)
  Closed:       0
Win rate:       — (no closed trades)
Total net P&L:  $0.00
Avg trade P&L:  —
Best trade:     —
Worst trade:    —
```

---

## Four cycles later — take profit triggered

ETH has risen to $3,315.00 (+5.24% from entry).

Cycle check: `3315.00 >= 3150.00 * 1.05` → **take_profit triggered**

**Trade entry written:**
```json
{"type":"trade","ts":"2026-03-22T18:00:00Z","paper":true,"action":"close","asset":"WETH","direction":"long","entry_price":3150.00,"exit_price":3315.00,"exit_reason":"take_profit","pnl_pct":5.24,"pnl_usd":4.95,"fees_usd":0.24,"net_pnl_usd":4.71}
```

Fee estimate (paper):
- Flash loan × 2: 0.09% × $63 × 2 = $0.11  (flash amount = seed × (leverage-1) = $31.50 × 2 = $63)
- Swap × 2: 0.05% × $63 × 2 = $0.06
- Protocol fee: 0.10% × $31.50 = $0.03
- Gas: ~$0.04
- Total: ~$0.24

**P&L summary after close:**
```
=== Strategy P&L Summary ===
Mode:           paper
Asset:          WETH
Total cycles:   5
Total trades:   2
  Open:         0
  Closed:       1
Win rate:       100.0%  (1W / 0L)
Total net P&L:  +$4.71
Avg trade P&L:  +$4.71
Best trade:     +$4.71  (WETH long, take_profit)
Worst trade:    +$4.71  (WETH long, take_profit)
```

---

## Reading the log

```bash
# View all entries
cat trades.jsonl | jq '.'

# View only trade entries
cat trades.jsonl | jq 'select(.type=="trade")'

# View all decisions
cat trades.jsonl | jq 'select(.type=="cycle") | {ts, trend_score, decision, reason}'

# Compute total net P&L
cat trades.jsonl | jq 'select(.type=="trade" and .action=="close") | .net_pnl_usd' | paste -sd+ | bc
```
