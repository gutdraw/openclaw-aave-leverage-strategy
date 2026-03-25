---
name: aave-leverage-strategy
description: Autonomous trend-following strategy for Aave v3 leverage on Base. Runs on a cron schedule, researches market conditions, sizes positions by signal confidence, and tracks P&L in a persistent log. Paper trading by default.
version: 1.1.0
author: gutdraw
tags: [defi, aave, leverage, base, crypto, trading, autonomous, strategy, paper-trading]
requires_skill: aave-leverage
---

# Aave Leverage Strategy

An autonomous trend-following strategy that wraps the `aave-leverage` skill with
decision-making logic. On each run it fetches market data, computes a trend signal,
and either opens, holds, adjusts, or closes a leveraged position — all without human
confirmation. Paper trading is the default: every trade is logged and P&L is tracked,
but no on-chain transactions are submitted until you explicitly go live.

## What this skill does

| Capability | Detail |
|------------|--------|
| Market research | CoinGecko 1h/24h/7d price + volume, USDC borrow cost, BTC dominance, perp funding rate, Fear & Greed index, Aave v3 on-chain utilization |
| Signal model | 3-timeframe trend score + 9 no-trade filters |
| Exit strategy | TP/SL, signal reversal (with min-hold guard), time-based (max_hold_days) |
| Position sizing | Scales seed size by signal confidence (full / half) |
| Paper trading | Full simulation, no chain calls, identical log output |
| P&L tracking | Append-only `trades.jsonl` — entry price, exit price, net P&L per trade |
| HF defense | Longs: reduce < 1.35, close < 1.20. Shorts: reduce < 1.09, close < 1.05 |
| TP / SL | Configurable % from entry, checked every cycle |

## Supported assets and directions

| Asset | Long | Short | Long max leverage | Short max leverage |
|-------|------|-------|-------------------|--------------------|
| WETH | Supply WETH, borrow USDC | Supply USDC, borrow WETH | 4.5x (config cap: 3x) | **2x hard cap** (HF ~1.17 at open) |
| cbBTC | Supply cbBTC, borrow USDC | Supply USDC, borrow cbBTC | 3.3x (config cap: 3x) | **2x hard cap** (HF ~1.17 at open) |
| wstETH | Supply wstETH, borrow WETH | — (not supported) | 4.3x (config cap: 3x) | — |

**Why 2x is the short cap:** USDC liquidation threshold on Aave v3 Base is 78%. At 2x short (supply=3×seed USDC, borrow=2×seed BTC/ETH), opening HF = 3×0.78/2 = **1.17**. At 3x short, HF = 4×0.78/3 = **1.04** — one small adverse move causes liquidation. The bot enforces this cap in `sizing.py` regardless of `leverage` config.

The bot trades both longs and shorts automatically based on the trend signal:
- 3/3 or 2/3 timeframes positive → long
- 1/3 or 0/3 timeframes positive → short

Configure the short asset via `short_borrow_asset` and `short_position_id` in `config.yml`.

## One wallet per bot instance — hard requirement

**Never run two bot instances that share the same wallet address.**

Each bot instance only tracks its own `trades.jsonl` to know whether a position is open.
Two instances sharing a wallet will not see each other's trades, leading to:

- Both bots opening simultaneously (double exposure)
- Both bots triggering health-factor defense at the same time (double reduce/close)
- Aave's cross-collateral HF being depleted unexpectedly when a second position opens

**If you want to trade multiple assets**, run one instance per asset, each with its own:
- Dedicated bot wallet (funded separately)
- Own `config.yml` pointing to that wallet
- Own `trades.jsonl` file
- Own MCP session token (tokens are wallet-bound)

```
bot-eth/
  config.yml       ← user_address: 0xWALLET_ETH
  trades.jsonl

bot-btc/
  config.yml       ← user_address: 0xWALLET_BTC
  trades.jsonl
```

This keeps each bot's risk fully isolated. If the ETH bot gets liquidated, it has zero
effect on the BTC bot's collateral or health factor.

---

## MCP session and x402 payments

