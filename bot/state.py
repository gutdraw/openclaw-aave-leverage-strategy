"""
Append-only JSONL trade log: read/write helpers for trades.jsonl.
All entries are immutable once written — never update or delete lines.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_entries(path: str) -> list[dict]:
    """Load all log entries. Returns empty list if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text().splitlines()
        if line.strip()
    ]


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


def get_last_btc_dominance(entries: list[dict]) -> Optional[float]:
    """Return BTC dominance % from the most recent cycle entry that has it."""
    for e in reversed(entries):
        if e.get("type") == "cycle" and e.get("btc_dominance_pct") is not None:
            return float(e["btc_dominance_pct"])
    return None


def append_entry(path: str, entry: dict) -> None:
    """Append a single JSON entry as a new line."""
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
