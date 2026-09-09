"""Check supervision state and persist threshold-aware on-host alerts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.alerts import Alert, reconcile
from bot.journal import ExecutionJournal

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
        "--alerts", help="JSON file storing active and last alert state"
    )
    parser.add_argument("--max-age", type=float, default=3900)
    parser.add_argument("--warn-health-factor", type=float)
    parser.add_argument("--critical-health-factor", type=float)
    args = parser.parse_args()

    result = collect_health(
        args.heartbeat,
        args.journal,
        args.max_age,
        args.config,
        args.warn_health_factor,
        args.critical_health_factor,
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