This skill calls the `aave-leverage` MCP server on every cycle. The server uses the
x402 payment protocol — sessions are paid in USDC on Base and are wallet-bound.

**Pricing:**

| Duration | Cost  | Best for |
|----------|-------|----------|
| 1 hour   | $0.05 | Testing, one-shot cycles |
| 1 day    | $0.25 | Short-term runs |
| 1 week   | $1.50 | Active monitoring |
| 1 month  | $4.00 | Production bots, cron jobs |

**Recommended for autonomous use: monthly session ($4.00)**

A cron running every 15 minutes makes ~2,880 tool calls per month. The monthly pass
at $4.00 is the most cost-effective option — it covers the full billing period for
less than three weekly passes. An hourly session ($0.05) expires between runs and
triggers a new payment on every cycle where the session has lapsed. Buy a monthly
session before starting the bot to avoid repeated micro-payments and potential failed
cycles due to payment confirmation delays.

**How x402 works:**

On the first tool call after a session expires, the MCP server returns HTTP 402
(Payment Required). OpenClaw handles this automatically — it signs a USDC transfer
from the bot wallet, submits it on-chain, and retries once confirmed. This adds
~10–30s to that cycle. All subsequent calls in the session are instant.

**Pre-run checklist:**

- Bot wallet has USDC on Base (at minimum $4.00 for a monthly session; $1.50 for weekly)
- Bot wallet has ETH on Base for gas (~$2–5 is sufficient for weeks of runs)
- `X-Wallet-Address` in `mcp-config.json` matches the bot wallet

**If a cycle fails due to payment error:**

The cycle log entry will record `"decision": "skip_payment_error"`. This is not a
trading decision — it means the session expired and OpenClaw could not complete
payment (most likely insufficient USDC). Top up the bot wallet and the next cycle
will trigger a fresh session payment automatically.

**What the agent must NOT do on payment failure:**

- Do not retry the tool call in the same cycle — the payment confirmation may still be pending
- Do not open or close positions if `get_position` hasn't returned successfully
- Write the cycle entry and exit; the next scheduled run will recover

---

## Configuration

