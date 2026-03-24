"""
Self-improvement tools server — exposes analyze_performance, backtest,
and update_config as HTTP endpoints on localhost:8001.

Hermes calls these via tools.json exactly as it calls the main MCP server.

Usage:
    python -m bot.improve_server
    python -m bot.improve_server --config my-config.yml --port 8001
"""
from __future__ import annotations

import argparse
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

import bot.analytics as analytics
import bot.backtest as backtest
import bot.config_updater as config_updater

log = logging.getLogger(__name__)

app = FastAPI(title="Aave Strategy Self-Improvement Tools", version="1.0.0")

# Config path is set at startup via --config arg
_config_path = "config.yml"
_trades_file = "trades.jsonl"
_changes_log = "config_changes.jsonl"


# ── analyze_performance ───────────────────────────────────────────────────────

@app.post("/tools/analyze_performance")
async def analyze_performance_endpoint() -> JSONResponse:
    """
    Analyze strategy performance from trades.jsonl.
    Returns metrics, per-signal breakdown, filter counts, and improvement hints.
    """
    try:
        report = analytics.analyze(_trades_file)
        return JSONResponse(analytics.report_to_dict(report))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── backtest ──────────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    leverage: Optional[float] = None
    base_position_pct: Optional[float] = None
    max_volatility_1h: Optional[float] = None
    max_borrow_apr: Optional[float] = None
    btc_dominance_rise_threshold: Optional[float] = None
    seed_usd: float = 1000.0
    compare_to_baseline: bool = False


@app.post("/tools/backtest")
async def backtest_endpoint(req: BacktestRequest) -> JSONResponse:
    """
    Replay historical price data with proposed parameters.
    If compare_to_baseline=true, also runs with default parameters and returns a delta.
    """
    try:
        params = backtest.BacktestParams(
            take_profit_pct=req.take_profit_pct,
            stop_loss_pct=req.stop_loss_pct,
            leverage=req.leverage,
            base_position_pct=req.base_position_pct,
            max_volatility_1h=req.max_volatility_1h,
            max_borrow_apr=req.max_borrow_apr,
            btc_dominance_rise_threshold=req.btc_dominance_rise_threshold,
        )

        if req.compare_to_baseline:
            baseline = backtest.BacktestParams()  # all defaults
            result = backtest.compare(baseline, params, _trades_file, req.seed_usd)
            return JSONResponse(result)

        result = backtest.run(params, _trades_file, req.seed_usd)
        return JSONResponse({
            "params": result.params,
            "total_cycles": result.total_cycles,
            "simulated_trades": result.simulated_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": result.win_rate,
            "total_pnl_usd": result.total_pnl_usd,
            "avg_pnl_usd": result.avg_pnl_usd,
            "best_trade_usd": result.best_trade_usd,
            "worst_trade_usd": result.worst_trade_usd,
            "max_drawdown_usd": result.max_drawdown_usd,
            "trades": [
                {
                    "direction": t.direction,
                    "signal": t.signal,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "realised_usd": t.realised_usd,
                    "realised_pct": t.realised_pct,
                }
                for t in result.trades
            ],
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── update_config ─────────────────────────────────────────────────────────────

class UpdateConfigRequest(BaseModel):
    changes: dict[str, Any]
    reason: str = ""


@app.post("/tools/update_config")
async def update_config_endpoint(req: UpdateConfigRequest) -> JSONResponse:
    """
    Propose and apply config parameter changes.
    All changes are validated against hard bounds.
    Requires paper_trading=true in current config.
    """
    try:
        result = config_updater.propose(
            changes=req.changes,
            config_path=_config_path,
            changes_log=_changes_log,
            reason=req.reason,
        )
        return JSONResponse({
            "success": result.success,
            "applied": result.applied,
            "rejected": result.rejected,
            "message": result.message,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tools/config_bounds")
async def config_bounds_endpoint() -> JSONResponse:
    """Return the hard bounds for all tunable parameters."""
    return JSONResponse(config_updater.get_bounds())


@app.get("/tools/config_history")
async def config_history_endpoint() -> JSONResponse:
    """Return the log of all config changes made by Hermes."""
    return JSONResponse(config_updater.get_change_history(_changes_log))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "improve_server"})


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    global _config_path, _trades_file, _changes_log

    parser = argparse.ArgumentParser(description="Strategy self-improvement tools server")
    parser.add_argument("--config",      default="config.yml",           help="Path to config.yml")
    parser.add_argument("--trades-file", default="trades.jsonl",         help="Path to trades.jsonl")
    parser.add_argument("--changes-log", default="config_changes.jsonl", help="Path to change log")
    parser.add_argument("--port",        default=8001, type=int,         help="Port to listen on")
    parser.add_argument("--host",        default="127.0.0.1",            help="Host to bind to")
    args = parser.parse_args()

    _config_path = args.config
    _trades_file = args.trades_file
    _changes_log = args.changes_log

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")
    log.info("Starting improve_server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
