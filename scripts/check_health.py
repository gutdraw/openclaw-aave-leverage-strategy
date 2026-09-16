"""Check supervision state and persist threshold-aware on-host alerts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.alerts import Alert, reconcile
from bot.journal import ExecutionJournal
from bot.risk_probe import (
    RISK_ESCALATION_HEALTH_FACTOR,
    RISK_SNAPSHOT_MAX_AGE_SECONDS,
    RISK_WARNING_HEALTH_FACTOR,
    load_snapshot,
    refresh_snapshot,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthCheck:
    issues: list[Alert]
    heartbeat: dict
    heartbeat_age: Optional[float]


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _telemetry_strings(value: object, limit: int = 16) -> list[str]:
    """Return bounded heartbeat telemetry for safe alert messages."""
    if not isinstance(value, list):
        return []
    return [item[:120] for item in value if isinstance(item, str)][:limit]


def _default_risk_snapshot_path(heartbeat_path: str) -> str:
    """Derive ``trades.risk.json`` from the standard heartbeat filename."""
    path = Path(heartbeat_path)
    marker = ".heartbeat.json"
    if path.name.endswith(marker):
        return str(path.with_name(path.name[: -len(marker)] + ".risk.json"))
    return str(path.with_suffix(".risk.json"))


def _risk_snapshot_alerts(
    snapshot_path: str,
    max_age: float = RISK_SNAPSHOT_MAX_AGE_SECONDS,
) -> list[Alert]:
    """Return alerts for an independent risk snapshot without trading side effects."""
    target = Path(snapshot_path)
    if not target.exists():
        return [Alert("risk_probe_missing", "critical", "risk snapshot is missing")]

    try:
        snapshot = load_snapshot(snapshot_path)
        age = time.time() - target.stat().st_mtime
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [
            Alert(
                "risk_probe_unreadable",
                "critical",
                f"risk snapshot unreadable: {type(error).__name__}",
            )
        ]

    if age > max_age:
        return [
            Alert(
                "risk_probe_stale",
                "critical",
                f"risk snapshot age exceeds {max_age:.0f}s",
            )
        ]

    if snapshot.get("status") != "ok" or snapshot.get("available") is not True:
        error = snapshot.get("error")
        detail = error[:120] if isinstance(error, str) else "unknown"
        return [
            Alert(
                "risk_probe_unavailable",
                "critical",
                f"independent risk probe unavailable: {detail}",
            )
        ]

    health_factor = _number(snapshot.get("health_factor"))
    if health_factor is None:
        return [
            Alert(
                "risk_probe_unavailable",
                "critical",
                "independent risk probe returned no numeric health factor",
            )
        ]
    if health_factor <= RISK_ESCALATION_HEALTH_FACTOR:
        return [
            Alert(
                "risk_probe_health_factor_critical",
                "critical",
                f"direct health factor {health_factor:.3f} is at or below "
                f"{RISK_ESCALATION_HEALTH_FACTOR:.3f}",
            )
        ]
    if health_factor <= RISK_WARNING_HEALTH_FACTOR:
        return [
            Alert(
                "risk_probe_health_factor_warning",
                "warning",
                f"direct health factor {health_factor:.3f} is at or below "
                f"{RISK_WARNING_HEALTH_FACTOR:.3f}",
            )
        ]
    return []


def _source_observation_alerts(heartbeat: dict, max_age: float) -> list[Alert]:
    """Validate local observation timestamps when the new metadata is present."""
    raw = heartbeat.get("last_source_observed_at")
    if raw is None:
        # Older heartbeat files predate source timestamps; preserve compatibility
        # until the bot has written its first metadata-bearing heartbeat.
        return []
    if not isinstance(raw, dict):
        return [
            Alert(
                "source_observation_unreadable",
                "warning",
                "source observation metadata is not an object",
            )
        ]

    required = (
        "coingecko_prices",
        "get_position",
        "coingecko_global",
        "funding_rate",
        "onchain",
        "fear_greed",
    )
    issues: list[Alert] = []
    for source in required:
        value = raw.get(source)
        if not isinstance(value, str) or not value:
            issues.append(
                Alert(
                    "source_observation_missing",
                    "warning",
                    f"source observation missing: {source}",
                )
            )
            continue
        try:
            observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - observed).total_seconds()
        except (TypeError, ValueError):
            issues.append(
                Alert(
                    "source_observation_unreadable",
                    "warning",
                    f"source observation timestamp invalid: {source}",
                )
            )
            continue
        if age > max_age:
            issues.append(
                Alert(
                    "source_observation_stale",
                    "warning",
                    f"source observation stale: {source}",
                )
            )
    if heartbeat.get("last_signal_source") == "ohlcv" and not heartbeat.get(
        "last_tech_observed_at"
    ):
        issues.append(
            Alert(
                "technical_observation_missing",
                "warning",
                "active OHLCV signal has no local observation timestamp",
            )
        )
    return issues


def _thresholds(
    config_path: Optional[str],
    warning: Optional[float],
    critical: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[Alert]]:
    raw: dict = {}
    if config_path:
        try:
            import yaml

        except ModuleNotFoundError as exc:
            return (
                None,
                None,
                Alert(
                    "health_config_unreadable",
                    "critical",
                    f"YAML support unavailable: {exc}",
                ),
            )
        try:
            loaded = yaml.safe_load(Path(config_path).read_text())
            if not isinstance(loaded, dict):
                raise ValueError("config must contain a YAML mapping")
            raw = loaded
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return (
                None,
                None,
                Alert(
                    "health_config_unreadable",
                    "critical",
                    f"health config unreadable: {exc}",
                ),
            )

    warning_value = warning
    critical_value = critical
    if warning_value is None and "hf_defense_reduce" in raw:
        warning_value = _number(raw["hf_defense_reduce"])
    if critical_value is None and "hf_defense_close" in raw:
        critical_value = _number(raw["hf_defense_close"])

    if "hf_defense_reduce" in raw and warning_value is None:
        return (
            None,
            None,
            Alert(
                "health_threshold_config",
                "critical",
                "warning health-factor threshold is not numeric",
            ),
        )
    if "hf_defense_close" in raw and critical_value is None:
        return (
            None,
            None,
            Alert(
                "health_threshold_config",
                "critical",
                "critical health-factor threshold is not numeric",
            ),
        )
    if (warning_value is None) != (critical_value is None):
        return (
            None,
            None,
            Alert(
                "health_threshold_config",
                "critical",
                "both warning and critical health-factor thresholds are required",
            ),
        )
    if warning_value is None:
        return None, None, None
    if warning_value <= 0 or critical_value is None or critical_value <= 0:
        return (
            None,
            None,
            Alert(
                "health_threshold_config",
                "critical",
                "health-factor thresholds must be positive",
            ),
        )
    if critical_value >= warning_value:
        return (
            None,
            None,
            Alert(
                "health_threshold_config",
                "critical",
                "critical health-factor threshold must be below warning threshold",
            ),
        )
    return warning_value, critical_value, None


def collect_health(
    heartbeat_path: str,
    journal_path: str,
    max_age: float,
    config_path: Optional[str] = None,
    warn_health_factor: Optional[float] = None,
    critical_health_factor: Optional[float] = None,
    risk_snapshot_path: Optional[str] = None,
    risk_refresh_error: Optional[str] = None,
) -> HealthCheck:
    """Collect all health issues without persisting or logging them."""
    issues: list[Alert] = []
    heartbeat: dict = {}
    heartbeat_age: Optional[float] = None
    warning, critical, threshold_issue = _thresholds(
        config_path, warn_health_factor, critical_health_factor
    )
    if threshold_issue:
        issues.append(threshold_issue)

    heartbeat_file = Path(heartbeat_path)
    if not heartbeat_file.exists():
        issues.append(Alert("heartbeat_missing", "critical", "heartbeat is missing"))
    else:
        try:
            loaded = json.loads(heartbeat_file.read_text())
            if not isinstance(loaded, dict):
                raise ValueError("heartbeat must be a JSON object")
            heartbeat = loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                Alert(
                    "heartbeat_unreadable", "critical", f"heartbeat unreadable: {exc}"
                )
            )
        else:
            heartbeat_age = time.time() - heartbeat_file.stat().st_mtime
            if heartbeat_age > max_age:
                issues.append(
                    Alert(
                        "heartbeat_stale",
                        "critical",
                        f"heartbeat age exceeds {max_age:.0f}s",
                    )
                )
            if heartbeat.get("status") == "error":
                issues.append(
                    Alert(
                        "heartbeat_error",
                        "critical",
                        f"last cycle failed: {heartbeat.get('error', 'unknown')}",
                    )
                )

            decision = heartbeat.get("last_decision")
            if decision in {
                "skip_execution_recovery",
                "skip_risk_config_unavailable",
                "skip_safety_data_unavailable",
                "skip_state_reconciliation",
            }:
                issues.append(
                    Alert(
                        "bot_safety_hold",
                        "warning",
                        f"last decision is {decision}",
                    )
                )

            if heartbeat.get("status") == "ok":
                source_failures = _telemetry_strings(
                    heartbeat.get("last_sources_failed")
                )
                non_funding_failures = [
                    source for source in source_failures if source != "funding_rate"
                ]
                if non_funding_failures:
                    issues.append(
                        Alert(
                            "market_data_degraded",
                            "warning",
                            "market data sources failed: "
                            + ", ".join(non_funding_failures),
                        )
                    )

                issues.extend(_source_observation_alerts(heartbeat, max_age))

                funding_provider = heartbeat.get("last_funding_provider")
                if "last_funding_provider" in heartbeat and not (
                    isinstance(funding_provider, str) and funding_provider.strip()
                ):
                    attempted = _telemetry_strings(
                        heartbeat.get("last_funding_sources_attempted")
                    )
                    failures = _telemetry_strings(
                        heartbeat.get("last_funding_failures")
                    )
                    details = ["funding provider unavailable"]
                    if attempted:
                        details.append("attempted=" + ",".join(attempted))
                    if failures:
                        details.append("failures=" + ",".join(failures))
                    issues.append(
                        Alert(
                            "funding_rate_unavailable",
                            "warning",
                            "; ".join(details),
                        )
                    )

            if heartbeat.get("paper_trading") is False and warning is not None:
                health_factor = _number(heartbeat.get("last_health_factor"))
                if health_factor is None:
                    issues.append(
                        Alert(
                            "health_factor_missing",
                            "warning",
                            "live heartbeat has no numeric health factor",
                        )
                    )
                elif critical is not None and health_factor <= critical:
                    issues.append(
                        Alert(
                            "health_factor_critical",
                            "critical",
                            f"health factor {health_factor:.3f} is at or below {critical:.3f}",
                        )
                    )
                elif health_factor <= warning:
                    issues.append(
                        Alert(
                            "health_factor_warning",
                            "warning",
                            f"health factor {health_factor:.3f} is at or below {warning:.3f}",
                        )
                    )

    if risk_refresh_error:
        issues.append(
            Alert(
                "risk_probe_unavailable",
                "critical",
                f"risk probe refresh failed: {risk_refresh_error}",
            )
        )
    elif risk_snapshot_path:
        issues.extend(_risk_snapshot_alerts(risk_snapshot_path))

    journal_file = Path(journal_path)
    if not journal_file.exists():
        issues.append(
            Alert("journal_missing", "critical", "execution journal is missing")
        )
    else:
        try:
            unresolved = ExecutionJournal(journal_path).recoverable()
        except Exception as exc:
            issues.append(
                Alert(
                    "journal_unreadable",
                    "critical",
                    f"execution journal unreadable: {exc}",
                )
            )
        else:
            if unresolved:
                ids = ", ".join(str(row.get("execution_id")) for row in unresolved)
                issues.append(
                    Alert(
                        "journal_unresolved",
                        "critical",
                        f"unresolved executions: {ids}",
                    )
                )

    return HealthCheck(issues, heartbeat, heartbeat_age)


def _log_transitions(transitions: list[dict]) -> None:
    for transition in transitions:
        status = transition.get("status")
        if status == "resolved":
            log.info(
                "ALERT RESOLVED [%s] %s: %s",
                transition.get("severity"),
                transition.get("key"),
                transition.get("message"),
            )
            continue
        level = (
            logging.ERROR
            if transition.get("severity") == "critical"
            else logging.WARNING
        )
        log.log(
            level,
            "ALERT %s [%s] %s: %s",
            str(status).upper(),
            transition.get("severity"),
            transition.get("key"),
            transition.get("message"),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--config")
    parser.add_argument(
        "--risk-snapshot",
        help="standalone atomic Aave risk snapshot path (derived from heartbeat by default)",
    )
    parser.add_argument(
        "--alerts", help="JSON file storing active and last alert state"
    )
    parser.add_argument("--max-age", type=float, default=3900)
    parser.add_argument("--warn-health-factor", type=float)
    parser.add_argument("--critical-health-factor", type=float)
    args = parser.parse_args()

    risk_snapshot_path = args.risk_snapshot
    risk_refresh_error: Optional[str] = None
    if args.config:
        risk_snapshot_path = risk_snapshot_path or _default_risk_snapshot_path(
            args.heartbeat
        )
        try:
            import yaml

            loaded = yaml.safe_load(Path(args.config).read_text())
            if not isinstance(loaded, dict):
                raise ValueError("config must contain a YAML mapping")
            rpc_url = loaded.get("rpc_url")
            user_address = loaded.get("user_address")
            if not isinstance(rpc_url, str) or not rpc_url:
                raise ValueError("rpc_url is missing")
            if not isinstance(user_address, str) or not user_address:
                raise ValueError("user_address is missing")
            refresh_snapshot(risk_snapshot_path, rpc_url, user_address)
        except Exception as error:
            risk_refresh_error = type(error).__name__
            log.warning("risk probe refresh failed: %s", risk_refresh_error)

    result = collect_health(
        args.heartbeat,
        args.journal,
        args.max_age,
        args.config,
        args.warn_health_factor,
        args.critical_health_factor,
        risk_snapshot_path,
        risk_refresh_error,
    )
    if args.alerts:
        try:
            transitions = reconcile(args.alerts, result.issues)
        except (OSError, ValueError, TypeError) as exc:
            log.error("alert state unavailable: %s", exc)
            result.issues.append(
                Alert(
                    "alert_state_unavailable",
                    "critical",
                    "alert state could not be persisted",
                )
            )
        else:
            _log_transitions(transitions)

    if result.issues:
        for issue in result.issues:
            log.log(
                logging.ERROR if issue.severity == "critical" else logging.WARNING,
                "unhealthy: [%s] %s",
                issue.key,
                issue.message,
            )
        return 3 if any(issue.severity == "critical" for issue in result.issues) else 2

    log.info(
        "healthy: "
        f"last_decision={result.heartbeat.get('last_decision')} "
        f"heartbeat_age={result.heartbeat_age:.0f}s"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