All parameters live in `config.yml` next to this skill. Edit before your first run.
The file is gitignored — it contains your wallet address.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `paper_trading` | bool | `true` | Master switch. `false` = real transactions. Do not change until you have 20+ paper cycles. |
| `asset` | string | `"WETH"` | Long collateral asset: `WETH`, `cbBTC`, or `wstETH`. |
| `position_id` | string | `"WETH/USDC"` | Long position ID for `prepare_close`. Must match `asset`. |
| `short_borrow_asset` | string | `"WETH"` | Asset to borrow for shorts: `WETH` or `cbBTC`. |
| `short_position_id` | string | `"USDC/WETH"` | Short position ID for `prepare_close`. Must match `short_borrow_asset`. |
| `user_address` | string | — | Your dedicated bot wallet address. **Required. Never share with another bot instance.** |
| `leverage` | float | `3.0` | Leverage for long positions (WETH max safe 4.5x, cbBTC 3.3x). |
| `max_leverage` | float | `4.0` | Hard cap for longs. MCP server also enforces per-asset ceiling. |
| `short_max_leverage` | float | `2.0` | Hard cap for shorts. Enforced in code regardless of `leverage`. 3x short HF ~1.04. |
| `base_position_pct` | float | `0.20` | Fraction of total collateral used as seed on a strong signal (1.0 multiplier). |
| `strong_signal_size` | float | `1.0` | Multiplier on `base_position_pct` for a strong signal (3/3 or 0/3 timeframes). |
| `moderate_signal_size` | float | `0.5` | Multiplier for a moderate signal (2/3 or 1/3 timeframes). |
| `take_profit_pct` | float | `5.0` | Close when position is up this % from entry. |
| `stop_loss_pct` | float | `3.0` | Close when position is down this % from entry. |
| `max_borrow_apr` | float | `8.0` | Skip new entries if USDC borrow APR exceeds this %. |
| `max_volatility_1h` | float | `5.0` | Skip entire cycle if 1h price move exceeds this % in either direction. |
| `btc_dominance_rise_threshold` | float | `2.0` | Suppress longs if BTC dom rose > this %; suppress shorts if BTC dom fell > this %. |
| `hf_defense_reduce` | float | `1.35` | Call `prepare_reduce` if HF drops below this (longs). |
| `hf_defense_close` | float | `1.20` | Force close if HF drops below this (longs). |
| `min_open_hf` | float | `1.30` | Skip opening a long if current HF is below this. |
| `short_hf_defense_reduce` | float | `1.09` | Call `prepare_reduce` if HF drops below this (shorts). ~7% adverse move at 2x. Must be < 1.17 (2x short open HF). |
| `short_hf_defense_close` | float | `1.05` | Force close if HF drops below this (shorts). ~11% adverse move at 2x. Liquidation is at ~17% adverse. |
| `short_min_open_hf` | float | `1.12` | Skip opening a short if current HF is below this. |
| `signal_reversal_exit` | bool | `true` | Close position when trend flips against it. |
| `signal_reversal_min_score` | int | `0` | Min reversal score to trigger: 0=strong only, 1=moderate+, 2=any opposing signal. |
| `min_hold_hours` | float | `2.0` | Minimum hours before signal reversal can close a position (prevents whipsaw). |
| `max_hold_days` | float | `14.0` | Force-close after this many days to avoid carry drag. Set 0 to disable. |
| `tp_on_strong_signal` | bool | `false` | When false (default), TP is skipped if signal is still at max strength — let winners ride. SL always applies. |
| `max_funding_rate_long` | float | `0.05` | Skip longs if Binance perp funding > this % per 8h (crowded longs). |
| `max_funding_rate_short` | float | `0.05` | Skip shorts if Binance perp funding < -this % per 8h (crowded shorts). |
| `max_fear_greed_long` | int | `85` | Skip longs if Fear & Greed ≥ this (extreme greed — market over-extended). |
| `min_fear_greed_short` | int | `15` | Skip shorts if Fear & Greed ≤ this (extreme fear / capitulation). |
| `min_volume_24h_usd` | float | `0` | Skip new entries if 24h spot volume < this USD. 0 = disabled. |
| `rpc_url` | string | `"https://mainnet.base.org"` | Base RPC for on-chain reads. Use Alchemy for `eth_getLogs` with longer lookback. |
| `onchain_lookback_blocks` | int | `10` | Block lookback for liquidation event scan. Alchemy free tier: max 10 (~20s on Base). |
| `max_usdc_utilization` | float | `0.92` | Skip new entries if Aave USDC pool utilization > this (borrow rate kink at ~90%). |
| `max_recent_liquidations` | int | `3` | Skip new entries if this many LiquidationCall events in the lookback window. |

**To go live:** change `paper_trading: false` in `config.yml`. All other behavior is identical.

---

## Market research

At the start of every cycle, fetch data from three sources in this order:

### Source 1 — CoinGecko price trend (required)

```
GET https://api.coingecko.com/api/v3/coins/markets
  ?vs_currency=usd
  &ids=<coingecko_id>
  &price_change_percentage=1h,24h,7d
```

CoinGecko ID by asset:
- WETH → `ethereum`
- cbBTC → `coinbase-wrapped-btc`
- wstETH → `wrapped-steth`

Fields to extract:
- `current_price` — current asset price in USD (used for paper trade entry/exit)
- `price_change_percentage_1h_in_currency` — 1h % change
- `price_change_percentage_24h_in_currency` — 24h % change
- `price_change_percentage_7d_in_currency` — 7d % change

> Note: 4h data is not available on the CoinGecko free tier. This skill uses 1h / 24h / 7d.

### Source 2 — Aave borrow rate (via get_position)

```
get_position(user_address)
```

The `get_position` tool now returns live Aave interest rates directly from the chain.
No external API call needed — this is the real borrow APR, not a proxy.

Field to extract: `rates.USDC.borrowApy` (USDC variable borrow APR %)

Also available: `rates.WETH.borrowApy`, `rates.WETH.supplyApy`, `rates.USDC.carryCost`, etc.

