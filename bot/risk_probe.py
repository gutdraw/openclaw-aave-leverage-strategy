"""Independent, read-only Aave account-risk snapshot utilities."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable, Optional

from bot.heartbeat import write
from bot.onchain import AccountRiskData, fetch_account_risk

RISK_SNAPSHOT_VERSION = 1
RISK_WARNING_HEALTH_FACTOR = 1.14
RISK_ESCALATION_HEALTH_FACTOR = 1.12
RISK_SNAPSHOT_MAX_AGE_SECONDS = 600.0


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def refresh_snapshot(
    path: str,
    rpc_url: str,
    user_address: Optional[str],
    probe: Callable[[str, Optional[str], float], AccountRiskData] = fetch_account_risk,
    timeout: float = 10,
    now: Optional[str] = None,
) -> dict:
    """Run the read-only probe and atomically write a standalone snapshot."""
    updated_at = now or _now_iso()
    try:
        result = probe(rpc_url, user_address, timeout)
    except Exception as error:
        result = AccountRiskData(
            available=False,
            fetched_at=updated_at,
            error=type(error).__name__,
        )

    error = result.error
    if result.available and result.health_factor is None:
        error = "missing_health_factor"

    snapshot = {
        "version": RISK_SNAPSHOT_VERSION,
        "status": "ok" if result.available and not error else "error",
        "available": bool(result.available and not error),
        "updated_at": updated_at,
        "fetched_at": result.fetched_at,
        "health_factor": result.health_factor,
        "total_collateral_usd": result.total_collateral_usd,
        "total_debt_usd": result.total_debt_usd,
        "block_number": result.block_number,
        "error": error,
    }
    write(path, snapshot)
    return snapshot


def load_snapshot(path: str) -> dict:
    """Load and validate the standalone risk snapshot JSON object."""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("risk snapshot must be a JSON object")
    return raw
