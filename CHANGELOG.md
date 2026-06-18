# Changelog

## [1.6.0] — 2026-06-18

### Changed — Fear & Greed short gate raised from 15 → 25

- **`min_fear_greed_short` raised to 25** (`config.py`, `config.example.yml`): the gate
  that blocks short entries in extreme fear was set at F&G ≤ 15. Backtesting 4,100+
  live cycles showed the gate fired 58 times at the old threshold and was wrong 53.6%
  of the time — blocking legitimate short entries more often than protecting against
  panic bounces. At F&G ≤ 25 the gate achieves 62.2% accuracy on blocked signals,
  making it actually defensive rather than a net negative. The RSI floor override
  (RSI ≥ 35 lifts the block) is unchanged.
- **Research basis**: AI subagent backtest across March 28 – June 18, 2026 data;
  tested 6 threshold configurations (F&G ≤ 10, 15, 20, 25, disabled, wider RSI floor).
  F&G ≤ 25 with RSI < 35 was the best-performing gate configuration. Disabling entirely
  was the primary recommendation but F&G ≤ 25 was retained as a conservative guard
  against genuine panic-bottom scenarios given the small live sample size (36 trades).

## [1.5.0] — 2026-05-20

### Added — OBV + MACD divergence gate on tech signal

- **On-Balance Volume (OBV)** (`ohlcv.py`): computed from 1h candle volume data already
  fetched from Coinbase/Kraken. OBV accumulates volume on up-closes and subtracts on
  down-closes; `obv_bull` = EMA(12) > EMA(26) of OBV series.
- **MACD histogram** (`ohlcv.py`): MACD line (EMA12 − EMA26) minus signal line (EMA9 of
  MACD). Positive histogram = momentum building; negative = fading.
- **Divergence gate on strong signals**: when BOTH OBV and MACD contradict the EMA
  direction, strong signals are downgraded to moderate (score 4→3 for longs, 0→1 for
  shorts). Single-indicator disagreement is ignored — both must diverge to trigger.
  This prevents strong_long calls at momentum tops (e.g. price up but volume drying out
  and MACD histogram already rolling over).
- **Cycle log fields** (`main.py`): `tech_obv_bull` and `tech_macd_bull` added to every
  cycle entry in `trades.jsonl` for auditability.
- **Fetchers now return volumes** (`ohlcv.py`): `_fetch_coinbase` and `_fetch_kraken`
  now return `(closes, volumes)` tuples. Volume was always in the candle payload
  (index 5 for Coinbase, index 6 for Kraken) — previously discarded.

## [1.4.0] — 2026-04-25

### Added — Trailing stop exit

- **`trailing_stop_pct` config** (`config.py`, `main.py`): closes the position when
  price pulls back more than `trailing_stop_pct` % from the peak observed since open
  (or since the last increase). Default `2.5`. `0` = disabled.
- **Peak tracking** (`state.py`, `main.py`): each cycle updates a `price_peak` field in
  `trades.jsonl` state. For longs: tracks highest price since open. For shorts: tracks
  lowest price since open. The trailing stop fires when the move from peak exceeds the
  threshold in the adverse direction.
- **Post-trailing-stop gate** (`main.py`, `config.py`): after a trailing-stop close,
  bot blocks re-entry for `post_trailing_stop_hours` (default 48h). A `strong` signal
  can bypass the gate early. This prevents immediately re-entering the same chop that
  triggered the stop.
- **`skip_post_trailing_stop` decision** logged to `trades.jsonl` cycles while gate
  is active.

### Fixed

- **Trailing stop direction bug** (`main.py`): peak tracking was using raw price for
  both directions; for shorts the "peak" should be the lowest price (most profitable
  point). Fixed to track `min_price` for shorts and `max_price` for longs.
- **Short borrow units** (`sizing.py`, `main.py`): `borrow` for shorts was being stored
  in cbBTC units instead of USDC — causing wrong weighted-average entry price on
  `increase` trades. Fixed to store `borrow = seed_usd * (leverage - 1)`.
- **Long borrow units** (`sizing.py`): symmetric fix — long borrow stored as
  `seed_usd * (leverage - 1)` in USDC, not in asset units.
