import json
import tempfile
from pathlib import Path

from bot.state import (
    append_entry,
    get_last_btc_dominance,
    get_open_trade,
    load_entries,
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
        {"type": "trade", "action": "open",  "asset": "WETH"},
        {"type": "trade", "action": "close", "asset": "WETH"},
        {"type": "trade", "action": "open",  "asset": "WETH"},
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
