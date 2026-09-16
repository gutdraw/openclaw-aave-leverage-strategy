import json
import tempfile

from bot.backtest import BacktestParams, compare, run, run_live, walk_forward


def _write_log(entries: list[dict]) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
    for e in entries:
        f.write(json.dumps(e) + "\n")
    f.close()
    return f.name


def _cycle(ts, price, c1h, c24h, c7d, borrow_apr=4.0, btc_dom=55.0, decision="hold"):
    return {
        "type": "cycle",
        "ts": ts,
        "price": price,
        "change_1h": c1h,
        "change_24h": c24h,
        "change_7d": c7d,
        "borrow_apr": borrow_apr,
        "short_borrow_apr": borrow_apr,
        "btc_dominance_pct": btc_dom,
        "health_factor": 999.0,
        "funding_rate": None,
        "fear_greed": None,
        "volume_24h": None,
        "usdc_utilization": None,
        "asset_utilization": None,
        "short_asset_utilization": None,
        "recent_liquidations": None,
        "position_data_available": True,
        "onchain_data_available": True,
        "risk_data_available": True,
        "risk_fetched_at": None,
        "paper_trading": True,
        "asset_frozen": None,
        "asset_paused": None,
        "borrow_asset_frozen": None,
        "borrow_asset_paused": None,
        "short_asset_frozen": None,
        "short_asset_paused": None,
        "tech_source": None,
        "tech_ema_bull": None,
        "tech_rsi": None,
        "decision": decision,
    }


# Strong long signal (3/3 positive), then price rises to TP
STRONG_LONG_TP = [
    _cycle("2026-01-01T00:00:00Z", 2000.0, 0.5, 1.0, 2.0),  # strong_long entry
    _cycle("2026-01-01T01:00:00Z", 2050.0, 0.5, 1.0, 2.0),  # +2.5%, no exit yet
    _cycle("2026-01-01T02:00:00Z", 2110.0, 0.5, 1.0, 2.0),  # +5.5% → TP at 5%
]

# Strong short signal (0/3 positive), then price falls to TP
STRONG_SHORT_TP = [
    _cycle("2026-01-01T00:00:00Z", 2000.0, -0.5, -1.0, -2.0),  # strong_short entry
    _cycle("2026-01-01T01:00:00Z", 1950.0, -0.5, -1.0, -2.0),  # -2.5%, no exit yet
    _cycle("2026-01-01T02:00:00Z", 1890.0, -0.5, -1.0, -2.0),  # -5.5% → TP at 5%
]

# Long signal then SL
LONG_SL = [
    _cycle("2026-01-01T00:00:00Z", 2000.0, 0.5, 1.0, 2.0),
    _cycle("2026-01-01T01:00:00Z", 1940.0, -0.5, -1.0, -2.0),  # -3% → SL
]


def test_empty_log():
    path = _write_log([])
    r = run(BacktestParams(), path)
    assert r.simulated_trades == 0
    assert r.total_pnl_usd == 0.0


def test_long_take_profit():
    path = _write_log(STRONG_LONG_TP)
    r = run(
        BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0), path, seed_usd=1000.0
    )
    assert r.simulated_trades == 1
    assert r.wins == 1
    assert r.trades[0].exit_reason == "take_profit"
    assert r.trades[0].direction == "long"
    assert r.trades[0].realised_usd > 0


def test_long_stop_loss():
    path = _write_log(LONG_SL)
    r = run(
        BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0), path, seed_usd=1000.0
    )
    assert r.simulated_trades == 1
    assert r.losses == 1
    assert r.trades[0].exit_reason == "stop_loss"
    assert r.trades[0].realised_usd < 0


def test_short_take_profit():
    path = _write_log(STRONG_SHORT_TP)
    r = run(
        BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0), path, seed_usd=1000.0
    )
    assert r.simulated_trades == 1
    assert r.wins == 1
    assert r.trades[0].exit_reason == "take_profit"
    assert r.trades[0].direction == "short"
    assert r.trades[0].realised_usd > 0


def test_wider_tp_misses_exit():
    # With TP=8%, the +5.5% move at cycle 3 doesn't trigger
    path = _write_log(STRONG_LONG_TP)
    r = run(
        BacktestParams(take_profit_pct=8.0, stop_loss_pct=3.0), path, seed_usd=1000.0
    )
    assert r.simulated_trades == 0  # position still open at end of data


