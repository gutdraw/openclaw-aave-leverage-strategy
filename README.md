# aave-leverage-strategy — OpenClaw Skill

> Autonomous trend-following strategy for Aave v3 leverage on Base.
> Paper trading by default. Persistent P&L log.

Two-layer design:
- **Layer 1 — Bot**: deterministic Python loop (no AI). Runs every 30 minutes, scores
  multi-timeframe EMA+RSI+OBV+MACD signals, executes trades via the `aave-leverage` MCP
  tool, appends structured JSON to `trades.jsonl`. Cost: $4/month flat MCP session via
  x402 — cheaper than a single LLM inference call at most model tiers.
- **Layer 2 — Agent**: any LLM (Claude, GPT, Hermes) reads `trades.jsonl` periodically,
  diagnoses anomalies, tunes parameters, and proposes improvements. The agent never
  touches the execution layer — reads logs, suggests code/config changes, human approves.

This split keeps execution reliable (no LLM latency on the hot path) and lets you run
any model as the reviewer without per-cycle inference cost dragging on P&L.

---

## Requirements

- [aave-leverage](https://github.com/gutdraw/openclaw-aave-leverage) skill installed and active
- OpenClaw with MCP + cron support
- Node.js >= 18 (for quote verification)
- USDC on Base for x402 session payment — recommend a monthly session ($4.00) for unattended cron use

---

## Installation

### 1. Install the base skill

Follow the setup instructions in `aave-leverage` first. That skill must be active
before this one can run — it provides all the on-chain execution tools.

### 2. Add the MCP server

Copy `mcp-config.json` into your `openclaw.json`:

```json
{
  "mcpServers": {
    "aave-leverage": {
      "url": "https://aave-leverage-agent-production.up.railway.app/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer YOUR_SESSION_TOKEN"
      }
    }
  }
}
```

### 3. Copy and edit config

```bash
cp config.yml.example config.yml   # or just edit config.yml directly
```

At minimum, set:
```yaml
user_address: "0xYOUR_WALLET_ADDRESS"
asset: "WETH"           # or cbBTC / wstETH
position_id: "WETH/USDC"
paper_trading: true     # keep true until you've validated
```

`config.yml` is gitignored — it contains your wallet address.

### 4. Add the skill to OpenClaw

Copy `SKILL.md` into your OpenClaw skills directory or submit to ClawHub.

---

## First run

Tell OpenClaw to run the strategy:

```
run the aave-leverage-strategy for one cycle
```

OpenClaw will:
1. Read `config.yml`
2. Fetch market data — CoinGecko prices + volume, Aave MCP position, BTC dominance, perp funding rate, Fear & Greed index, Base on-chain Aave state, hourly OHLCV candles
3. Compute the OHLCV signal — 3-timeframe EMA (1h/6h/1d) + RSI + OBV + MACD divergence gate (Coinbase → Kraken fallback; CoinGecko 3-timeframe as last resort) — and apply 9 no-trade filters
4. Decide whether to open, hold, or close
5. Write a cycle entry to `trades.jsonl`
6. Print the P&L summary

On the first run with `paper_trading: true`, no transactions will be submitted.

Funding rates use the providers listed in `funding_sources`, in order. The default
is `okx`, then `binance`, then `bybit`, because Binance and Bybit can reject US
source IPs. Each cycle records the selected provider, attempted providers, and
bounded provider failures in `trades.jsonl`.

---

## Scheduled runs (cron)

To run automatically every 15 minutes in OpenClaw:

```
/cron 15m run the aave-leverage-strategy for one cycle
```

The strategy is stateless per run — it reads `trades.jsonl` for state and the chain
for position data. Safe to run on any schedule.

---

## Reading the P&L log

```bash
# All entries
cat trades.jsonl | jq '.'

# Trade entries only (opens and closes)
cat trades.jsonl | jq 'select(.type=="trade")'

# All cycle decisions
cat trades.jsonl | jq 'select(.type=="cycle") | {ts, trend_score, decision, reason}'

# Closed trades with P&L
cat trades.jsonl | jq 'select(.type=="trade" and .action=="close") | {ts, exit_reason, pnl_pct, net_pnl_usd}'

# Total net P&L
cat trades.jsonl | jq '[select(.type=="trade" and .action=="close") | .net_pnl_usd] | add'

# Win rate
cat trades.jsonl | jq '[select(.type=="trade" and .action=="close")] | {total: length, wins: [.[] | select(.net_pnl_usd > 0)] | length}'
```

---

## Going live

After validating with paper trading:

1. Review your `trades.jsonl` — check win rate, avg P&L, and that all exit types appear
2. Confirm you have enough WETH/cbBTC/USDC on Base to fund positions
3. Confirm you have enough USDC on Base for the x402 MCP session ($4.00/month recommended)
4. Edit `config.yml`:
   ```yaml
   paper_trading: false
   ```
5. Run one cycle and verify a real `get_position` call shows the expected open position

There is no separate "live mode" — changing `paper_trading: false` is the only switch.
All other logic is identical.

---

## Repo structure

```
openclaw-aave-leverage-strategy/
├── SKILL.md              # OpenClaw skill definition (strategy spec)
├── SETUP.md              # Setup and config reference
├── CHANGELOG.md          # Version history
├── config.example.yml    # Config template (copy to my-config.yml)
├── config.yml            # Your config (gitignored)
├── trades.jsonl          # Trade log, created at runtime (gitignored)
├── bot/
│   ├── main.py           # Entry point — per-cycle execution loop
│   ├── journal.py        # SQLite transaction journal and crash recovery state
│   ├── heartbeat.py      # Atomic supervisor heartbeat writer
│   ├── provenance.py     # Process, code, and config identity metadata
│   ├── audit.py          # Read-only execution/accounting audit helpers
│   ├── config.py         # Config dataclass
│   ├── market.py         # Market data fetcher (7 sources)
│   ├── ohlcv.py          # OHLCV signal engine — 3-TF EMA + RSI + OBV + MACD (Coinbase → Kraken)
│   ├── onchain.py        # Aave v3 Base on-chain reads (utilization, liquidations)
│   ├── risk_probe.py     # Independent atomic Aave account-risk snapshots
│   ├── signal.py         # CoinGecko 3-timeframe signal (last-resort fallback)
│   ├── filters.py        # 9 no-trade filters
│   ├── sizing.py         # Position sizing + increase delta
│   ├── executor.py       # Trade execution (open/close/increase/reduce)
│   ├── state.py          # trades.jsonl read/write + effective size helpers
│   ├── pnl.py            # P&L computation
│   └── backtest.py       # Legacy studies plus faithful live-snapshot replay
├── tests/                # Unit tests
└── scripts/
    ├── buy_session.py    # Purchase MCP session token
    ├── audit_history.py  # Offline/RPC-enriched read-only audit report
    └── check_health.py   # Heartbeat/journal/risk snapshot check
```

The live loop writes `trades.jsonl` as an audit export and uses a sibling SQLite
execution journal for crash recovery. New swaps fail closed unless the MCP
response includes a fresh quoted minimum output; the current legacy router shape
also receives a client-side freshness deadline before signing. See `deploy/` for
the systemd service and health-check timer templates. The timer records active
and resolved service, journal, safety-hold, and health-factor alerts in
`trades.alerts.json` and emits transitions to the systemd journal. A future MCP upgrade can
add an on-chain router deadline without changing the bot policy.

The health timer also performs an independent read-only Aave account-risk probe
every five minutes. It writes an atomic `trades.risk.json` snapshot, warns below
health factor 1.14, escalates at 1.12, and alerts when the snapshot is unavailable
or older than ten minutes. This probe has no signer and cannot submit trades.

For historical analysis, `bot.backtest.run()` preserves the legacy parameter
study behavior. `bot.backtest.run_live()` replays the recorded live signal and
shared filter pipeline, and reports incomplete snapshots instead of treating
missing live inputs as equivalent to a clean backtest.

For an evidence-first operational audit, run:

```bash
python scripts/audit_history.py --trades trades.jsonl --journal trades.sqlite3
```

The default report is offline and read-only. It classifies every recorded cycle,
joins trade events to the SQLite execution journal where possible, checks the
modelled P&L formula, and reports missing evidence. Add `--config my-config.yml`
to perform optional read-only Base-RPC receipt and ERC-20 transfer enrichment;
the audit never creates a signer, renews an MCP session, or broadcasts a
transaction. `reconciled_realised_usd` intentionally remains null until actual
fills, gas, interest, fees, and wallet flows are evidenced.

`bot.backtest.walk_forward()` provides fixed-parameter rolling out-of-sample
windows with the explicit fee, gas, and borrow-cost assumptions from
`BacktestParams`. Those costs are estimates and must not be read as wallet P&L.

---

## License

MIT
