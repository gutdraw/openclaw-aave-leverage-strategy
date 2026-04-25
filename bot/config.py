"""
Config loader for the Aave leverage strategy bot.
Reads config.yml and validates required fields.
"""

import os
from dataclasses import dataclass, fields
from pathlib import Path
import yaml

PLACEHOLDER_ADDR = "0xYOUR_BOT_WALLET_ADDRESS"


@dataclass
class BotConfig:
    # ── Identity ──────────────────────────────────────────────────────────
    user_address: str = PLACEHOLDER_ADDR
    mcp_url: str = "https://aave-leverage-agent-production.up.railway.app"
    mcp_session_token: str = ""
    mcp_session_duration: str = (
        "month"  # auto-renewal duration: hour | day | week | month
    )
    private_key: str = ""  # required for live mode only — set via env var

    # ── Strategy ──────────────────────────────────────────────────────────
    asset: str = "WETH"
    borrow_asset: str = "USDC"
    short_borrow_asset: str = "WETH"  # asset to borrow (short) — e.g. WETH or cbBTC
    leverage: float = 3.0  # default leverage for both directions
    long_leverage: float = 0.0  # override leverage for longs only (0 = use leverage)
    short_leverage: float = 0.0  # override leverage for shorts only (0 = use leverage)
    max_leverage: float = 4.0
    base_position_pct: float = 0.20
    strong_signal_size: float = 1.0
    moderate_signal_size: float = 0.5

    # ── Filters ───────────────────────────────────────────────────────────
    max_volatility_1h: float = 5.0
    max_borrow_apr: float = 8.0
    btc_dominance_rise_threshold: float = 2.0
    # Funding rate: Binance perp funding in % per 8h. Positive = longs pay shorts.
    # Extreme positive → crowded longs → suppress new longs (and vice versa for shorts).
    max_funding_rate_long: float = 0.05  # skip longs if funding > 0.05% per 8h
    max_funding_rate_short: float = 0.05  # skip shorts if funding < -0.05% per 8h
    # Fear & Greed Index (0=extreme fear, 100=extreme greed).
    # Extreme greed → suppress longs (over-extended). Extreme fear → suppress shorts.
    max_fear_greed_long: int = 85  # skip longs if F&G >= this
    min_fear_greed_short: int = (
        15  # skip shorts if F&G <= this AND RSI < fear_greed_short_rsi_floor
    )
    fear_greed_short_rsi_floor: float = (
        35.0  # F&G short block lifted once RSI recovers above this
    )
    # Volume: suppress new entries if 24h spot volume is below threshold (USD).
    # Low volume = weak conviction behind price moves. Set 0 to disable.
    min_volume_24h_usd: float = 0.0

    # ── Health factor thresholds — longs ──────────────────────────────────
    min_open_hf: float = 1.30
    hf_defense_close: float = 1.20
    hf_defense_reduce: float = 1.35

    # ── Health factor thresholds — shorts ─────────────────────────────────
    # 2x short (supply=3×seed USDC, borrow=2×seed cbBTC/WETH) opens at HF ~1.17.
    # Short-specific thresholds must be below 1.17 to avoid immediate auto-close.
    short_max_leverage: float = 2.0  # hard cap — 3x short HF ~1.04 (near liquidation)
    short_min_open_hf: float = 1.12  # skip open if HF < this (buffer below 1.17)
    short_hf_defense_close: float = (
        1.05  # force close if HF drops here (~11% adverse move at 2x)
    )
    short_hf_defense_reduce: float = (
        1.09  # reduce if HF drops here (~7% adverse move at 2x)
    )

    # ── Exit rules ────────────────────────────────────────────────────────
    take_profit_pct: float = 5.0
    long_take_profit_pct: float = 0.0  # override TP for longs (0 = use take_profit_pct)
    short_take_profit_pct: float = (
        0.0  # override TP for shorts (0 = use take_profit_pct)
    )
    stop_loss_pct: float = 3.0
    long_stop_loss_pct: float = 0.0  # override SL for longs (0 = use stop_loss_pct)
    short_stop_loss_pct: float = 0.0  # override SL for shorts (0 = use stop_loss_pct)
    # Signal reversal exit: close a long when signal flips to short (or vice versa).
    # Only triggers when the opposing score reaches signal_reversal_min_score or below.
    # e.g. default=1 means close long if signal score ≤ 1 (moderate_short or strong_short).
    signal_reversal_exit: bool = True
    signal_reversal_min_score: int = (
        0  # 0=only strong reversal (score 0/3); 1=moderate+strong
    )
    min_hold_hours: float = 2.0  # minimum hours before signal reversal can trigger
    # Time-based exit: close after N days regardless of P&L (prevents carry drag + HF drift).
    max_hold_days: float = 14.0
    # TP suppression on strong signal: when False (default), take-profit is skipped if the
    # signal is still at maximum strength in the trade direction — let winners ride.
    # SL always applies. Set True to restore fixed-TP behaviour regardless of signal.
    tp_on_strong_signal: bool = False
    # Post-TP gate expiry: hours after a TP before moderate-signal same-direction
    # reopen is allowed again. Prevents indefinite blocking in range-bound markets.
    # Set 0 to disable (gate never expires — requires strong signal to reopen).
    post_tp_gate_hours: float = 48.0
    # Post-trailing-stop gate: hours after a trailing-stop close before moderate-signal
    # same-direction reopen is allowed. Mirrors post_tp_gate_hours but for stop-outs.
    # Set 0 to disable (always allow reopen after trailing stop, even on moderate signal).
    post_trailing_stop_gate_hours: float = 48.0

    # ── On-chain ──────────────────────────────────────────────────────────
    # Free public Base RPC — used for read-only on-chain data (utilization, liquidations).
    # For live mode with a private key, set this to a paid RPC for reliability.
    rpc_url: str = "https://mainnet.base.org"
    # eth_getLogs lookback in blocks. Alchemy free tier: max 10 (~20s on Base).
    # Alchemy PAYG supports up to 2000 (150 blocks ≈ 5 min is a good value then).
    onchain_lookback_blocks: int = 10
    # Suppress new entries if USDC pool utilization exceeds this (borrow APR spike risk).
    max_usdc_utilization: float = 0.92
    # Suppress new entries if this many liquidations occurred within the lookback window.
    # With 10-block window, even 1-2 liquidations in ~20s is notable stress.
    max_recent_liquidations: int = 3

    # ── Liquidity escape ──────────────────────────────────────────────────
    # If the flash-loan asset's pool utilization exceeds this while a position
    # is open, close immediately — don't wait until flash loans are impossible.
    # For longs the flash asset is USDC; for shorts it is the borrow asset (e.g. cbBTC).
    # Default 0.95 = exit at 95% utilization (well before the 100% lock-out).
    liquidity_escape_utilization: float = 0.95
    # Close immediately if utilization rose by more than this amount in a single
    # cycle — a fast-moving cascade is more dangerous than a slow drift.
    # 0.05 = 5 percentage-point jump per cycle triggers escape.
    liquidity_escape_velocity: float = 0.05

    # ── Trailing stop ─────────────────────────────────────────────────
    # Close if price drops more than trailing_stop_pct% from the highest
    # price since position open. 0 = disabled.
    # For longs: fires when price falls X% from peak (locks in gains).
    # For shorts: fires when price rises X% from trough (locks in gains).
    # Evaluated after TP/SL, before signal reversal — min_hold_hours applies.
    trailing_stop_pct: float = 0.0

    # ── Mode ──────────────────────────────────────────────────────────────
    paper_trading: bool = True
    paper_seed_usd: float = (
        0.0  # if > 0, use this as collateral in paper mode (no real funds needed)
    )
    trades_file: str = "trades.jsonl"

    # Internal — set by load(), not from config file
    _config_path: str = ""

    def tp_for(self, direction: str) -> float:
        """Return the effective take-profit % for the given direction."""
        if direction == "short":
            return (
                self.short_take_profit_pct
                if self.short_take_profit_pct > 0
                else self.take_profit_pct
            )
        return (
            self.long_take_profit_pct
            if self.long_take_profit_pct > 0
            else self.take_profit_pct
        )

    def sl_for(self, direction: str) -> float:
        """Return the effective stop-loss % for the given direction."""
        if direction == "short":
            return (
                self.short_stop_loss_pct
                if self.short_stop_loss_pct > 0
                else self.stop_loss_pct
            )
        return (
            self.long_stop_loss_pct
            if self.long_stop_loss_pct > 0
            else self.stop_loss_pct
        )

    def leverage_for(self, direction: str) -> float:
        """Return the effective leverage cap for a given trade direction.

        Respects long_leverage / short_leverage overrides if set (> 0).
        Shorts are additionally capped at short_max_leverage (hard safety limit).
        """
        if direction == "short":
            base = self.short_leverage if self.short_leverage > 0 else self.leverage
            return min(base, self.short_max_leverage)
        return self.long_leverage if self.long_leverage > 0 else self.leverage

    @classmethod
    def load(cls, path: str = "config.yml") -> "BotConfig":
        raw = yaml.safe_load(Path(path).read_text())
        valid_keys = {f.name for f in fields(cls) if not f.name.startswith("_")}
        filtered = {k: v for k, v in raw.items() if k in valid_keys}
        cfg = cls(**filtered)
        cfg._config_path = str(Path(path).resolve())

        if cfg.user_address == PLACEHOLDER_ADDR:
            raise ValueError(
                "user_address is still the placeholder. "
                "Set your bot wallet address in config.yml before running."
            )
        if not cfg.mcp_session_token:
            raise ValueError(
                "mcp_session_token is empty. "
                "Run scripts/buy_session.py to purchase one, or set PRIVATE_KEY "
                "and the client will auto-purchase on first call."
            )
        if (
            not cfg.paper_trading
            and not cfg.private_key
            and not os.environ.get("PRIVATE_KEY")
        ):
            raise ValueError(
                "private_key is required for live mode. "
                "Set PRIVATE_KEY env var or add it to config.yml (never commit it)."
            )
        return cfg