def test_volatility_filter_blocks_entry():
    cycles = [
        _cycle("2026-01-01T00:00:00Z", 2000.0, 6.0, 1.0, 2.0)
    ]  # 1h = 6% > 5% threshold
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


def test_live_replay_requires_recorded_signal_snapshot() -> None:
    path = _write_log(
        [
            {
                **_cycle(
                    "2026-01-01T00:00:00Z",
                    2000.0,
                    0.5,
                    1.0,
                    2.0,
                    decision="open_long",
                ),
                "signal": "strong_long",
                "score": 3,
                "position_data_available": True,
                "onchain_data_available": True,
                "strategy_config": {"tp_on_strong_signal": True},
            },
            {
                **_cycle(
                    "2026-01-01T01:00:00Z",
                    2110.0,
                    0.5,
                    1.0,
                    2.0,
                    decision="take_profit",
                ),
                "signal": "strong_long",
                "score": 3,
                "position_data_available": True,
                "onchain_data_available": True,
                "strategy_config": {"tp_on_strong_signal": True},
            },
        ]
    )

    result = run_live(
        path,
        BacktestParams(take_profit_pct=5.0, stop_loss_pct=3.0),
        seed_usd=1000.0,
    )

    assert result.faithful_replay is True
    assert result.incomplete_snapshot_cycles == 0
    assert result.simulated_trades == 1


def test_live_replay_reports_incomplete_snapshots() -> None:
    path = _write_log([_cycle("2026-01-01T00:00:00Z", 2000.0, 0.5, 1.0, 2.0)])

    result = run_live(path, require_complete_snapshots=True)

    assert result.simulated_trades == 0
    assert result.incomplete_snapshot_cycles == 1


def test_walk_forward_reports_cost_aware_out_of_sample_windows() -> None:
    path = _write_log(STRONG_LONG_TP + LONG_SL)
    with open(path, "rb") as source:
        before = source.read()

    result = walk_forward(
        BacktestParams(
            take_profit_pct=5.0,
            stop_loss_pct=3.0,
            round_trip_fee_bps=10.0,
            gas_usd_per_trade=2.0,
        ),
        path,
        test_cycles=3,
        faithful=False,
    )

    assert result["method"] == "fixed_parameter_walk_forward"
    assert result["cost_model_is_estimate"] is True
    assert result["aggregate"]["windows"] == 2
    assert result["aggregate"]["total_cost_usd"] > 0
    with open(path, "rb") as source:
        assert source.read() == before


def test_live_replay_uses_recorded_reversal_and_logged_time() -> None:
    config = {
        "signal_reversal_exit": True,
        "signal_reversal_min_score": 0,
        "min_hold_hours": 0.0,
        "tp_on_strong_signal": True,
        "max_hold_days": 0.0,
        "long_max_hold_days": 0.0,
    }
    reversal_path = _write_log(
        [
            {
                **_cycle("2026-01-01T00:00:00Z", 2000.0, 1.0, 1.0, 1.0),
                "signal": "strong_long",
                "strategy_config": config,
            },
            {
                **_cycle("2026-01-01T01:00:00Z", 2000.0, -1.0, -1.0, -1.0),
                "signal": "strong_short",
                "strategy_config": config,
            },
        ]
    )
    reversal = run_live(reversal_path, seed_usd=1000.0)

    assert reversal.simulated_trades == 1
    assert reversal.trades[0].exit_reason == "signal_reversal"

    time_path = _write_log(
        [
            {
                **_cycle("2026-01-01T00:00:00Z", 2000.0, 1.0, 1.0, 1.0),
                "signal": "strong_long",
                "strategy_config": {
                    **config,
                    "signal_reversal_exit": False,
                    "max_hold_days": 1.0,
                },
            },
            {
                **_cycle("2026-01-02T00:00:00Z", 2000.0, 0.5, 0.5, 0.5),
                "signal": "moderate_long",
                "strategy_config": {
                    **config,
                    "signal_reversal_exit": False,
                    "max_hold_days": 1.0,
                },
            },
        ]
    )
    time_exit = run_live(time_path, seed_usd=1000.0)

    assert time_exit.simulated_trades == 1
    assert time_exit.trades[0].exit_reason == "max_hold_days"
