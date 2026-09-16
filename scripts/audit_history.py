#!/usr/bin/env python3
"""Produce a read-only execution and accounting audit report."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.audit import (
    ReadOnlyRpc,
    build_report,
    enrich_with_rpc,
    load_journal_records,
    source_sha256,
)
from bot.state import load_entries

log = logging.getLogger(__name__)


def _load_rpc_settings(config_path: str) -> tuple[str | None, str | None]:
    import yaml

    loaded = yaml.safe_load(Path(config_path).read_text())
    if not isinstance(loaded, dict):
        raise ValueError("config must contain a YAML mapping")
    rpc_url = loaded.get("rpc_url")
    wallet_address = loaded.get("user_address")
    return (
        rpc_url if isinstance(rpc_url, str) and rpc_url else None,
        wallet_address if isinstance(wallet_address, str) and wallet_address else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", default="trades.jsonl")
    parser.add_argument("--journal", default="trades.sqlite3")
    parser.add_argument("--since", help="include cycles at or after ISO-8601 timestamp")
    parser.add_argument(
        "--config", help="read-only source of rpc_url and user_address for RPC mode"
    )
    parser.add_argument(
        "--rpc-url", help="optional RPC URL; enables read-only receipt enrichment"
    )
    parser.add_argument(
        "--wallet-address", help="wallet address used for transfer deltas"
    )
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args()

    try:
        entries = load_entries(args.trades)
    except (OSError, ValueError) as error:
        log.error("unable to read trades: %s", type(error).__name__)
        return 2

    journal_records = load_journal_records(args.journal)
    report = build_report(
        entries,
        journal_records,
        trades_source={
            "path": Path(args.trades).name,
            "sha256": source_sha256(args.trades),
            "records": len(entries),
        },
        journal_source={
            "path": Path(args.journal).name,
            "sha256": source_sha256(args.journal),
            "executions": len(journal_records),
        },
        since=args.since,
    )

    rpc_url = args.rpc_url
    wallet_address = args.wallet_address
    if args.config:
        try:
            configured_rpc, configured_wallet = _load_rpc_settings(args.config)
        except (OSError, ValueError):
            configured_rpc, configured_wallet = None, None
        rpc_url = rpc_url or configured_rpc
        wallet_address = wallet_address or configured_wallet

    if rpc_url:
        enrich_with_rpc(report, ReadOnlyRpc(rpc_url), wallet_address)

    sys.stdout.write(
        json.dumps(report, indent=args.indent, sort_keys=True, default=str)
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
