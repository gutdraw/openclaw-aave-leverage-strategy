# Changelog

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