- **`target_leverage` parameter rename** (`mcp_client.py`): `prepare_increase` was
  sending `leverage` but MCP server expects `target_leverage`. Fixed.
- **MCPClient session reuse** (`main.py`): `MCPClient` was being instantiated fresh
  every cycle from `cfg.mcp_session_token`. When the client auto-renewed its session
  token, the updated token was saved on the instance but `cfg` was never updated, so
  the next cycle started a new client with the old expired token — triggering another
  $4 renewal. Fixed by creating MCPClient once in `main()` and passing it into
  `run_cycle`.

## [1.3.0] — 2026-04-21

### Added — Multi-timeframe OHLCV signal

- **Three-timeframe EMA scoring** (`ohlcv.py`): upgraded from single-timeframe (1h
  only) to three timeframes: 1h, intermediate (~6h via Coinbase 21600s / Kraken 240m),
  and 1d. Higher TFs must agree — disagreement between 1d and mid returns `hold`
  regardless of 1h. 1h + RSI only determine whether the signal is moderate or strong.
- **Prevents 1h whipsaws**: a 1h wick against the trend while 1d and mid are still
  bullish now resolves to `hold` rather than a reversal signal.
- **Scoring table updated** (score 0–4 unchanged in label mapping):
  - 4 = strong_long: 1d bull + mid bull + 1h bull + RSI bullish
  - 3 = moderate_long: 1d bull + mid bull (1h unconfirmed)
  - 2 = hold: 1d and mid disagree
  - 1 = moderate_short: 1d bear + mid bear (1h unconfirmed)
  - 0 = strong_short: 1d bear + mid bear + 1h bear + RSI bearish
- **Cycle log fields**: `tech_mid_bull` and `tech_1d_bull` added to `trades.jsonl`.

### Added — Liquidity escape

- **Flash-loan pool utilization tracking** (`state.py`, `main.py`): the bot now tracks
  Aave USDC and asset pool utilization across cycles. If the utilization trend indicates
  the pool is drying up (approaching the interest-rate kink), an open position is closed
  preemptively to avoid being unable to exit via flash-loan later.
- **`close_reason: liquidity_escape`** logged when this path triggers.

### Fixed

- **On-chain reconciliation after open** (`main.py`, `state.py`): after a live
  `openPosition` tx confirms, the bot now reads the actual on-chain supply/borrow from
  the MCP position response and overwrites the locally-computed values in `trades.jsonl`.
  Prevents P&L and HF drift caused by Uniswap slippage and fee rounding at open time.
  Includes a 3-retry loop to handle RPC indexing lag.
- **Always check wallet token before short** (`main.py`): `_ensure_wallet_token` was
  skipped when wallet already held some USDC. Fixed to always run the swap-to-correct-
  token check before opening a short (all cbBTC must be converted to USDC first).
- **Short carry APR formula** (`main.py`): carry = `usdc_supply_apy * lev − asset_borrow_apy * (lev − 1)`.
  Previous formula used `lev + 1` for the supply side (wrong for shorts, where supply
  = `lev × seed` not `(lev+1) × seed`). Now matches Aave dashboard display.
- **cbBTC approve gas limit** (`mcp_client.py`, `signer.py`): cbBTC's approve function
  is heavier than standard ERC20 (proxy contract). Hard floor raised to 100k gas to
  prevent out-of-gas reverts on the approve step.
- **Swap all cbBTC → USDC on short entry** (`main.py`): previously only swapped enough
  cbBTC to cover the seed, leaving the remainder as cbBTC in the wallet (unhedged BTC
  exposure). Now swaps the full cbBTC balance before entering a short.

## [1.2.2] — 2026-04-01

### Fixed — Live trading bugs (first live cbBTC long)

- **Direction-specific TP/SL wired** (`main.py`): `compute_unrealised` was using shared
  `take_profit_pct` / `stop_loss_pct` instead of the direction-specific `tp_for()` /
  `sl_for()` helpers added in 1.2.1. Now correctly uses per-direction overrides.
- **Double swap on long open removed** (`executor.py`): `open_position` and
  `increase_position` were calling `mcp.swap(USDC→asset)` internally, duplicating the
  pre-swap already done by `_ensure_wallet_token` in `main.py`. Removed from executor —
  `_ensure_wallet_token` is the single swap path for live longs.
