from types import SimpleNamespace

from bot.config import BotConfig
from bot.journal import ExecutionJournal
from bot.main import (
    _cycle_heartbeat_payload,
    _run_price_unavailable_cycle,
    _safety_snapshot_incomplete,
)
from bot.market import MarketData


def _market_data(*, position_available: bool, onchain_available: bool) -> MarketData:
    return MarketData(
        price=80_000.0,
        change_1h=0.0,
        change_24h=0.0,
        change_7d=0.0,
        borrow_apr=5.0,
        btc_dominance=50.0,
        health_factor=1.15,
        total_collateral_usd=500.0,
        position_data={"aave": {}},
        position_available=position_available,
        onchain_available=onchain_available,
    )


def test_safety_snapshot_requires_full_onchain_data_before_new_exposure():
    data = _market_data(position_available=True, onchain_available=False)

    assert _safety_snapshot_incomplete(data, None) is True


def test_safety_snapshot_keeps_open_position_protection_available():
    data = _market_data(position_available=True, onchain_available=False)

    assert _safety_snapshot_incomplete(data, {"action": "open"}) is False


def test_safety_snapshot_blocks_position_increase_without_full_onchain_data():
    data = _market_data(position_available=True, onchain_available=False)

    assert _safety_snapshot_incomplete(data, {"action": "open"}, new_exposure=True)


def test_safety_snapshot_always_requires_position_snapshot():
    data = _market_data(position_available=False, onchain_available=True)

    assert _safety_snapshot_incomplete(data, {"action": "open"}) is True


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


def test_cycle_heartbeat_marks_unavailable_price_without_raw_failure_details(tmp_path):
    journal = ExecutionJournal(str(tmp_path / "journal.sqlite3"))
    config = BotConfig(asset="cbBTC", paper_trading=False)

    heartbeat = _cycle_heartbeat_payload(
        {
            "decision": "skip_market_data_unavailable",
            "position_state_before": "flat",
            "price_available": False,
            "price_failures": ["coingecko_prices:raw provider URL"],
            "sources_failed": ["coingecko_prices:raw provider URL"],
        },
        config,
        journal,
    )

    assert heartbeat["status"] == "error"
    assert heartbeat["error"] == "market_data_unavailable"
    assert heartbeat["last_price_failures"] == ["coingecko_prices"]
    assert heartbeat["last_sources_failed"] == ["coingecko_prices"]


def test_cycle_heartbeat_marks_wrapper_proxy_as_degraded(tmp_path):
    journal = ExecutionJournal(str(tmp_path / "journal.sqlite3"))
    config = BotConfig(asset="cbBTC", paper_trading=False)

    heartbeat = _cycle_heartbeat_payload(
        {
            "decision": "skip_price_protection_unavailable",
            "position_state_before": "open",
            "price": 83_000.0,
            "price_available": True,
            "price_provider": "coinbase_price",
            "price_entry_eligible": False,
            "price_protection_eligible": False,
            "price_failures": ["coingecko_prices:blocked_http_403"],
            "sources_failed": ["coingecko_prices:blocked_http_403"],
        },
        config,
        journal,
    )

    assert heartbeat["status"] == "ok"
    assert heartbeat["market_data_degraded"] is True
    assert heartbeat["last_price_provider"] == "coinbase_price"
    assert heartbeat["last_price_entry_eligible"] is False
    assert heartbeat["last_price_protection_eligible"] is False


def test_price_unavailable_open_position_still_closes_on_hf(tmp_path, monkeypatch):
    position = {
        "supplySymbol": "cbBTC",
        "supplyAsset": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "borrowSymbol": "USDC",
        "borrowAsset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "aTokenBalance": 0.005,
        "variableDebt": 250.0,
    }
    data = MarketData(
        price=0.0,
        change_1h=0.0,
        change_24h=0.0,
        change_7d=0.0,
        borrow_apr=4.0,
        btc_dominance=50.0,
        health_factor=1.0,
        total_collateral_usd=500.0,
        position_data={"aavePositions": {"positions": [position]}},
        price_available=False,
        position_available=True,
        onchain_available=True,
    )
    config = BotConfig(
        asset="cbBTC",
        borrow_asset="USDC",
        short_borrow_asset="cbBTC",
        paper_trading=False,
        trades_file=str(tmp_path / "trades.jsonl"),
    )
    open_trade = {
        "action": "open",
        "asset": "cbBTC",
        "direction": "long",
        "position_id": "cbBTC/USDC",
        "entry_price": 80_000.0,
        "supply": 0.005,
        "borrow": 250.0,
        "leverage": 3.0,
        "ts": "2026-09-29T00:00:00Z",
    }
    close_result = SimpleNamespace(
        tx_hash="0xclose",
        execution_id=None,
        raw={},
    )
    monkeypatch.setattr(
        "bot.main.executor.close_position", lambda *args, **kwargs: close_result
    )

    result = _run_price_unavailable_cycle(
        config,
        {"position_id": "cbBTC/USDC"},
        data,
        ["coingecko_prices:blocked_http_403"],
        [open_trade],
        open_trade,
        0.005,
        250.0,
        80_000.0,
        {"schema_version": 1},
        SimpleNamespace(),
        SimpleNamespace(),
        None,
    )

    assert result["decision"] == "hf_close"
    entries = [
        line for line in (tmp_path / "trades.jsonl").read_text().splitlines() if line
    ]
    assert len(entries) == 2
    assert '"pnl_pending_price": true' in entries[1]


def test_price_unavailable_chain_position_without_local_state_holds_for_reconciliation(
    tmp_path,
):
    data = MarketData(
        price=0.0,
        change_1h=0.0,
        change_24h=0.0,
        change_7d=0.0,
        borrow_apr=4.0,
        btc_dominance=50.0,
        health_factor=999.0,
        total_collateral_usd=500.0,
        position_data={"aavePositions": {"positions": [{"positionId": "cbBTC/USDC"}]}},
        price_available=False,
        position_available=True,
        onchain_available=True,
    )
    config = BotConfig(
        asset="cbBTC",
        paper_trading=False,
        trades_file=str(tmp_path / "trades.jsonl"),
    )

    result = _run_price_unavailable_cycle(
        config,
        {"position_id": "cbBTC/USDC"},
        data,
        ["coingecko_prices:blocked_http_403"],
        [],
        None,
        0.0,
        0.0,
        0.0,
        {"schema_version": 1},
        SimpleNamespace(),
        SimpleNamespace(),
        None,
    )

    assert result["decision"] == "skip_state_reconciliation"
