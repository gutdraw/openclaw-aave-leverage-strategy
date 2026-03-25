# Changelog

## [1.1.0] — 2026-03-25

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