This replaces the previous DeFi Llama proxy method. The no-trade filter
(`max_borrow_apr`) is now compared directly against `rates.USDC.borrowApy`.

### Source 3 — CoinGecko BTC dominance (required)

```
GET https://api.coingecko.com/api/v3/global
```

Field to extract: `data.market_cap_percentage.btc` (current BTC dominance %)

The 24h change in BTC dominance is computed by comparing this value to the
`btc_dominance_pct` field from the most recent cycle entry in `trades.jsonl`.
If no prior cycle exists, skip this filter for the first run.

### Source 4 — Perp funding rate (soft — failure logged, not blocking)

Tries Binance → Bybit → OKX in order. Binance and Bybit geo-block US IPs (HTTP 451/403);
OKX is accessible globally without auth.

```
GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol=ETHUSDT
GET https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT
GET https://www.okx.com/api/v5/public/funding-rate?instId=ETH-USDT-SWAP
```

Field to extract: `lastFundingRate` (Binance) / `fundingRate` (Bybit/OKX), multiply by 100 to get % per 8h.

Asset → symbol mapping: WETH/wstETH → ETHUSDT / ETH-USDT-SWAP, cbBTC → BTCUSDT / BTC-USDT-SWAP.

### Source 5 — Aave v3 Base on-chain state (soft — failure logged, not blocking)

Read directly from Base via eth_call and eth_getLogs. Uses the configured `rpc_url`
(default public Base RPC; use Alchemy for longer lookback windows).

**USDC pool utilization:**
```
varDebtToken.totalSupply() / aToken.totalSupply()
```
- USDC aToken: `0x4e65fE4DbA92790696d040ac24Aa414708F5c0AB`
- USDC varDebtToken: `0x59dca05b6c26dbd64b5381374aaac5cd05644c28`

Also fetched for the trade asset (WETH, cbBTC) as `asset_utilization`.

**Recent liquidations:**
```
eth_getLogs for LiquidationCall on Aave v3 Pool in last onchain_lookback_blocks blocks
```
- Aave v3 Pool: `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5`
- Topic: keccak256("LiquidationCall(address,address,address,uint256,uint256,address,bool)")
- Alchemy free tier: max 10 blocks (~20s). PAYG: set `onchain_lookback_blocks: 150` (~5 min).

### Source 6 — Fear & Greed Index (soft — failure logged, not blocking)

```
GET https://api.alternative.me/fng/?limit=1
```

Field to extract: `data[0].value` (integer 0–100).
0 = extreme fear, 100 = extreme greed.

### Fallback behavior

Sources 1-3 (CoinGecko prices, get_position, CoinGecko global) are required. Sources 4-6 are soft — if unavailable, the corresponding filter is simply skipped.

- If any required source fails: log in `sources_failed`, continue with remaining sources.
- If fewer than 2 required sources succeed: write a cycle entry with
  `"decision": "skip_insufficient_data"` and exit without acting.
- Price is always mandatory: if CoinGecko prices fail, exit regardless of other sources.
- Note: `get_position` is always called if a position is open (Step 6) — if it fails,
  treat it as a hard stop and exit without acting regardless of other sources.

---

## Signal model

### Step 1 — Compute trend score

Count how many of the three timeframes show positive price change:

| Positives (out of 3) | Signal | Direction | Multiplier | Action |
|---------------------|--------|-----------|------------|--------|
| 3 | `strong_long` | long | 1.0 | Open long at full size |
| 2 | `moderate_long` | long | 0.5 | Open long at half size |
| 1 | `moderate_short` | short | 0.5 | Open short at half size |
| 0 | `strong_short` | short | 1.0 | Open short at full size |

Example: ETH is +0.8% (1h), +1.2% (24h), -0.5% (7d) → 2 positives → `moderate_long`

### Step 2 — Apply no-trade filters (in priority order)

**Filter 1 — Volatility spike** (skips entire cycle)
- Trigger: `abs(price_change_1h) > max_volatility_1h`
- Action: write cycle entry with `"decision": "skip_volatility"`, exit immediately
- Why: entering a leveraged position into a 1h spike is high-risk in both directions

