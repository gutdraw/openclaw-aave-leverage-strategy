import json

from bot.onchain import AccountRiskData
from bot.risk_probe import load_snapshot, refresh_snapshot


def test_refresh_snapshot_writes_atomic_success_payload(tmp_path) -> None:
    path = tmp_path / "trades.risk.json"

    def probe(rpc_url, user_address, timeout):
        assert rpc_url == "https://rpc.example"
        assert user_address == "0x0000000000000000000000000000000000000001"
        assert timeout == 10
        return AccountRiskData(
            available=True,
            health_factor=1.15,
            total_collateral_usd=475.82,
            total_debt_usd=329.04,
            block_number=123,
            fetched_at="2026-09-14T00:00:00Z",
        )

    snapshot = refresh_snapshot(
        str(path),
        "https://rpc.example",
        "0x0000000000000000000000000000000000000001",
        probe=probe,
        now="2026-09-14T00:00:01Z",
    )

    assert snapshot["status"] == "ok"
    assert snapshot["health_factor"] == 1.15
    assert load_snapshot(str(path)) == snapshot
    assert json.loads(path.read_text())["error"] is None


def test_refresh_snapshot_records_probe_error_without_raw_details(tmp_path) -> None:
    path = tmp_path / "trades.risk.json"

    def probe(rpc_url, user_address, timeout):
        del rpc_url, user_address, timeout
        raise RuntimeError("rpc URL and response details must not be persisted")

    snapshot = refresh_snapshot(
        str(path), "https://rpc.example", None, probe=probe, now="2026-09-14T00:00:01Z"
    )

    assert snapshot["status"] == "error"
    assert snapshot["available"] is False
    assert snapshot["error"] == "RuntimeError"
    assert "rpc URL" not in path.read_text()
