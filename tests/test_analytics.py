import json
import tempfile
from pathlib import Path

from bot.analytics import analyze, report_to_dict


def _write_log(entries: list[dict]) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
    for e in entries:
        f.write(json.dumps(e) + "\n")
    f.close()
    return f.name


def _cycle(ts, price, signal, decision, borrow_apr=4.0, btc_dom=55.0, change_1h=0.5):
    return {
        "type": "cycle", "ts": ts, "price": price,
        "signal": signal, "decision": decision,
        "change_1h": change_1h, "change_24h": 1.0, "change_7d": 2.0,
        "borrow_apr": borrow_apr, "btc_dominance_pct": btc_dom,
    }


def _open(ts, price, signal="strong_long", direction="long", supply=1.0, borrow=2.0, seed=2000.0, lev=3.0):
    return {
        "type": "trade", "action": "open", "ts": ts,
        "asset": "WETH", "direction": direction, "signal": signal,
        "entry_price": price, "supply": supply, "borrow": borrow,
        "seed_usd": seed, "leverage": lev, "paper": True,
    }


def _close(ts, price, reason, realised):
    return {
        "type": "trade", "action": "close", "ts": ts,
        "asset": "WETH", "close_price": price,
        "reason": reason, "realised_usd": realised, "paper": True,
    }


def test_empty_log():
    path = _write_log([])
    r = analyze(path)
    assert r.total_cycles == 0
    assert r.total_trades == 0
    assert r.win_rate == 0.0


def test_single_winning_trade():
    entries = [
        _cycle("2026-01-01T00:00:00Z", 2000.0, "strong_long", "open_long"),
        _open("2026-01-01T00:00:00Z", 2000.0),
        _cycle("2026-01-01T01:00:00Z", 2100.0, "strong_long", "take_profit"),
        _close("2026-01-01T01:00:00Z", 2100.0, "take_profit", 300.0),
    ]
    path = _write_log(entries)
    r = analyze(path)
    assert r.closed_trades == 1
    assert r.win_rate == 1.0
    assert r.total_pnl_usd == 300.0
    assert r.best_trade_usd == 300.0


def test_mixed_trades():
    entries = [
        _cycle("2026-01-01T00:00:00Z", 2000.0, "strong_long", "open_long"),
        _open("2026-01-01T00:00:00Z", 2000.0, seed=2000.0),
        _close("2026-01-01T01:00:00Z", 2100.0, "take_profit", 300.0),
        _cycle("2026-01-02T00:00:00Z", 2100.0, "strong_long", "open_long"),
        _open("2026-01-02T00:00:00Z", 2100.0, seed=2000.0),
        _close("2026-01-02T01:00:00Z", 2037.0, "stop_loss", -180.0),
    ]
    path = _write_log(entries)
    r = analyze(path)
    assert r.closed_trades == 2
    assert r.win_rate == 0.5
    assert r.total_pnl_usd == 120.0
    assert r.worst_trade_usd == -180.0


def test_filter_counts():
    entries = [
        _cycle("2026-01-01T00:00:00Z", 2000.0, "strong_long", "skip_volatility"),
        _cycle("2026-01-02T00:00:00Z", 2000.0, "strong_long", "skip_volatility"),
        _cycle("2026-01-03T00:00:00Z", 2000.0, "strong_long", "open_long"),
    ]
    path = _write_log(entries)
    r = analyze(path)
    assert r.filter_counts.get("skip_volatility") == 2
    assert r.filter_counts.get("open_long") == 1


def test_by_signal_breakdown():
    entries = [
        _cycle("2026-01-01T00:00:00Z", 2000.0, "strong_long", "open_long"),
        _open("2026-01-01T00:00:00Z", 2000.0, signal="strong_long"),
        _close("2026-01-01T01:00:00Z", 2100.0, "take_profit", 300.0),
        _cycle("2026-01-02T00:00:00Z", 2100.0, "moderate_long", "open_long"),
        _open("2026-01-02T00:00:00Z", 2100.0, signal="moderate_long"),
        _close("2026-01-02T01:00:00Z", 2037.0, "stop_loss", -90.0),
    ]
    path = _write_log(entries)
    r = analyze(path)
    assert "strong_long" in r.by_signal
    assert r.by_signal["strong_long"].wins == 1
    assert "moderate_long" in r.by_signal
    assert r.by_signal["moderate_long"].losses == 1


def test_open_position_detected():
    entries = [
        _open("2026-01-01T00:00:00Z", 2000.0),
    ]
    path = _write_log(entries)
    r = analyze(path)
    assert r.open_trades == 1
    assert r.open_position is not None


def test_report_to_dict_serializable():
    entries = [
        _cycle("2026-01-01T00:00:00Z", 2000.0, "strong_long", "open_long"),
        _open("2026-01-01T00:00:00Z", 2000.0),
        _close("2026-01-01T01:00:00Z", 2100.0, "take_profit", 300.0),
    ]
    path = _write_log(entries)
    r = analyze(path)
    d = report_to_dict(r)
    # Must be JSON-serializable
    json.dumps(d)
    assert isinstance(d["by_signal"], dict)
    assert isinstance(d["hints"], list)
