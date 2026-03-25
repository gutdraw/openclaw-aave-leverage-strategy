"""
Validated config updater with guardrails.

Hermes calls this to propose parameter changes. Every change is:
  1. Validated against hard bounds
  2. Rejected if paper_trading is not true
  3. Written to config.yml atomically
  4. Logged to a change history file (config_changes.jsonl)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

import yaml

import bot.state as state


# ── Hard bounds — cannot be overridden ───────────────────────────────────────

BOUNDS: dict[str, tuple[float, float]] = {
    "leverage":                      (1.0,  4.0),
    "max_leverage":                  (1.0,  4.0),
    "base_position_pct":             (0.01, 0.50),
    "strong_signal_size":            (0.0,  1.0),
    "moderate_signal_size":          (0.0,  1.0),
    "take_profit_pct":               (1.0,  50.0),
    "stop_loss_pct":                 (0.5,  25.0),
    "max_borrow_apr":                (1.0,  30.0),
    "max_volatility_1h":             (0.5,  20.0),
    "btc_dominance_rise_threshold":  (0.5,  10.0),
    "hf_defense_reduce":             (1.10, 2.50),
    "hf_defense_close":              (1.05, 2.00),
    "min_open_hf":                   (1.05, 2.00),
    # Short-specific HF thresholds (must remain below 1.17 = 2x short open HF)
    "short_max_leverage":            (1.0,  2.0),
    "short_hf_defense_reduce":       (1.01, 1.16),
    "short_hf_defense_close":        (1.01, 1.14),
    "short_min_open_hf":             (1.01, 1.16),
    # Exit rules
    "signal_reversal_min_score":     (0,    2),    # int: 0=only strong flip, 1=moderate+strong, 2=any flip
    "max_hold_days":                 (0.5,  30.0),
}

# Fields that cannot be changed via this tool (identity / secrets)
IMMUTABLE = {"user_address", "mcp_url", "mcp_session_token", "private_key",
             "asset", "borrow_asset", "short_borrow_asset", "trades_file"}


@dataclass
class UpdateResult:
    success: bool
    applied: dict[str, Any]     # changes that were applied
    rejected: dict[str, str]    # field → reason for rejection
    message: str


def propose(
    changes: dict[str, Any],
    config_path: str = "config.yml",
    changes_log: str = "config_changes.jsonl",
    reason: str = "",
) -> UpdateResult:
    """
    Validate and apply proposed config changes.

    changes: dict of field → new value (e.g. {"take_profit_pct": 7.0})
    reason:  human-readable explanation from Hermes (logged for audit trail)

    Returns UpdateResult with applied/rejected breakdown.
    Writes to config.yml only if at least one change is valid.
    """
    raw = yaml.safe_load(Path(config_path).read_text())

    # ── Safety gate: must be in paper mode ───────────────────────────────
    if not raw.get("paper_trading", True):
        return UpdateResult(
            success=False,
            applied={},
            rejected={k: "live mode — switch to paper_trading: true before changing parameters" for k in changes},
            message="Config updates are only allowed in paper_trading mode. Switch to paper mode first.",
        )

    applied: dict[str, Any] = {}
    rejected: dict[str, str] = {}

    for field, new_val in changes.items():
        # ── Immutable fields ──────────────────────────────────────────────
        if field in IMMUTABLE:
            rejected[field] = f"'{field}' cannot be changed via this tool"
            continue

        # ── Unknown fields ────────────────────────────────────────────────
        if field not in raw and field not in BOUNDS:
            rejected[field] = f"unknown config field '{field}'"
            continue

        # ── Type coercion ─────────────────────────────────────────────────
        try:
            if field in BOUNDS:
                new_val = float(new_val)
            elif isinstance(raw.get(field), bool):
                new_val = bool(new_val)
            elif isinstance(raw.get(field), int):
                new_val = int(new_val)
        except (TypeError, ValueError) as e:
            rejected[field] = f"type error: {e}"
            continue

        # ── Bounds check ──────────────────────────────────────────────────
        if field in BOUNDS:
            lo, hi = BOUNDS[field]
            if not (lo <= new_val <= hi):
                rejected[field] = (
                    f"value {new_val} out of bounds [{lo}, {hi}]"
                )
                continue

        # ── Cross-field sanity: hf_defense_close < hf_defense_reduce ─────
        if field == "hf_defense_close":
            reduce_val = changes.get("hf_defense_reduce", raw.get("hf_defense_reduce", 1.35))
            if new_val >= float(reduce_val):
                rejected[field] = (
                    f"hf_defense_close ({new_val}) must be < hf_defense_reduce ({reduce_val})"
                )
                continue

        if field == "hf_defense_reduce":
            close_val = changes.get("hf_defense_close", raw.get("hf_defense_close", 1.20))
            if new_val <= float(close_val):
                rejected[field] = (
                    f"hf_defense_reduce ({new_val}) must be > hf_defense_close ({close_val})"
                )
                continue

        # Cross-field sanity for short HF pair
        if field == "short_hf_defense_close":
            reduce_val = changes.get("short_hf_defense_reduce", raw.get("short_hf_defense_reduce", 1.09))
            if new_val >= float(reduce_val):
                rejected[field] = (
                    f"short_hf_defense_close ({new_val}) must be < short_hf_defense_reduce ({reduce_val})"
                )
                continue

        if field == "short_hf_defense_reduce":
            close_val = changes.get("short_hf_defense_close", raw.get("short_hf_defense_close", 1.05))
            if new_val <= float(close_val):
                rejected[field] = (
                    f"short_hf_defense_reduce ({new_val}) must be > short_hf_defense_close ({close_val})"
                )
                continue

        applied[field] = new_val

    # ── Write config if anything was approved ─────────────────────────────
    if applied:
        raw.update(applied)
        Path(config_path).write_text(
            yaml.dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )

        # Append to change log
        log_entry = {
            "type": "config_change",
            "ts": state.now_iso(),
            "applied": applied,
            "rejected": rejected,
            "reason": reason,
        }
        state.append_entry(changes_log, log_entry)

    if applied and not rejected:
        message = f"Applied {len(applied)} change(s): {list(applied.keys())}"
    elif applied and rejected:
        message = (
            f"Applied {len(applied)} change(s): {list(applied.keys())}. "
            f"Rejected {len(rejected)}: {rejected}"
        )
    else:
        message = f"All {len(rejected)} change(s) rejected: {rejected}"

    return UpdateResult(
        success=bool(applied),
        applied=applied,
        rejected=rejected,
        message=message,
    )


def get_bounds() -> dict:
    """Return the hard bounds for all tunable parameters."""
    return {k: {"min": v[0], "max": v[1]} for k, v in BOUNDS.items()}


def get_change_history(changes_log: str = "config_changes.jsonl") -> list[dict]:
    """Return all logged config changes."""
    return [
        e for e in state.load_entries(changes_log)
        if e.get("type") == "config_change"
    ]
