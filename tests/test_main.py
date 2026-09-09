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
