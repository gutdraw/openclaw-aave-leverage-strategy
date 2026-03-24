"""
Config loader for the Aave leverage strategy bot.
Reads config.yml and validates required fields.
"""
from dataclasses import dataclass, field, fields
from pathlib import Path
import yaml

PLACEHOLDER_ADDR = "0xYOUR_BOT_WALLET_ADDRESS"


@dataclass
class BotConfig:
    # ── Identity ──────────────────────────────────────────────────────────
    user_address: str = PLACEHOLDER_ADDR
    mcp_url: str = "https://aave-leverage-agent-production.up.railway.app"
    mcp_session_token: str = ""
    private_key: str = ""        # required for live mode only — set via env var

    # ── Strategy ──────────────────────────────────────────────────────────
    asset: str = "WETH"
    borrow_asset: str = "USDC"
    short_borrow_asset: str = "WETH"   # asset to borrow (short) — e.g. WETH or cbBTC
    leverage: float = 3.0
    max_leverage: float = 4.0
    base_position_pct: float = 0.20
    strong_signal_size: float = 1.0
    moderate_signal_size: float = 0.5

    # ── Filters ───────────────────────────────────────────────────────────
    max_volatility_1h: float = 5.0
    max_borrow_apr: float = 8.0
    btc_dominance_rise_threshold: float = 2.0

    # ── Health factor thresholds ──────────────────────────────────────────
    min_open_hf: float = 1.30
    hf_defense_close: float = 1.20
    hf_defense_reduce: float = 1.35

    # ── Exit rules ────────────────────────────────────────────────────────
    take_profit_pct: float = 5.0
    stop_loss_pct: float = 3.0

    # ── Mode ──────────────────────────────────────────────────────────────
    paper_trading: bool = True
    paper_seed_usd: float = 0.0   # if > 0, use this as collateral in paper mode (no real funds needed)
    trades_file: str = "trades.jsonl"

    @classmethod
    def load(cls, path: str = "config.yml") -> "BotConfig":
        raw = yaml.safe_load(Path(path).read_text())
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in raw.items() if k in valid_keys}
        cfg = cls(**filtered)

        if cfg.user_address == PLACEHOLDER_ADDR:
            raise ValueError(
                "user_address is still the placeholder. "
                "Set your bot wallet address in config.yml before running."
            )
        if not cfg.mcp_session_token:
            raise ValueError(
                "mcp_session_token is empty. "
                "Buy a session via POST /mcp/auth and paste the token into config.yml."
            )
        if not cfg.paper_trading and not cfg.private_key:
            raise ValueError(
                "private_key is required for live mode. "
                "Set PRIVATE_KEY env var or add it to config.yml (never commit it)."
            )
        return cfg
