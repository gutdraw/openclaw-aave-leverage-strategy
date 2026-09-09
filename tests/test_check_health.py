import os
import time

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
