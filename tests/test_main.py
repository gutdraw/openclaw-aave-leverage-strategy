from bot.config import BotConfig
from bot.journal import ExecutionJournal
from bot.main import _cycle_heartbeat_payload


def test_cycle_heartbeat_sanitizes_source_failure_details(tmp_path):
    journal = ExecutionJournal(str(tmp_path / "journal.sqlite3"))
    config = BotConfig(asset="cbBTC", paper_trading=False)
    result = {
        "decision": "skip_already_open",
        "price": 78_000.0,
        "health_factor": 1.15,
        "funding_rate": 0.01,
        "funding_provider": "okx",
        "funding_sources_attempted": ("okx",),
        "funding_failures": (),
        "sources_failed": [
            "coingecko_global:provider response included sensitive details",
            "funding_rate:raw provider error should not be copied",
        ],
    }

    heartbeat = _cycle_heartbeat_payload(result, config, journal)

    assert heartbeat["last_funding_provider"] == "okx"
    assert heartbeat["last_funding_sources_attempted"] == ["okx"]
    assert heartbeat["last_sources_failed"] == ["coingecko_global", "funding_rate"]
    assert all("sensitive" not in value for value in heartbeat["last_sources_failed"])


def test_cycle_heartbeat_includes_stable_provenance(tmp_path):
    journal = ExecutionJournal(str(tmp_path / "journal.sqlite3"))
    config = BotConfig(
        asset="cbBTC",
        paper_trading=False,
        _config_path=str(tmp_path / "my-config.yml"),
    )
    (tmp_path / "my-config.yml").write_text("user_address: test\n")
    provenance = {
        "schema_version": 1,
        "process_started_at": "2026-09-16T00:00:00Z",
        "pid": 123,
        "code_commit": "a" * 40,
        "config_sha256": "b" * 64,
    }

    heartbeat = _cycle_heartbeat_payload(
        {
            "decision": "hold",
            "position_state_before": "flat",
            "signal": "hold",
            "price": 78_000.0,
            "health_factor": 999.0,
            "source_observed_at": {"coingecko_prices": "2026-09-16T00:00:00Z"},
        },
        config,
        journal,
        provenance,
    )

    assert heartbeat["provenance"] == provenance
    assert heartbeat["last_decision_category"] == "flat_signal_hold"
    assert heartbeat["last_source_observed_at"] == {
        "coingecko_prices": "2026-09-16T00:00:00Z"
    }
