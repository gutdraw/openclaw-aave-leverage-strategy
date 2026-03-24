import json
import tempfile

from bot.backtest import BacktestParams, run, compare


def _write_log(entries: list[dict]) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
    for e in entries:
        f.write(json.dumps(e) + "\n")
    f.close()
    return f.name


def _cycle(ts, price, c1h, c24h, c7d, borrow_apr=4.0, btc_dom=55.0, decision="hold"):
    return {
        "type": "cycle", "ts": ts, "price": price,
        "change_1h": c1h, "change_24h": c24h, "change_7d": c7d,
        "borrow_apr": borrow_apr, "btc_dominance_pct": btc_dom,
        "decision": decision,
    }


# Strong long signal (3/3 positive), then price rises to TP
STRONG_LONG_TP = [
    _cycle("2026-01-01T00:00:00Z", 2000.0,  0.5,  1.0,  2.0),   # strong_long entry
    _cycle("2026-01-01T01:00:00Z", 2050.0,  0.5,  1.0,  2.0),   # +2.5%, no exit yet
    _cycle("2026-01-01T02:00:00Z", 2110.0,  0.5,  1.0,  2.0),   # +5.5% → TP at 5%
]

# Strong short signal (0/3 positive), then price falls to TP
STRONG_SHORT_TP = [
    _cycle("2026-01-01T00:00:00Z", 2000.0, -0.5, -1.0, -2.0),   # strong_short entry
    _cycle("2026-01-01T01:00:00Z", 1950.0, -0.5, -1.0, -2.0),   # -2.5%, no exit yet
    _cycle("2026-01-01T02:00:00Z", 1890.0, -0.5, -1.0, -2.0),   # -5.5% → TP at 5%
]

# Long signal then SL
LONG_SL = [
    _cycle("2026-01-01T00:00:00Z", 2000.0,  0.5,  1.0,  2.0),
    _cycle("2026-01-01T01:00:00Z", 1940.0, -0.5, -1.0, -2.0),   # -3% → SL
]


def test_empty_log():
    path = _write_log([])
    r = run(BacktestParams(), path)
    assert r.simulated_trades == 0
    assert r.total_pnl_usd == 0.0


def test_long_take_profit():
    path = _write_log(STRONG_LONG_TP)
    r = run(BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0), path, seed_usd=1000.0)
    assert r.simulated_trades == 1
    assert r.wins == 1
    assert r.trades[0].exit_reason == "take_profit"
    assert r.trades[0].direction == "long"
    assert r.trades[0].realised_usd > 0


def test_long_stop_loss():
    path = _write_log(LONG_SL)
    r = run(BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0), path, seed_usd=1000.0)
    assert r.simulated_trades == 1
    assert r.losses == 1
    assert r.trades[0].exit_reason == "stop_loss"
    assert r.trades[0].realised_usd < 0


def test_short_take_profit():
    path = _write_log(STRONG_SHORT_TP)
    r = run(BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0), path, seed_usd=1000.0)
    assert r.simulated_trades == 1
    assert r.wins == 1
    assert r.trades[0].exit_reason == "take_profit"
    assert r.trades[0].direction == "short"
    assert r.trades[0].realised_usd > 0


def test_wider_tp_misses_exit():
    # With TP=8%, the +5.5% move at cycle 3 doesn't trigger
    path = _write_log(STRONG_LONG_TP)
    r = run(BacktestParams(take_profit_pct=8.0, stop_loss_pct=3.0), path, seed_usd=1000.0)
    assert r.simulated_trades == 0   # position still open at end of data


def test_volatility_filter_blocks_entry():
    cycles = [_cycle("2026-01-01T00:00:00Z", 2000.0, 6.0, 1.0, 2.0)]  # 1h = 6% > 5% threshold
    path = _write_log(cycles)
    r = run(BacktestParams(max_volatility_1h=5.0), path, seed_usd=1000.0)
    assert r.simulated_trades == 0


def test_compare_returns_delta():
    path = _write_log(STRONG_LONG_TP + LONG_SL)
    baseline = BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0)
    proposed = BacktestParams(take_profit_pct=5.0, stop_loss_pct=5.0)  # wider SL
    result = compare(baseline, proposed, path, seed_usd=1000.0)
    assert "baseline" in result
    assert "proposed" in result
    assert "delta" in result
    assert result["delta"]["verdict"] in ("improvement", "regression")