- **Swap tx revert detection** (`signer.py`): `wait_for_receipt` now raises
  `RuntimeError` when the mined tx has `status == 0`. Previously it returned silently on
  reverts, allowing subsequent steps to proceed with a broken state.
- **Wallet shortfall logic** (`main.py._ensure_wallet_token`): previous logic required
  either cbBTC or USDC alone to cover the full seed. Fixed to compute the shortfall
  (`supply_needed_usd − existing_asset_usd`) and swap only that delta from USDC —
  avoids skipping valid opens when wallet holds partial asset + partial USDC.
- **RPC propagation delay after swap** (`main.py`): added `time.sleep(3)` after
  `wait_for_receipt` for pre-open swaps so the MCP server's RPC node sees the confirmed
  balance before `prepare_open` runs its balance pre-check.
- **`size.supply` cap to wallet balance** (`main.py`): when `_ensure_wallet_token`
  returns True (no swap needed), CoinGecko price vs Uniswap execution price can differ
  by a tiny fraction, causing `size.supply` to exceed the actual wallet balance by
  ~0.000002 cbBTC. Cap `size.supply` to `actual_wallet_balance` before calling
  `prepare_open` to prevent the MCP balance check from rejecting the order.
- **`approveDelegation` skip check fixed** (`signer.py`): `_should_skip_approval` was
  calling ERC20 `allowance(address,address)` on Aave v3 variable debt tokens, which
  revert with `ContractCustomError 0x29a270f5` because they implement
  `borrowAllowance(fromUser,toUser)` instead. Fixed to use the correct function per step
  type. Added `try/except` so a failed check falls through to sending the tx.
- **Base sequencer 3s pause after approvals** (`signer.py`): added `time.sleep(3)` after
  each mined `approve` / `approveDelegation` tx. Base enforces a 1-in-flight-tx limit
  for delegated accounts — without the pause, the main `openPosition` tx is rejected
  with `in-flight transaction limit reached`.

## [1.2.1] — 2026-03-29

### Added — Direction-specific leverage
- **`long_leverage` / `short_leverage` config fields** (`config.py`, `sizing.py`): Override
  `leverage` for each direction independently. `0` means "use the shared `leverage` value."
  `short_leverage` is still capped at `short_max_leverage` (hard safety limit). Useful for
  running higher conviction on longs (e.g. 3x) while capping shorts at 2x for HF safety.
- **`cfg.leverage_for(direction)`** (`config.py`): Helper method used by `sizing.py` and
  `main.py` wherever direction-aware leverage is needed — cycle log `short_carry_apr`,
  trade entry `leverage` field, both `compute()` and `compute_increase()` in `sizing.py`.

### Fixed — Post-TP reopen consistency
- **`skip_post_tp` decision** (`main.py`, `state.py`): After a take-profit close, the bot
  now gates same-direction reopening on the same signal strength that would have suppressed
  the TP. Previously, a TP could fire on a moderate signal and the bot would immediately
  reopen in the same direction — inconsistent with `tp_on_strong_signal=false` intent.
  Now: after a TP close, a same-direction reopen requires `score==3` (strong_long) for
  longs or `score==0` (strong_short) for shorts. Only applies when
  `tp_on_strong_signal=false` (default). Gate is a no-op when `tp_on_strong_signal=true`.
- **`state.get_last_close()`** (`state.py`): New helper — returns the most recent
  `action=close` trade entry. Used by the post-TP gate in `main.py`.

## [1.2.0] — 2026-03-29

### Added — Live transaction signing (signer.py + MCP server normalisation)
- **Unified MCP response format** (`aave-leverage-agent/api/src/routes/mcp.py`): all
  seven prepare_* and swap tools now return a single consistent shape:
  `{"transaction_steps": [{contract, abi_fn, args, gas, title, ...}]}`.
  Previously `prepare_open` / `prepare_close` returned a bespoke `calldata` dict
  requiring client-side ABI re-encoding; `prepare_reduce` / `prepare_increase` used bare
  function names; `swap` passed struct args as a dict. Now every step carries a full ABI
  signature and flat positional args — no tool-specific logic needed in the signer.
