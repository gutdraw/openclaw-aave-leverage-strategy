import json
import tempfile
from pathlib import Path

from bot.state import (
    append_entry,
    classify_cycle_decision,
    get_last_btc_dominance,
    get_open_trade,
    load_entries,
    load_entries_with_recovery,
    now_iso,
)


def test_now_iso_format():
    ts = now_iso()
    assert ts.endswith("Z")
    assert "T" in ts


def test_load_empty():
    with tempfile.TemporaryDirectory() as d:
        assert load_entries(str(Path(d) / "missing.jsonl")) == []


def test_append_and_load():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        path = f.name
    append_entry(path, {"type": "cycle", "x": 1})
    append_entry(path, {"type": "cycle", "x": 2})
    entries = load_entries(path)
    assert len(entries) == 2
    assert entries[0]["x"] == 1


def test_recovers_incomplete_final_line_and_preserves_quarantine(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    path.write_bytes(b'{"type":"cycle","x":1}\n{"type":"cycle"')

    entries, recovery_path = load_entries_with_recovery(str(path))

    assert entries == [{"type": "cycle", "x": 1}]
    assert recovery_path is not None
    assert Path(recovery_path).read_bytes() == b'{"type":"cycle"'


def test_get_open_trade_none_when_empty():
    assert get_open_trade([]) is None


def test_get_open_trade_open():
    entries = [{"type": "trade", "action": "open", "asset": "WETH"}]
    t = get_open_trade(entries)
    assert t is not None
    assert t["asset"] == "WETH"


def test_get_open_trade_closed():
    entries = [
        {"type": "trade", "action": "open", "asset": "WETH"},
        {"type": "trade", "action": "close", "asset": "WETH"},
    ]
    assert get_open_trade(entries) is None


def test_get_open_trade_reopened():
    entries = [
        {"type": "trade", "action": "open", "asset": "WETH"},
        {"type": "trade", "action": "close", "asset": "WETH"},
        {"type": "trade", "action": "open", "asset": "WETH"},
    ]
    assert get_open_trade(entries) is not None


def test_get_last_btc_dominance():
    entries = [
        {"type": "cycle", "btc_dominance_pct": 48.0},
        {"type": "cycle", "btc_dominance_pct": 51.0},
    ]
    assert get_last_btc_dominance(entries) == 51.0


def test_get_last_btc_dominance_missing():
    assert get_last_btc_dominance([{"type": "cycle"}]) is None


def test_classify_cycle_decision_distinguishes_holds_and_safety_gates() -> None:
    assert classify_cycle_decision("hold", "flat", "hold") == "flat_signal_hold"
    assert classify_cycle_decision("hold", "open", "hold") == "position_management_hold"
    assert classify_cycle_decision("skip_volatility", "flat", "hold") == "strategy_gate"
    assert (
        classify_cycle_decision("skip_execution_recovery", "unknown", None)
        == "execution_recovery"
    )


def test_append_entry_persists_decision_category(tmp_path) -> None:
    path = tmp_path / "trades.jsonl"
    append_entry(
        str(path),
        {
            "type": "cycle",
            "signal": "hold",
            "position_state_before": "flat",
            "decision": "hold",
        },
    )

    record = json.loads(path.read_text())
    assert record["decision_category"] == "flat_signal_hold"
