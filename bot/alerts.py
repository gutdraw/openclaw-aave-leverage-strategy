"""Durable on-host alert state for the supervised trading bot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from bot.heartbeat import write


@dataclass(frozen=True)
class Alert:
    """A condition that should be visible to the operator."""

    key: str
    severity: str
    message: str


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _load(path: Path) -> dict:
    if not path.exists():
        return {"active": {}, "last": {}}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("alert state must be a JSON object")
    active = raw.get("active", {})
    last = raw.get("last", {})
    if not isinstance(active, dict) or not isinstance(last, dict):
        raise ValueError("alert state active and last fields must be objects")
    return {"active": active, "last": last}


def reconcile(path: str, alerts: Iterable[Alert], now: str | None = None) -> list[dict]:
    """Persist active alerts and return only alert transitions."""
    target = Path(path)
    timestamp = now or _now_iso()
    previous = _load(target)
    previous_active = previous["active"]
    current_alerts = {alert.key: alert for alert in alerts}
    active: dict[str, dict] = {}
    transitions: list[dict] = []

    for key, alert in current_alerts.items():
        prior = previous_active.get(key)
        first_seen = (
            prior.get("first_seen", timestamp) if isinstance(prior, dict) else timestamp
        )
        active[key] = {
            "key": alert.key,
            "severity": alert.severity,
            "message": alert.message,
            "first_seen": first_seen,
            "last_seen": timestamp,
        }
        if not isinstance(prior, dict):
            transitions.append(
                {
                    "key": key,
                    "status": "firing",
                    "severity": alert.severity,
                    "message": alert.message,
                    "ts": timestamp,
                }
            )
        elif prior.get("severity") != alert.severity:
            transitions.append(
                {
                    "key": key,
                    "status": "updated",
                    "severity": alert.severity,
                    "message": alert.message,
                    "ts": timestamp,
                }
            )

    last = dict(previous["last"])
    for key, prior in previous_active.items():
        if key in current_alerts or not isinstance(prior, dict):
            continue
        transitions.append(
            {
                "key": key,
                "status": "resolved",
                "severity": prior.get("severity", "warning"),
                "message": prior.get("message", ""),
                "ts": timestamp,
            }
        )

    for transition in transitions:
        last[transition["key"]] = transition

    write(
        str(target),
        {
            "version": 1,
            "updated_at": timestamp,
            "active": active,
            "last": last,
        },
    )
    return transitions