- **Full ABI signatures in all steps** (`mcp.py`): `abi_fn` fields upgraded from bare
  names (`"approve"`, `"reduceLeverage"`) to complete signatures
  (`"approve(address,uint256)"`,
  `"reduceLeverage(address,address,address,address,uint256,uint24,uint256,bytes)"`, etc.).
  Signer encodes directly from the signature — no lookup table required.
- **Uniswap struct args as ordered lists** (`mcp.py`): `exactInputSingle` and
  `exactInput` args changed from `[{"tokenIn":...}]` dict to
  `[[tokenIn, tokenOut, fee, ...]]` list matching the ABI tuple layout. Signer handles
  list-as-tuple natively via the recursive `_coerce_arg` type walker.
- **Simplified signer** (`signer.py` rewrite): single `transaction_steps` execution
  path replaces the previous four-branch dispatcher. Removed: `_expand_calldata_response`,
  `_WELL_KNOWN_SIGS`, `_UNISWAP_STRUCT_FIELDS`, `_CLOSE_SIG`, `_OPEN_SIG`. Added:
  `_coerce_arg` — a recursive Solidity-type-aware coercion function that correctly
  handles `address`, `uint*/int*`, `bytes/bytesN`, and nested tuple `(T1,T2,...)` types.
- **Allowance-skip moved to per-step check** (`signer._should_skip_approval`): replaces
  the old inject-then-skip pattern inside `_expand_calldata_response`. Now runs for every
  `approve` / `approveDelegation` step regardless of which tool produced it — avoids Base
  sequencer's "in-flight transaction limit for delegated accounts" error universally.
- **Nested-tuple-aware type parser** (`signer._split_sig_types`): replaces naive
  `types_str.split(",")` which shredded tuple types like
  `(address,address,uint24,...)` into fragments. Depth-tracking parser keeps each tuple
  type intact as a single element in the types list.
- **On-chain allowance check** (`signer._erc20_allowance`): direct `eth_call` to
  `allowance(address,address)` — no ABI file required.
- **Internal nonce tracker** (`signer._next_nonce`): initialised once from
  `get_transaction_count("pending")`, incremented locally per step. Avoids RPC race
  conditions between steps in a single cycle. `reset_nonce()` clears on error.
- **EIP-1559 fee bump** (`signer.sign_and_send`): priority tip 1 gwei — replaces any
  stuck pending txs from previous runs.
- **Wallet balance fallback for position sizing** (`market.py`, `main.py`): when Aave
  `totalCollateralUSD` is zero (position closed, wallet is flat), effective collateral
  falls back to `wallet_collateral_usd` (USDC balance + asset balance × price). Allows
  the bot to open the next position autonomously without manual USDC transfer.
- **Pre-open token swap** (`main.py._ensure_wallet_token`): before opening a new live
  position, checks whether the wallet holds the correct token side (USDC for shorts,
  supply-asset for longs). If not, swaps via `mcp.swap()` at 0.2% slippage buffer.
  Enables fully autonomous long/short cycling.

### Fixed
- **KeyError on MCP response keys**: executor.py previously called
  `signer.sign_and_send(resp["transaction"])` — fails when MCP returns
  `transaction_steps`. All calls replaced with `signer.execute_steps(resp)`.
- **Stale log wording**: step log changed from "mined" to "sent" — the log fires before
  `wait_for_receipt`, so "mined" was misleading.

## [1.1.2] — 2026-03-27

### Fixed
- **Short position sizing** (`sizing.py`): `borrow` was calculated as `(lev-1)×seed/price`
  (1× seed at 2x leverage), giving only 1× price exposure in paper P&L. The MCP
  flash-loan loop actually creates `supply=(lev+1)×seed` USDC and `borrow=lev×seed`
  asset on-chain. Fixed to `borrow = seed × lev / price` — 2× seed at 2x leverage.
- **Paper health factor** (`main.py`): `_paper_health_factor` used `leverage×supply×lt`
  for shorts, under-stating the true Aave collateral (which is `(lev+1)×seed`). Fixed
  to `(leverage+1)×supply×lt` — now returns HF ≈ 1.17 at 2x short open (matches Aave).
- **Short carry APR** (`main.py`): carry formula `supply_apy×lev − borrow_apy×(lev−1)`
  was wrong for the same reason. Fixed to `supply_apy×(lev+1) − borrow_apy×lev`.
  At 2x with current rates: 5.71% (was 4.08%).