**Filter 2 — Elevated borrow cost** (suppresses all new entries)
- Trigger: `rates.USDC.borrowApy > max_borrow_apr` (from `get_position` response)
- Action: set decision to `hold` or `no_trade`, log `"filters_triggered": ["borrow_cost"]`
- Why: high carry cost reduces the edge needed to profit; wait for rates to normalize

**Filter 3 — BTC dominance shift** (direction-aware)
- Long trigger: `btc_dominance_pct - btc_dominance_prev > btc_dominance_rise_threshold`
  → suppress longs; rising BTC dom = capital rotating into BTC away from alts
- Short trigger: `btc_dominance_prev - btc_dominance_pct > btc_dominance_rise_threshold`
  → suppress shorts; falling BTC dom = alt season, alts rallying against BTC
- Action: log `"filters_triggered": ["btc_dominance"]`
- Note: `btc_dominance_prev` is read from the last cycle entry in `trades.jsonl`.
  If no prior cycle exists, skip this filter.

**Filter 4 — Funding rate crowding** (direction-aware, suppresses new entries only)
- Long trigger: `funding_rate > max_funding_rate_long` (longs paying shorts heavily — crowded)
- Short trigger: `funding_rate < -max_funding_rate_short` (shorts paying longs heavily — crowded)
- Skipped if funding rate unavailable (soft source)
- Why: extreme funding = crowded positioning, mean-reversion risk

**Filter 5 — Fear & Greed sentiment** (direction-aware, suppresses new entries only)
- Long trigger: `fear_greed >= max_fear_greed_long` (extreme greed — market over-extended)
- Short trigger: `fear_greed <= min_fear_greed_short` (extreme fear / capitulation)
- Skipped if F&G unavailable (soft source)
- Why: extreme sentiment often precedes reversals

**Filter 6 — Volume floor** (suppresses new entries only)
- Trigger: `volume_24h < min_volume_24h_usd` (disabled by default: 0)
- Why: low liquidity → wider spreads, slippage, and manipulation risk

**Filter 7 — USDC pool utilization** (suppresses new entries only)
- Trigger: `usdc_utilization > max_usdc_utilization` (default 0.92)
- Skipped if on-chain unavailable (soft source)
- Why: Aave's borrow rate kinks sharply above ~90% utilization. At 92% the variable APR is already well above normal — the MCP `borrow_apr` field is too slow to catch this in real-time.

**Filter 8 — Liquidation cascade** (suppresses new entries only)
- Trigger: `recent_liquidations > max_recent_liquidations` in last `onchain_lookback_blocks` blocks
- Skipped if on-chain unavailable (soft source)
- Why: entering into a cascade amplifies risk. Even a small number of liquidations in a 20s window is a stress signal.

**Filter 9 — Position already open in same direction** (suppresses duplicate entry)
- Trigger: open trade exists in `trades.jsonl` with same direction as current signal
- Action: set decision to `hold`, do not open another position
- Why: one position at a time — never stack leverage

---

## Position sizing

When a signal passes all filters and no position is open:

```
seed_usd = total_collateral_usd * base_position_pct * signal_multiplier
```

Where:
- `total_collateral_usd` — from `get_position` response. If no position open and no
  collateral in Aave, use `balances.<asset>.usd` from the same response.
- `signal_multiplier` — `strong_signal_size` (default 1.0) for `strong_long`,
  `moderate_signal_size` (default 0.5) for `moderate_long`

**Pre-open health factor check:**

Before opening, estimate the projected health factor:

```
projected_hf ≈ (seed_usd * max_leverage) / (seed_usd * (max_leverage - 1)) * ltv_factor
```

If projected HF < `min_open_hf` (default 1.30), skip the open and log
`"decision": "skip_hf_too_low"`.

In practice, calling `prepare_open` will return the exact projected HF in its response —
use that value to confirm before proceeding to execution steps.

---

## Per-cycle execution flow

Execute these steps in order on every run:

