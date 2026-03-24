# Setup Guide — Aave Leverage Strategy Bot

## Prerequisites

- Python 3.12+
- An MCP session token (from the `aave-leverage-agent` MCP server)
- A dedicated bot wallet on Base (never use your main wallet)
- USDC on Base for the MCP session fee ($0.25/day or $1.50/week)

## 1. Install dependencies

```bash
cd openclaw-aave-leverage-strategy
pip install -r requirements.txt
```

## 2. Configure

Copy and edit the config file:

```bash
cp config.yml my-config.yml
```

Set the required fields:

```yaml
user_address: "0xYOUR_BOT_WALLET"     # your dedicated bot wallet
mcp_session_token: "tok_..."           # from POST /mcp/auth
asset: "WETH"                          # WETH | cbBTC | wstETH
position_id: "WETH/USDC"              # matches asset
paper_trading: true                    # start in paper mode
```

## 3. Get an MCP session token

The MCP server charges a small USDC fee on Base for API access. The `buy_session.py`
script handles the full payment flow (EIP-3009 signature → on-chain settlement → token).

**Requirements:** your bot wallet needs USDC on Base.
- Bridge from Ethereum: https://bridge.base.org
- Buy directly on Base via Coinbase or any Base DEX

```bash
# Set your private key as an env var (never paste it into commands)
export PRIVATE_KEY=0xYOUR_PRIVATE_KEY

# Purchase a 1-week session ($1.50 USDC) and write token directly into config
python3.12 scripts/buy_session.py \
  --wallet 0xYOUR_BOT_WALLET \
  --duration week \
  --config my-config.yml
```

The script will print the token and write it to `my-config.yml` automatically.

**Duration options:**

| Duration | Price | Best for |
|---|---|---|
| `hour` | $0.05 | Quick test |
| `day` | $0.25 | Daily use |
| `week` | $1.50 | Running bots |
| `month` | $4.00 | Production |

Tokens are wallet-bound and stateless — renew before expiry by re-running the script.

## 4. Run in paper mode (recommended first)

Single cycle:
```bash
python -m bot.main --config my-config.yml
```

Continuous loop (every hour):
```bash
python -m bot.main --config my-config.yml --loop 3600
```

Watch the trade log:
```bash
tail -f trades.jsonl | python -m json.tool
```

## 5. Validate with 20+ paper cycles

Run at least 20 cycles in paper mode before going live. Check:
- `decision` field in each cycle entry makes sense
- `entry_price`, `supply`, `borrow` are reasonable
- `unrealised_pct` tracks live price correctly
- No unexpected errors in the log

## 6. Switch to live mode

**Use a dedicated wallet. Never use your main wallet.**

```yaml
paper_trading: false
```

Set your private key (prefer env var over config file):

```bash
export PRIVATE_KEY=0xYOUR_PRIVATE_KEY
export RPC_URL=https://mainnet.base.org   # or your own Base RPC
python -m bot.main --config my-config.yml --loop 3600
```

Or with Docker:

```bash
docker build -t aave-leverage-bot .
docker run -e PRIVATE_KEY=0x... -e RPC_URL=https://mainnet.base.org \
  -v $(pwd)/my-config.yml:/app/config.yml \
  -v $(pwd)/trades.jsonl:/app/trades.jsonl \
  aave-leverage-bot --loop 3600
```

## 7. Running multiple assets (one wallet per bot — required)

**Never point two bot instances at the same wallet address.** Each instance only reads its own `trades.jsonl` — they cannot see each other's open positions. Two bots on one wallet will:
- Both try to open simultaneously (double exposure)
- Both trigger health-factor defense at the same time (double close/reduce)
- Deplete each other's Aave collateral unexpectedly (HF is cross-collateral on Aave)

**Correct setup — one wallet per asset:**

```
mkdir bot-eth bot-btc
cp config.yml bot-eth/config.yml   # set asset=WETH, user_address=0xWALLET_ETH
cp config.yml bot-btc/config.yml   # set asset=cbBTC, user_address=0xWALLET_BTC
```

Run each independently:
```bash
# ETH bot
python3 -m bot.main --config bot-eth/config.yml --loop 3600

# BTC bot (separate terminal or process)
python3 -m bot.main --config bot-btc/config.yml --loop 3600
```

Each wallet needs its own:
- USDC + ETH balance on Base
- MCP session token (tokens are wallet-bound)
- `trades.jsonl` file (stored next to its config)

## 8. Cron deployment (single-cycle mode)

For minimal resource use, run one cycle per hour via cron:

```cron
0 * * * * cd /path/to/bot && python -m bot.main --config config.yml >> /var/log/aave-bot.log 2>&1
```

## Config reference

| Field | Default | Description |
|---|---|---|
| `paper_trading` | `true` | Dry-run mode — no real transactions |
| `asset` | `WETH` | Asset to trade |
| `leverage` | `3.0` | Target leverage for new positions |
| `max_leverage` | `4.0` | Hard cap — server will also enforce |
| `base_position_pct` | `0.20` | Fraction of total collateral used as seed |
| `strong_signal_size` | `1.0` | Multiplier for strong signal (3/3 timeframes) |
| `moderate_signal_size` | `0.5` | Multiplier for moderate signal (2/3 timeframes) |
| `take_profit_pct` | `5.0` | Close when up this % from entry |
| `stop_loss_pct` | `3.0` | Close when down this % from entry |
| `max_borrow_apr` | `8.0` | Skip new entries above this USDC APR |
| `max_volatility_1h` | `5.0` | Skip cycle if 1h move exceeds this % |
| `btc_dominance_rise_threshold` | `2.0` | Suppress longs if BTC dom rose > this % |
| `hf_defense_reduce` | `1.35` | Trigger reduce below this health factor |
| `hf_defense_close` | `1.20` | Force-close below this health factor |
| `min_open_hf` | `1.30` | Don't open if current HF is below this |

## Running tests

```bash
pip install pytest
pytest tests/ -v
```