- **Signal reversal fires on `hold`** (`main.py`): `hold` has score=0, same as
  `strong_short`. The condition `sig.score <= signal_reversal_min_score` (default 1)
  was triggering reversal exits on `hold` signals. Fixed by adding `sig.direction ==
  "short"` check — `hold` (direction=none) no longer triggers reversal.
- **Exit ordering** (`main.py`): TP/SL (price-based, deterministic) now runs before
  signal reversal (signal-based). Previously a signal reversal could preempt an SL
  that should have fired at the same price.
- **F&G short filter too aggressive** (`filters.py`, `config.py`): Filter 5 blocked
  shorts whenever F&G ≤ 15 (extreme fear), even in sustained downtrends where RSI had
  recovered from oversold. Added `fear_greed_short_rsi_floor` gate: the block lifts once
  RSI climbs above this value (default 35), indicating the oversold bounce is done.
- **Short carry APY fields** (`market.py`): Added `usdc_supply_apy` and
  `asset_borrow_apy` to `MarketData` and cycle log — raw rates from `get_position`
  reserveRates response.
- **pnl.py docstring**: Updated to reflect correct borrow definition (`lev×seed/price`).
- **config.example.yml**: `signal_reversal_min_score` default corrected to `1`
  (moderate+strong reversal); added `fear_greed_short_rsi_floor: 35.0`.

## [1.1.1] — 2026-03-25

### Fixed
- **RSI overbought scoring** (`ohlcv.py`): EMA-bull + RSI > 75 (overbought) incorrectly
  scored as `moderate_long` instead of `hold`. Added explicit `overbought`/`oversold`
  guards — those edge cases now correctly resolve to hold.
- **Position size truthiness bug** (`main.py`): `eff_supply or float(...)` evaluated
  `False` when effective size was 0.0, producing wrong P&L and HF values. Replaced with
  explicit `> 0` comparisons throughout.
- **Concurrent write corruption** (`state.py`): `append_entry` now acquires an exclusive
  `fcntl.flock` before writing, preventing interleaved output under concurrent processes.
- **Malformed log line handling** (`state.py`): `load_entries` now skips malformed JSON
  lines with a warning instead of crashing the cycle.
- **Deferred import** (`onchain.py`): moved `import requests` from inside the function
  body to top-level; replaced with `httpx` for consistency.
- **Config example default** (`config.example.yml`): `max_recent_liquidations` corrected
  from `10` to `3` to match the `config.py` default.

## [1.1.0] — 2026-03-25

### Added — OHLCV signal (primary signal engine)
- **EMA crossover + RSI on hourly candles**: Coinbase Exchange public API
  (`api.exchange.coinbase.com`) → Kraken fallback. EMA(12/26) crossover gives trend
  direction; RSI(14) gives momentum zone. Scores 0–4 map to the same labels as the
  CoinGecko 3-timeframe engine (`strong_long` / `moderate_long` / `hold` /
  `moderate_short` / `strong_short`).
- **Signal hierarchy changed**: OHLCV is now the primary signal. CoinGecko 3-timeframe
  is used only as a last-resort fallback when both Coinbase and Kraken are unavailable.
- Cycle entry now includes `tech_signal`, `tech_ema_bull`, `tech_rsi`, `tech_source`,
  and `cg_signal` fields for full auditability.

### Added — Position increase (moderate → strong signal upgrade)
- When a `moderate_long` or `moderate_short` position is open and the signal upgrades
  to `strong_long` / `strong_short`, the bot tops up the half-size position to full
  size instead of doing nothing. Only one increase per trade is allowed.
- `compute_increase()` added to `sizing.py` — computes the delta between current seed
  and the full-strength target.
- `increase_position()` added to `executor.py` — paper stub logs; live mode calls
  `prepare_increase` on the MCP server.
- `get_effective_size()` and `has_been_increased()` added to `state.py` — read increase
  entries from `trades.jsonl` to compute accurate effective supply/borrow for P&L and HF.
- `prepare_increase()` added to `mcp_client.py`.

### Added — Exit strategy
- **Signal reversal exit**: close position when all 3 timeframes flip against it
  (`signal_reversal_exit`, `signal_reversal_min_score: 0`)
