import os
import time
from datetime import datetime, timezone

from bot.alerts import Alert
from bot.heartbeat import write
from bot.journal import ExecutionJournal
from scripts.check_health import collect_health


def _paths(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    journal = tmp_path / "journal.sqlite3"
    ExecutionJournal(str(journal))
    return heartbeat, journal


def test_collect_health_flags_live_health_factor_warning(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": False,
            "last_decision": "skip_already_open",
            "last_health_factor": 1.10,
        },
    )

    result = collect_health(
        str(heartbeat),
        str(journal),
        max_age=3900,
        warn_health_factor=1.12,
        critical_health_factor=1.07,
    )

    assert result.issues == [
        Alert(
            "health_factor_warning",
            "warning",
            "health factor 1.100 is at or below 1.120",
        )
    ]


def test_collect_health_flags_critical_factor_and_safety_hold(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": False,
            "last_decision": "skip_safety_data_unavailable",
            "last_health_factor": 1.05,
        },
    )

    result = collect_health(
        str(heartbeat),
        str(journal),
        max_age=3900,
        warn_health_factor=1.12,
        critical_health_factor=1.07,
    )

    assert {issue.key for issue in result.issues} == {
        "bot_safety_hold",
        "health_factor_critical",
    }


def test_collect_health_flags_stale_heartbeat(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    write(str(heartbeat), {"status": "ok", "paper_trading": True})
    old = time.time() - 100
    os.utime(heartbeat, (old, old))

    result = collect_health(str(heartbeat), str(journal), max_age=10)

    assert [issue.key for issue in result.issues] == ["heartbeat_stale"]


def test_collect_health_reads_configured_defense_thresholds(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    config = tmp_path / "my-config.yml"
    config.write_text("hf_defense_reduce: 1.12\nhf_defense_close: 1.07\n")
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": False,
            "last_decision": "skip_already_open",
            "last_health_factor": 1.08,
        },
    )

    result = collect_health(
        str(heartbeat), str(journal), max_age=3900, config_path=str(config)
    )

    assert result.issues == [
        Alert(
            "health_factor_warning",
            "warning",
            "health factor 1.080 is at or below 1.120",
        )
    ]


def test_collect_health_flags_unavailable_funding_provider(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": True,
            "last_funding_provider": None,
            "last_funding_sources_attempted": ["okx", "binance", "bybit"],
            "last_funding_failures": [
                "okx:timeout",
                "binance:blocked_http_451",
                "bybit:blocked_http_403",
            ],
            "last_sources_failed": ["funding_rate"],
        },
    )

    result = collect_health(str(heartbeat), str(journal), max_age=3900)

    assert result.issues == [
        Alert(
            "funding_rate_unavailable",
            "warning",
            "funding provider unavailable; "
            "attempted=okx,binance,bybit; "
            "failures=okx:timeout,binance:blocked_http_451,bybit:blocked_http_403",
        )
    ]


def test_collect_health_flags_non_funding_market_source_failure(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": True,
            "last_funding_provider": "okx",
            "last_sources_failed": ["coingecko_global", "fear_greed"],
        },
    )

    result = collect_health(str(heartbeat), str(journal), max_age=3900)

    assert result.issues == [
        Alert(
            "market_data_degraded",
            "warning",
            "market data sources failed: coingecko_global, fear_greed",
        )
    ]


def test_collect_health_accepts_fresh_source_observation_metadata(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    observed = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": True,
            "last_source_observed_at": {
                source: observed
                for source in (
                    "coingecko_prices",
                    "get_position",
                    "coingecko_global",
                    "funding_rate",
                    "onchain",
                    "fear_greed",
                )
            },
            "last_signal_source": "coingecko",
        },
    )

    result = collect_health(str(heartbeat), str(journal), max_age=3900)

    assert result.issues == []


def test_collect_health_flags_stale_source_observation(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    write(
        str(heartbeat),
        {
            "status": "ok",
            "paper_trading": True,
            "last_source_observed_at": {"coingecko_prices": "2020-01-01T00:00:00Z"},
        },
    )

    result = collect_health(str(heartbeat), str(journal), max_age=10)

    assert {issue.key for issue in result.issues} == {
        "source_observation_missing",
        "source_observation_stale",
    }


def test_collect_health_flags_direct_risk_warning(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    risk_snapshot = tmp_path / "risk.json"
    write(
        str(heartbeat),
        {"status": "ok", "paper_trading": True},
    )
    write(
        str(risk_snapshot),
        {
            "status": "ok",
            "available": True,
            "health_factor": 1.13,
        },
    )

    result = collect_health(
        str(heartbeat),
        str(journal),
        max_age=3900,
        risk_snapshot_path=str(risk_snapshot),
    )

    assert result.issues == [
        Alert(
            "risk_probe_health_factor_warning",
            "warning",
            "direct health factor 1.130 is at or below 1.140",
        )
    ]


def test_collect_health_flags_direct_risk_escalation(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    risk_snapshot = tmp_path / "risk.json"
    write(str(heartbeat), {"status": "ok", "paper_trading": True})
    write(
        str(risk_snapshot),
        {"status": "ok", "available": True, "health_factor": 1.12},
    )

    result = collect_health(
        str(heartbeat),
        str(journal),
        max_age=3900,
        risk_snapshot_path=str(risk_snapshot),
    )

    assert result.issues == [
        Alert(
            "risk_probe_health_factor_critical",
            "critical",
            "direct health factor 1.120 is at or below 1.120",
        )
    ]


def test_collect_health_flags_unavailable_direct_risk_probe(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    risk_snapshot = tmp_path / "risk.json"
    write(str(heartbeat), {"status": "ok", "paper_trading": True})
    write(
        str(risk_snapshot),
        {"status": "error", "available": False, "error": "Timeout"},
    )

    result = collect_health(
        str(heartbeat),
        str(journal),
        max_age=3900,
        risk_snapshot_path=str(risk_snapshot),
    )

    assert result.issues == [
        Alert(
            "risk_probe_unavailable",
            "critical",
            "independent risk probe unavailable: Timeout",
        )
    ]


def test_collect_health_flags_stale_direct_risk_snapshot(tmp_path):
    heartbeat, journal = _paths(tmp_path)
    risk_snapshot = tmp_path / "risk.json"
    write(str(heartbeat), {"status": "ok", "paper_trading": True})
    write(
        str(risk_snapshot),
        {"status": "ok", "available": True, "health_factor": 1.10},
    )
    old = time.time() - 601
    os.utime(risk_snapshot, (old, old))

    result = collect_health(
        str(heartbeat),
        str(journal),
        max_age=3900,
        risk_snapshot_path=str(risk_snapshot),
    )

    assert result.issues == [
        Alert(
            "risk_probe_stale",
            "critical",
            "risk snapshot age exceeds 600s",
        )
    ]