**Step 1 — Read config**
Load all parameters from `config.yml`. If the file is missing or `user_address` is
still the placeholder, exit with an error.

**Step 2 — Read state from trades.jsonl**
Scan `trades.jsonl` (if it exists) to find:
- The last open position: most recent `type=trade, action=open` with no subsequent
  matching `action=close` for the same asset/direction
- The last cycle's `btc_dominance_pct` for the BTC dominance filter

**Step 3 — Fetch market data**
Fetch all three sources (CoinGecko prices, DeFi Llama, CoinGecko global).
If fewer than 2 succeed, write a skipped cycle entry and exit.

**Step 4 — Compute trend score**
Apply the signal model to the fetched price changes.

**Step 5 — Apply no-trade filters**
Check all 9 filters in priority order. Record which filters triggered.

**Step 6 — Check open position (if one exists)**
Call `get_position(user_address)` to get current HF and on-chain state.

- a. If `health_factor < hf_close_threshold` → force close regardless of signal
  (exit_reason: `hf_defense_close`)
- b. Else if `health_factor < hf_reduce_threshold` → call `prepare_reduce` to bring
  leverage down toward `min_open_hf` target (exit_reason: `hf_defense_reduce`)
- c. Else check exit conditions:
  - If `current_price >= entry_price * (1 + take_profit_pct)` AND
    (`tp_on_strong_signal: true` OR signal not at max strength) → close
    (exit_reason: `take_profit`)
  - If `current_price <= entry_price * (1 - stop_loss_pct)` → close
    (exit_reason: `stop_loss`)
  - If `signal_reversal_exit: true` AND trend_score is opposite direction AND
    score >= `signal_reversal_min_score` AND position age >= `min_hold_hours` → close
    (exit_reason: `signal_reversal`)
  - If `max_hold_days > 0` AND position age > `max_hold_days` → close
    (exit_reason: `max_hold_days`)

**Step 7 — Open position (if no position open and signal is actionable)**
- Compute `seed_usd` from sizing formula
- Call `prepare_open` to get projected HF — verify > `min_open_hf`
- **Paper mode**: record open trade entry, skip execution steps
- **Live mode**: execute all transaction steps from `prepare_open` response in order;
  if any step fails, log the error, do not retry in this cycle, exit

**Step 8 — Write cycle entry to trades.jsonl**
Append a cycle entry with all market data, signal, filters, decision, and current
position state. Always written regardless of what happened.

**Step 9 — Compute and print P&L summary**
Read all trade entries from `trades.jsonl`, compute summary, print to output.

---

## Exit rules

Every exit writes a `type=trade, action=close` entry to `trades.jsonl` with:
- `exit_price` — current asset price (from CoinGecko or `get_position`)
- `exit_reason` — one of the strings below
- `pnl_pct`, `pnl_usd`, `fees_usd`, `net_pnl_usd`

| Exit reason | Trigger | Priority |
|-------------|---------|----------|
| `hf_defense_close` | HF < `hf_close_threshold` | Highest — overrides all |
| `hf_defense_reduce` | HF < `hf_reduce_threshold` | High — reduces, does not close |
| `take_profit` | price >= entry * (1 + `take_profit_pct`), unless `tp_on_strong_signal: false` and signal still at max strength | Normal |
| `stop_loss` | price <= entry * (1 - `stop_loss_pct`) | Normal |
| `signal_reversal` | trend score flips to opposite direction, score >= `signal_reversal_min_score`, position age >= `min_hold_hours` | Normal |
| `max_hold_days` | position open longer than `max_hold_days` days | Normal |

HF defense always runs before exit condition checks. If HF is fine, check TP/SL/reversal/time.

**Signal reversal details:**
- Only triggers if `signal_reversal_exit: true` (default)
- `signal_reversal_min_score: 0` — only a strong reversal (all 3 timeframes) triggers close. Set to 1 for moderate or strong, 2 for any opposing signal.
- `min_hold_hours` — prevents the reversal exit from firing within N hours of open. Protects against 30-minute whipsaw on noisy mid-timeframe data. Default: 2h.
- A "strong" signal reversal means score ≥ `signal_reversal_min_score` in the opposite direction. With score=0, only a score of 3 against the position triggers.

