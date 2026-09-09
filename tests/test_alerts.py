import json

from bot.alerts import Alert, reconcile


def test_reconcile_records_firing_without_repeating_same_alert(tmp_path):
    path = tmp_path / "alerts.json"
    alert = Alert("health_factor_warning", "warning", "health factor is low")

    first = reconcile(str(path), [alert], now="2026-09-09T00:00:00Z")
    second = reconcile(str(path), [alert], now="2026-09-09T00:05:00Z")

    assert first[0]["status"] == "firing"
    assert second == []
    state = json.loads(path.read_text())
    assert state["active"]["health_factor_warning"]["first_seen"] == (
        "2026-09-09T00:00:00Z"
    )
    assert state["active"]["health_factor_warning"]["last_seen"] == (
        "2026-09-09T00:05:00Z"
    )


def test_reconcile_records_resolution(tmp_path):
    path = tmp_path / "alerts.json"
    alert = Alert("heartbeat_stale", "critical", "heartbeat is stale")
    reconcile(str(path), [alert], now="2026-09-09T00:00:00Z")

    transitions = reconcile(str(path), [], now="2026-09-09T00:10:00Z")

    assert transitions == [
        {
            "key": "heartbeat_stale",
            "status": "resolved",
            "severity": "critical",
            "message": "heartbeat is stale",
            "ts": "2026-09-09T00:10:00Z",
        }
    ]
    state = json.loads(path.read_text())
    assert state["active"] == {}
    assert state["last"]["heartbeat_stale"]["status"] == "resolved"
