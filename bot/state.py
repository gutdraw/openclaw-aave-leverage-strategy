"""
Append-only JSONL trade log: read/write helpers for trades.jsonl.
All entries are immutable once written — never update or delete lines.
"""
import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_entries(path: str) -> list[dict]:
    """Load all log entries. Returns empty list if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return []
    entries = []
    for i, line in enumerate(p.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            log.warning("state: skipping malformed line %d in %s: %s", i, path, e)
    return entries


def get_open_trade(entries: list[dict]) -> Optional[dict]:
    """
    Return the currently open trade, or None.
    A trade is open if the last 'open' action for an asset
    has no subsequent 'close' action for the same asset.
    """
    open_trade: Optional[dict] = None
    for e in entries:
        if e.get("type") != "trade":
            continue
        if e.get("action") == "open":
            open_trade = e
        elif e.get("action") == "close" and open_trade is not None:
            if (e.get("asset") == open_trade.get("asset")
                    and e.get("direction", "long") == open_trade.get("direction", "long")):
                open_trade = None
    return open_trade


def get_effective_size(open_trade: Optional[dict], entries: list[dict]) -> tuple[float, float, float]:
    """
    Return (total_supply, total_borrow, avg_entry_price) for the open position,
    including any increases.  avg_entry_price is borrow-weighted so that P&L on
    the combined position is computed correctly.
    """
    if open_trade is None:
        return 0.0, 0.0, 0.0
    supply = float(open_trade.get("supply", 0))
    borrow = float(open_trade.get("borrow", 0))
    entry_price = float(open_trade.get("entry_price", 0))
    # running weighted sum: price × borrow for each tranche
    weighted_price = entry_price * borrow
    open_ts = open_trade.get("ts", "")
    past_open = False
    for e in entries:
        if e.get("type") != "trade":
            continue
        if e.get("action") == "open" and e.get("ts") == open_ts:
            past_open = True
            continue
        if not past_open:
            continue
        if e.get("action") == "close":
            break
        if e.get("action") == "increase":
            add_b = float(e.get("add_borrow", 0))
            add_s = float(e.get("add_supply", 0))
            inc_price = float(e.get("price", entry_price))
            supply += add_s
            borrow += add_b
            weighted_price += inc_price * add_b
    avg_entry = weighted_price / borrow if borrow > 0 else entry_price
    return supply, borrow, avg_entry


def has_been_increased(open_trade: Optional[dict], entries: list[dict]) -> bool:
    """Return True if the current open position has already been increased this trade."""
    if open_trade is None:
        return False
    open_ts = open_trade.get("ts", "")
    past_open = False
    for e in entries:
        if e.get("type") != "trade":
            continue
        if e.get("action") == "open" and e.get("ts") == open_ts:
            past_open = True
            continue
        if not past_open:
            continue
        if e.get("action") == "close":
            return False
        if e.get("action") == "increase":
            return True
    return False


def get_last_btc_dominance(entries: list[dict]) -> Optional[float]:
    """Return BTC dominance % from the most recent cycle entry that has it."""
    for e in reversed(entries):
        if e.get("type") == "cycle" and e.get("btc_dominance_pct") is not None:
            return float(e["btc_dominance_pct"])
    return None


def append_entry(path: str, entry: dict) -> None:
    """Append a single JSON entry as a new line. Uses an exclusive file lock to prevent
    concurrent writes from corrupting the log."""
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry) + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