**TP suppression on strong signal:**
- When `tp_on_strong_signal: false` (default): if the signal is still at maximum strength (all 3 timeframes aligned), take-profit is skipped. The position stays open to let winners ride.
- Stop-loss and HF defense always apply regardless of this setting.
- Set `tp_on_strong_signal: true` to restore fixed-TP behavior.

**P&L computation for closed trades:**
```
pnl_pct    = (exit_price - entry_price) / entry_price   [for longs]
pnl_usd    = seed_usd * leverage * pnl_pct
fees_usd   = (flash_loan_fee * 2) + (swap_fee * 2) + protocol_fee
           = (0.0009 * seed_usd * (leverage-1) * 2)
           + (0.0005 * seed_usd * (leverage-1) * 2)
           + (0.001 * seed_usd)
net_pnl_usd = pnl_usd - fees_usd
```

In paper mode, fees are estimated using the formula above. In live mode, actual fees
are embedded in the transaction steps from `prepare_open` / `prepare_close`.

---

## Trade log (trades.jsonl)

`trades.jsonl` is an append-only log file that lives next to `config.yml`.
It is gitignored — you are responsible for backing it up.

Each line is a self-contained JSON object. Two entry types:

### Cycle entry (written every run)

```json
{
  "type": "cycle",
  "ts": "2026-03-22T14:00:00Z",
  "paper": true,
  "asset": "WETH",
  "current_price": 3150.00,
  "price_change_1h": 0.8,
  "price_change_24h": 1.2,
  "price_change_7d": 3.1,
  "trend_score": "strong_long",
  "usdc_borrow_apr": 4.2,
  "btc_dominance_pct": 56.3,
  "btc_dominance_prev": 55.8,
  "volatility_1h_abs": 0.8,
  "volume_24h": 12500000000.0,
  "funding_rate": 0.007,
  "fear_greed": 62,
  "usdc_utilization": 0.718,
  "asset_utilization": 0.065,
  "recent_liquidations": 0,
  "filters_triggered": [],
  "sources_failed": [],
  "decision": "open_long",
  "reason": "all 3 timeframes positive, no filters triggered",
  "position_open": false
}
```

Soft-source fields (`volume_24h`, `funding_rate`, `fear_greed`, `usdc_utilization`, `asset_utilization`, `recent_liquidations`) are `null` when the source is unavailable.

### Trade entry — open

```json
{
  "type": "trade",
  "ts": "2026-03-22T14:00:00Z",
  "paper": true,
  "action": "open",
  "asset": "WETH",
  "direction": "long",
  "leverage": 3.0,
  "seed_usd": 100.0,
  "entry_price": 3150.00,
  "position_id": "WETH/USDC",
  "hf_after": 1.55,
  "liquidation_price": 2100.00,
  "signal": "strong_long"
}
```

### Trade entry — close

```json
{
  "type": "trade",
  "ts": "2026-03-22T18:00:00Z",
  "paper": true,
  "action": "close",
  "asset": "WETH",
  "direction": "long",
  "entry_price": 3150.00,
  "exit_price": 3307.50,
  "exit_reason": "take_profit",
  "pnl_pct": 5.0,
  "pnl_usd": 15.00,
  "fees_usd": 0.76,
  "net_pnl_usd": 14.24
}
```

**Finding the last open position:**
Scan the file from the end. The last `type=trade, action=open` entry with no subsequent
`type=trade, action=close` for the same asset/direction is the open position.

---

## P&L summary

Printed at the end of every cycle. Computed by reading all `type=trade` entries
from `trades.jsonl` and pairing sequential open/close entries.

```
=== Strategy P&L Summary ===
Mode:           paper
Asset:          WETH
Total cycles:   24
Total trades:   12
  Open:         1 (WETH long @ $3150.00, unrealized: +$47.25)
  Closed:       11
Win rate:       63.6%  (7W / 4L)
Total net P&L:  +$84.20
Avg trade P&L:  +$7.65
Best trade:     +$31.50  (WETH long, take_profit)
Worst trade:    -$18.90  (WETH long, stop_loss)
```

