# aave-leverage-strategy — OpenClaw Skill

> Autonomous trend-following strategy for Aave v3 leverage on Base.
> Paper trading by default. Persistent P&L log. No ML, no black boxes.

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
      "headers": {
        "X-Wallet-Address": "0xYOUR_WALLET_ADDRESS"
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
3. Compute the OHLCV EMA+RSI signal (Coinbase → Kraken fallback; CoinGecko 3-timeframe as last resort) and apply 9 no-trade filters
4. Decide whether to open, hold, or close
5. Write a cycle entry to `trades.jsonl`
6. Print the P&L summary

On the first run with `paper_trading: true`, no transactions will be submitted.

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
│   ├── config.py         # Config dataclass
│   ├── market.py         # Market data fetcher (7 sources)
│   ├── ohlcv.py          # OHLCV signal engine — EMA+RSI (Coinbase → Kraken)
│   ├── onchain.py        # Aave v3 Base on-chain reads (utilization, liquidations)
│   ├── signal.py         # CoinGecko 3-timeframe signal (last-resort fallback)
│   ├── filters.py        # 9 no-trade filters
│   ├── sizing.py         # Position sizing + increase delta
│   ├── executor.py       # Trade execution (open/close/increase/reduce)
│   ├── state.py          # trades.jsonl read/write + effective size helpers
│   └── pnl.py            # P&L computation
├── tests/                # Unit tests
└── scripts/
    └── buy_session.py    # Purchase MCP session token
```

---

## License

MIT