- **Minimum hold time**: prevent whipsaw closes within `min_hold_hours` of opening
- **Time-based exit**: close after `max_hold_days` to prevent carry drag and HF drift
- **TP suppression on strong signal**: when `tp_on_strong_signal: false` (default),
  take-profit is skipped if the signal is still at maximum strength — let winners ride.
  Stop-loss always applies regardless.

### Added — Data sources
- **Binance/Bybit/OKX funding rate**: perp funding rate in % per 8h. Chain tries
  Binance → Bybit → OKX (Binance/Bybit geo-blocked on US IPs). Soft source — never
  blocks the cycle on failure.
- **Fear & Greed Index** (alternative.me): 0–100 sentiment score. Soft source.
- **CoinGecko 24h volume**: already fetched, now extracted and logged each cycle.

### Added — On-chain Aave v3 Base data (via Alchemy or public RPC)
- **USDC pool utilization**: reads `varDebtToken.totalSupply() / aToken.totalSupply()`
  directly from Base. Suppresses new entries when > `max_usdc_utilization` (default 92%)
  — Aave's interest rate curve kinks sharply at ~90%.
- **Recent liquidation count**: `eth_getLogs` for `LiquidationCall` events on Aave v3
  Pool in last `onchain_lookback_blocks` blocks (~20s with free Alchemy tier, ~5 min
  with PAYG). Suppresses entries during cascades.
- Configurable `rpc_url` (default: `https://mainnet.base.org`; Alchemy for getLogs).
- Configurable `onchain_lookback_blocks` (default 10; set 150 with Alchemy PAYG).

### Added — No-trade filters
- **Filter 4**: Funding rate — suppresses longs if funding > `max_funding_rate_long`
  (crowded longs) or shorts if funding < `-max_funding_rate_short` (crowded shorts).
- **Filter 5**: Fear & Greed — suppresses longs if F&G ≥ `max_fear_greed_long` (extreme
  greed) or shorts if F&G ≤ `min_fear_greed_short` (extreme fear / capitulation).
- **Filter 6**: Volume — suppresses entries if 24h volume < `min_volume_24h_usd` (disabled
  by default; set a threshold once you have baseline volume data).
- **Filter 7**: USDC utilization — suppresses entries if > `max_usdc_utilization`.
- **Filter 8**: Liquidation cascade — suppresses entries if recent liquidations >
  `max_recent_liquidations` within the lookback window.

### Added — Short position support (HF-aware)
- Short-specific HF thresholds: `short_hf_defense_reduce`, `short_hf_defense_close`,
  `short_min_open_hf` — all must be below 1.17 (2x short open HF).
- `short_max_leverage` hard cap (default 2.0) enforced in sizing.py regardless of
  `leverage` config.

### Fixed
- **Paper trading HF simulation**: paper mode no longer reads real on-chain HF.
  Instead computes simulated HF from paper position parameters using Aave v3 Base
  liquidation thresholds (WETH=0.83, cbBTC=0.78, USDC=0.78). Prevents paper bot
  from triggering hf_close based on unrelated real wallet positions.
- **Signal zero-change neutrality**: all-zero price changes (flat market / missing data)
  now correctly return `hold` with multiplier=0.0 instead of `strong_short`.
- **Market price guard**: explicit check that price is non-None after 2-of-3 source
  quorum — prevents sizing with stale data if CoinGecko fails but quorum still passes.
- **OKX funding rate fallback**: Binance returns HTTP 451 and Bybit HTTP 403 on AWS
  US IPs; OKX is accessible globally without auth.

## [1.0.0] — 2026-03-24

### Added
- Full autonomous trading bot (paper and live modes)
- 3-timeframe trend signal engine (1h/24h/7d CoinGecko)
- No-trade filters: volatility spike, borrow APR, BTC dominance, position overlap
- Position sizing: seed_usd = collateral * base_pct * signal_multiplier
- Health-factor defense: reduce at HF < 1.35, force-close at HF < 1.20
- Take-profit and stop-loss exits
- Append-only JSONL trade log (trades.jsonl)
- Paper trading mode (default on)
- Live mode via web3.py + eth_account signer
- Dockerfile for containerized cron deployment
- Unit tests: signal, filters, sizing, pnl, state