**Unrealized P&L** (for open positions):
```
unrealized_pnl_usd = (current_price - entry_price) / entry_price * seed_usd * leverage
```

**To inspect the log directly:**
```bash
# All trades
cat trades.jsonl | jq 'select(.type=="trade")'

# Closed trades only
cat trades.jsonl | jq 'select(.type=="trade" and .action=="close")'

# All cycle decisions
cat trades.jsonl | jq 'select(.type=="cycle") | {ts, decision, reason}'

# Running net P&L
cat trades.jsonl | jq 'select(.type=="trade" and .action=="close") | .net_pnl_usd' | paste -sd+ | bc
```

---

## Paper trading

Paper trading mode runs the complete strategy — data fetching, signal computation,
position sizing, exit checks — but skips the execution step. Trades are recorded
in `trades.jsonl` as if they executed at the current market price.

**What is identical in paper mode:**
- All market research and data fetching
- Signal computation and filter application
- Position sizing calculations
- `get_position` calls (reads your real on-chain state)
- Log output — cycle and trade entries are identical
- P&L summary

**What is skipped in paper mode:**
- `prepare_open`, `prepare_close`, `prepare_reduce`, `prepare_increase` calls
- All transaction execution steps
- No gas, no on-chain state changes

**Validation before going live:**
1. Run at least 20 cycles across varied market conditions
2. Verify the P&L log looks correct — entry/exit prices match what you'd expect
3. Check that HF defense, TP, SL, and signal reversal exits all appear in the log
4. Review the win rate and avg trade P&L — does the signal have a meaningful edge?
5. When satisfied, change `paper_trading: false` in `config.yml`

---

## Wallet security

This skill executes transactions autonomously and unattended. Key management matters more here than in interactive use.

- **Use a dedicated bot wallet** — never run this strategy from your main wallet. Create a separate address used only for this bot. If a signal goes wrong or a bug causes an unexpected trade, the damage is limited to that wallet.
- **Never put your private key in any file in this repo** — not in `config.yml`, not in `mcp-config.json`, not anywhere on disk in plaintext. Your private key belongs only in OpenClaw's secure key store.
- **Minimum funding principle** — only bridge what you need: enough collateral for your `base_position_pct` position size, plus a small ETH buffer for gas (~$2–5 on Base). Do not park savings in the bot wallet.
- **`user_address` is a public address** — safe to store in `config.yml` and MCP headers. It is not a secret.
- **Revoke approvals after closing** — the `prepare_open` flow grants `uint256 max` ERC20 approval to the router. After fully closing a position, revoke it using Revoke.cash on Base (`https://revoke.cash`). The autonomous flow does not do this automatically.
- **Monitor the wallet** — set up a balance alert (e.g. via Etherscan or a simple on-chain monitor) so you know if the bot wallet is unexpectedly drained.

---

## Safety and hard limits

The following limits cannot be overridden by `config.yml`:

- **Never open with leverage > 4x** — the Aave server also enforces this at 1.1 HF floor
- **Never open if projected HF < 1.2** — verified via `prepare_open` response
- **Never more than one open position at a time** — checked against `trades.jsonl`
- **On any unhandled error or ambiguous state: log and exit without acting** —
  fail safe, not fail open. Never leave the strategy in a partial state.
- **Max one open and one close per cycle** — limits blast radius from bad signals

**Verified contracts (Base mainnet) — referenced from aave-leverage skill:**

| Contract | Address |
|----------|---------|
| LeverageRouterV4 | `0x4A60C1E7d78DA2A61007fE21d282a859D3906724` |
| LeverageVaultV4 | `0xf2A51d441E6bA96c37fD0024115DccF03764478f` |
| Aave v3 Pool | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` |

Always verify `step.contract` against these addresses before executing any live transaction.
The base `aave-leverage` skill's safety rules apply here too — never sign a step if the
contract is not one of the above or a known token address.
