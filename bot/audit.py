"""Read-only runtime, execution, and accounting audit helpers.

The strategy's ``realised_usd`` field is a modelled price-delta result. This
module keeps that number visible, but never upgrades it to cash-flow P&L unless
the required on-chain evidence exists. The default audit path is completely
offline and does not construct a signer, call MCP, renew a session, or write a
runtime file.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

import httpx
from web3 import Web3

import bot.state as state
from bot.onchain import _ASSET_ADDR, _ATOKEN, _VARDEBT
from bot.pnl import compute_realised

AUDIT_SCHEMA_VERSION = 1
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_BYTE_LITERAL_RE = re.compile(r"^b(['\"])(.*)\1$")
_HEXBYTES_RE = re.compile(r"^HexBytes\((.*)\)$")
_BASE_CHAIN_ID = "0x2105"
_TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()


def normalize_tx_hash(value: object) -> Optional[str]:
    """Normalize common JSON, HexBytes, and byte-literal transaction hashes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        candidate = bytes(value).hex()
    else:
        candidate = str(value).strip()
        wrapped = _HEXBYTES_RE.fullmatch(candidate)
        if wrapped:
            candidate = wrapped.group(1).strip()
        byte_literal = _BYTE_LITERAL_RE.fullmatch(candidate)
        if byte_literal:
            try:
                decoded = ast.literal_eval(candidate)
            except (SyntaxError, ValueError):
                decoded = None
            if isinstance(decoded, bytes):
                candidate = decoded.hex()
        candidate = candidate.strip("'\"")
        if candidate.startswith("0x"):
            candidate = candidate[2:]
    candidate = candidate.lower()
    return candidate if _HASH_RE.fullmatch(candidate) else None


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _int_hex(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        raw = str(value)
        return int(raw, 16) if raw.startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_after(value: object, since: Optional[datetime]) -> bool:
    if since is None:
        return True
    parsed = _timestamp(value)
    return parsed is not None and parsed >= since


def _position_key(event: dict) -> tuple[str, str, str]:
    return (
        str(event.get("asset") or ""),
        str(event.get("direction") or "long"),
        str(event.get("position_id") or ""),
    )


def _live_event(event: dict) -> bool:
    return event.get("paper") is False or bool(normalize_tx_hash(event.get("tx_hash")))


def _journal_steps(record: dict) -> list[dict]:
    raw_steps = record.get("steps")
    if isinstance(raw_steps, list):
        return [step for step in raw_steps if isinstance(step, dict)]
    fallback_hash = normalize_tx_hash(record.get("tx_hash"))
    return [{"tx_hash": fallback_hash}] if fallback_hash else []


def _step_hashes(record: dict) -> set[str]:
    hashes = {
        normalized
        for step in _journal_steps(record)
        if (normalized := normalize_tx_hash(step.get("tx_hash"))) is not None
    }
    normalized = normalize_tx_hash(record.get("tx_hash"))
    if normalized:
        hashes.add(normalized)
    return hashes


def _record_has_receipt(record: dict) -> bool:
    if record.get("receipt"):
        return True
    return any(isinstance(step.get("receipt"), dict) for step in _journal_steps(record))


def _finding(
    severity: str,
    code: str,
    message: str,
    *refs: str,
) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "evidence_refs": list(refs),
    }


def classify_cycles(entries: list[dict], since: Optional[str] = None) -> dict:
    """Classify cycle decisions and summarize the post-close idle period."""
    since_dt = _timestamp(since)
    active: dict[tuple[str, str, str], dict] = {}
    rows: list[dict] = []
    category_counts: dict[str, int] = {}
    signal_conflicts = 0
    last_close_ts: Optional[str] = None

    for line_number, event in enumerate(entries, 1):
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "cycle":
            if event.get("signal_conflict") is True:
                signal_conflicts += 1
            if not _is_after(event.get("ts"), since_dt):
                continue
            position_state = event.get("position_state_before")
            if position_state not in {"open", "flat"}:
                position_state = "open" if active else "flat"
            category = event.get("decision_category") or state.classify_cycle_decision(
                event.get("decision"), position_state, event.get("signal")
            )
            category_counts[category] = category_counts.get(category, 0) + 1
            rows.append(
                {
                    "line": line_number,
                    "ts": event.get("ts"),
                    "decision": event.get("decision"),
                    "decision_category": category,
                    "signal": event.get("signal"),
                    "signal_source": event.get("signal_source"),
                    "position_state_before": position_state,
                }
            )
        elif event_type == "trade":
            action = event.get("action")
            key = _position_key(event)
            if action == "open":
                active[key] = event
            elif action == "close":
                matched = key if key in active else None
                if matched is None and len(active) == 1:
                    matched = next(iter(active))
                if matched is not None:
                    active.pop(matched, None)
                last_close_ts = event.get("ts") or last_close_ts

    classified = sum(
        count
        for category, count in category_counts.items()
        if category not in {"unclassified", "unclassified_hold", "unrecorded_decision"}
    )
    unexplained = [
        row
        for row in rows
        if row["decision_category"]
        in {"unclassified", "unclassified_hold", "unrecorded_decision"}
    ]
    return {
        "total_cycles": len(rows),
        "classified_cycles": classified,
        "unexplained_cycles": len(unexplained),
        "coverage_pct": round(classified / len(rows) * 100, 2) if rows else 100.0,
        "by_category": category_counts,
        "signal_conflicts": signal_conflicts,
        "last_close_ts": last_close_ts,
        "unexplained": unexplained[:100],
    }


def _trade_requirements(action: object) -> tuple[str, ...]:
    if action == "open":
        return ("entry_price", "supply", "borrow", "leverage")
    if action == "close":
        return (
            "close_price",
            "entry_price",
            "supply",
            "borrow",
            "realised_usd",
        )
    if action == "increase":
        return ("price", "add_supply", "add_borrow")
    return ()


def _journal_index(
    journal_records: list[dict],
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    by_id: dict[str, dict] = {}
    by_hash: dict[str, list[dict]] = {}
    for record in journal_records:
        execution_id = record.get("execution_id")
        if isinstance(execution_id, str) and execution_id:
            by_id[execution_id] = record
        for tx_hash in _step_hashes(record):
            by_hash.setdefault(tx_hash, []).append(record)
    return by_id, by_hash


def _linked_auxiliary_ids(entries: list[dict]) -> set[str]:
    linked: set[str] = set()
    for event in entries:
        if not isinstance(event, dict):
            continue
        for key in ("pre_swap_execution_id", "post_close_swap_execution_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                linked.add(value)
    return linked


def reconcile_trades(entries: list[dict], journal_records: list[dict]) -> dict:
    """Reconcile trade structure, journal joins, and modelled P&L formulas."""
    by_id, by_hash = _journal_index(journal_records)
    active: dict[tuple[str, str, str], tuple[int, dict]] = {}
    pairs: list[dict] = []
    transactions: list[dict] = []
    findings: list[dict] = []
    linked_journal_ids: set[str] = _linked_auxiliary_ids(entries)
    hash_present = 0
    execution_id_present = 0
    journal_joined = 0
    receipt_present = 0
    state_event_present = 0
    steps_present = 0
    reported_total = 0.0
    formula_total = 0.0
    formula_count = 0
    duplicate_hashes: dict[str, int] = {}

    def resolve_journal(event: dict) -> Optional[dict]:
        execution_id = event.get("execution_id")
        if isinstance(execution_id, str) and execution_id in by_id:
            return by_id[execution_id]
        normalized_hash = normalize_tx_hash(event.get("tx_hash"))
        matches = by_hash.get(normalized_hash or "", [])
        return matches[0] if len(matches) == 1 else None

    for line_number, event in enumerate(entries, 1):
        if not isinstance(event, dict) or event.get("type") != "trade":
            continue
        action = event.get("action")
        if action not in {"open", "close", "increase", "reduce"}:
            continue
        ref = f"jsonl:{line_number}"
        missing = [
            field
            for field in _trade_requirements(action)
            if _number(event.get(field)) is None
        ]
        if missing:
            findings.append(
                _finding(
                    "warning",
                    "trade_fields_missing",
                    f"{action} is missing numeric fields: {', '.join(missing)}",
                    ref,
                )
            )

        normalized_hash = normalize_tx_hash(event.get("tx_hash"))
        if normalized_hash:
            hash_present += 1
            duplicate_hashes[normalized_hash] = (
                duplicate_hashes.get(normalized_hash, 0) + 1
            )
        execution_id = event.get("execution_id")
        if isinstance(execution_id, str) and execution_id:
            execution_id_present += 1

        live = _live_event(event)
        record = resolve_journal(event) if live else None
        if live and not execution_id:
            findings.append(
                _finding(
                    "warning",
                    "trade_missing_execution_id",
                    "live trade has no structural execution journal join key",
                    ref,
                )
            )
        if live and not normalized_hash:
            findings.append(
                _finding(
                    "warning",
                    "trade_missing_or_invalid_tx_hash",
                    "live trade has no normalized transaction hash",
                    ref,
                )
            )
        if live and record is None:
            findings.append(
                _finding(
                    "warning",
                    "trade_missing_journal_join",
                    "live trade does not join exactly one journal execution",
                    ref,
                )
            )
        if record is not None:
            journal_joined += 1
            journal_id = record.get("execution_id")
            if isinstance(journal_id, str):
                linked_journal_ids.add(journal_id)
            if _record_has_receipt(record):
                receipt_present += 1
            if record.get("state_event"):
                state_event_present += 1
            if record.get("steps"):
                steps_present += 1
            if normalized_hash and normalized_hash not in _step_hashes(record):
                findings.append(
                    _finding(
                        "warning",
                        "trade_journal_hash_mismatch",
                        "trade hash does not occur in its joined journal execution",
                        ref,
                        f"sqlite:{journal_id}",
                    )
                )
            if record.get("action") not in {action, "paper"}:
                findings.append(
                    _finding(
                        "warning",
                        "trade_journal_action_mismatch",
                        f"trade action {action} joined journal action {record.get('action')}",
                        ref,
                        f"sqlite:{journal_id}",
                    )
                )
            if record.get("status") != "complete":
                findings.append(
                    _finding(
                        "critical",
                        "journal_execution_not_complete",
                        f"journal execution status is {record.get('status')}",
                        ref,
                        f"sqlite:{journal_id}",
                    )
                )
            if live and not _record_has_receipt(record):
                findings.append(
                    _finding(
                        "warning",
                        "journal_receipt_missing",
                        "joined live execution has no persisted receipt",
                        ref,
                        f"sqlite:{journal_id}",
                    )
                )

        transaction = {
            "trade_ref": {"line": line_number, "action": action},
            "execution_id": execution_id,
            "hash": normalized_hash,
            "journal_status": record.get("status") if record else None,
            "step_count": len(_journal_steps(record)) if record else 0,
            "receipt_present": _record_has_receipt(record) if record else False,
            "evidence_grade": (
                "partial"
                if record is not None and _record_has_receipt(record)
                else "unproven"
            ),
        }
        transactions.append(transaction)

        key = _position_key(event)
        if action == "open":
            if key in active:
                findings.append(
                    _finding(
                        "warning",
                        "duplicate_open",
                        "open appears before its prior position closed",
                        ref,
                    )
                )
            active[key] = (line_number, event)
        elif action == "close":
            matched = key if key in active else None
            if matched is None and len(active) == 1:
                matched = next(iter(active))
            if matched is None:
                findings.append(
                    _finding(
                        "warning", "unmatched_close", "close has no preceding open", ref
                    )
                )
                continue
            open_line, opening = active.pop(matched)
            pair = {
                "open_line": open_line,
                "close_line": line_number,
                "direction": event.get("direction", opening.get("direction", "long")),
                "reported_realised_usd": _number(event.get("realised_usd")),
                "formula_realised_usd": None,
            }
            if pair["reported_realised_usd"] is not None:
                reported_total += pair["reported_realised_usd"]
            close_price = _number(event.get("close_price"))
            if close_price is not None:
                basis = {
                    "entry_price": event.get("entry_price", opening.get("entry_price")),
                    "supply": event.get("supply", opening.get("supply")),
                    "borrow": event.get("borrow", opening.get("borrow")),
                    "leverage": event.get("leverage", opening.get("leverage", 1)),
                    "direction": event.get(
                        "direction", opening.get("direction", "long")
                    ),
                }
                if all(
                    _number(basis.get(field)) is not None
                    for field in ("entry_price", "supply", "borrow", "leverage")
                ):
                    expected = compute_realised(basis, close_price)
                    pair["formula_realised_usd"] = round(expected, 2)
                    formula_total += expected
                    formula_count += 1
                    reported = pair["reported_realised_usd"]
                    if (
                        reported is not None
                        and abs(reported - round(expected, 2)) > 0.02
                    ):
                        findings.append(
                            _finding(
                                "warning",
                                "reported_pnl_formula_mismatch",
                                f"reported P&L {reported:.2f} differs from formula {expected:.2f}",
                                ref,
                                f"jsonl:{open_line}",
                            )
                        )
            pairs.append(pair)

    for open_line, event in active.values():
        findings.append(
            _finding(
                "warning",
                "unmatched_open",
                "open has no subsequent close",
                f"jsonl:{open_line}",
            )
        )

    for tx_hash, count in duplicate_hashes.items():
        if count > 1:
            findings.append(
                _finding(
                    "warning",
                    "duplicate_trade_tx_hash",
                    f"normalized hash appears in {count} trade records",
                    f"tx:{tx_hash}",
                )
            )

    seen_transaction_hashes = {
        tx_hash for transaction in transactions if (tx_hash := transaction.get("hash"))
    }
    for record in journal_records:
        execution_id = record.get("execution_id")
        for step in _journal_steps(record):
            tx_hash = normalize_tx_hash(step.get("tx_hash"))
            if not tx_hash or tx_hash in seen_transaction_hashes:
                continue
            seen_transaction_hashes.add(tx_hash)
            step_receipt = step.get("receipt")
            transactions.append(
                {
                    "trade_ref": None,
                    "journal_ref": f"sqlite:{execution_id}",
                    "execution_id": execution_id,
                    "hash": tx_hash,
                    "journal_status": record.get("status"),
                    "step_count": 1,
                    "receipt_present": isinstance(step_receipt, dict),
                    "evidence_grade": (
                        "partial" if isinstance(step_receipt, dict) else "unproven"
                    ),
                }
            )

    for record in journal_records:
        execution_id = record.get("execution_id")
        if not isinstance(execution_id, str) or execution_id in linked_journal_ids:
            continue
        findings.append(
            _finding(
                "warning",
                "journal_execution_unlinked",
                "journal execution is not structurally linked to a trade or auxiliary cycle event",
                f"sqlite:{execution_id}",
            )
        )

    missing_evidence = [
        "actual_swap_output_and_fill_price",
        "gas_cost_usd",
        "aave_interest_usd",
        "mcp_session_fee_usd",
        "manual_wallet_flows",
    ]
    return {
        "trade_records": sum(
            1
            for event in entries
            if isinstance(event, dict) and event.get("type") == "trade"
        ),
        "pairs": pairs,
        "transactions": transactions,
        "coverage": {
            "trade_records": sum(
                1
                for event in entries
                if isinstance(event, dict) and event.get("type") == "trade"
            ),
            "hash_present": hash_present,
            "execution_id_present": execution_id_present,
            "journal_joined": journal_joined,
            "receipt_present": receipt_present,
            "state_event_present": state_event_present,
            "steps_present": steps_present,
        },
        "pnl": {
            "reported_realised_usd": round(reported_total, 2),
            "formula_realised_usd": round(formula_total, 2),
            "formula_checked_pairs": formula_count,
            "reconciled_realised_usd": None,
            "confidence": "unproven",
            "costs": {
                "gas_wei": None,
                "gas_usd": None,
                "interest_usd": None,
                "slippage_usd": None,
                "mcp_fee_usd": None,
            },
            "missing_evidence": missing_evidence,
        },
        "open_positions": len(active),
        "findings": findings,
    }


class ReadOnlyRpc:
    """Tiny allowlisted JSON-RPC reader used only for optional audit enrichment."""

    _ALLOWED_METHODS = frozenset(
        {
            "eth_chainId",
            "eth_getTransactionByHash",
            "eth_getTransactionReceipt",
            "eth_getBlockByNumber",
            "eth_call",
            "eth_getLogs",
        }
    )

    def __init__(self, rpc_url: str, timeout: float = 10) -> None:
        self._rpc_url = rpc_url
        self._timeout = timeout

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in self._ALLOWED_METHODS:
            raise ValueError("RPC method is not allowlisted")
        try:
            response = httpx.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "id": 1,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise RuntimeError(type(error).__name__) from error
        if (
            not isinstance(payload, dict)
            or "error" in payload
            or "result" not in payload
        ):
            raise RuntimeError("invalid_rpc_response")
        return payload["result"]

    def receipt(self, tx_hash: str) -> Optional[dict]:
        result = self.call("eth_getTransactionReceipt", ["0x" + tx_hash])
        return result if isinstance(result, dict) else None

    def transaction(self, tx_hash: str) -> Optional[dict]:
        result = self.call("eth_getTransactionByHash", ["0x" + tx_hash])
        return result if isinstance(result, dict) else None


def _token_metadata() -> dict[str, tuple[str, int]]:
    metadata: dict[str, tuple[str, int]] = {}
    decimals = {"USDC": 6, "cbBTC": 8, "WETH": 18, "wstETH": 18}
    for symbol, address in _ASSET_ADDR.items():
        metadata[address.lower()] = (symbol, decimals.get(symbol, 18))
    for symbol, addresses in (("a", _ATOKEN), ("vDebt", _VARDEBT)):
        for asset, address in addresses.items():
            metadata[address.lower()] = (f"{symbol}{asset}", decimals.get(asset, 18))
    return metadata


def _decode_transfers(receipt: dict, wallet_address: Optional[str]) -> list[dict]:
    metadata = _token_metadata()
    wallet = wallet_address.lower() if isinstance(wallet_address, str) else None
    transfers: list[dict] = []
    for log_entry in receipt.get("logs", []):
        if not isinstance(log_entry, dict):
            continue
        topics = log_entry.get("topics")
        if (
            not isinstance(topics, list)
            or len(topics) < 3
            or str(topics[0]).lower() != _TRANSFER_TOPIC
        ):
            continue
        from_topic = str(topics[1])
        to_topic = str(topics[2])
        if len(from_topic) < 40 or len(to_topic) < 40:
            continue
        sender = "0x" + from_topic[-40:].lower()
        recipient = "0x" + to_topic[-40:].lower()
        raw_amount = _int_hex(log_entry.get("data"))
        if raw_amount is None:
            continue
        token_address = str(log_entry.get("address") or "").lower()
        token, decimals = metadata.get(token_address, ("unknown", 0))
        amount = raw_amount / (10**decimals) if token != "unknown" else None
        from_wallet = wallet is not None and sender == wallet
        to_wallet = wallet is not None and recipient == wallet
        transfers.append(
            {
                "token": token,
                "token_address": token_address,
                "from_wallet": from_wallet,
                "to_wallet": to_wallet,
                "raw_amount": str(raw_amount),
                "amount": amount,
                "wallet_delta": (
                    (amount or 0.0) * (int(to_wallet) - int(from_wallet))
                    if wallet is not None and (from_wallet or to_wallet)
                    else None
                ),
            }
        )
    return transfers


def enrich_with_rpc(
    report: dict,
    rpc: ReadOnlyRpc,
    wallet_address: Optional[str] = None,
) -> dict:
    """Add receipt, gas, and ERC-20 transfer evidence without changing files."""
    findings = report.setdefault("findings", [])
    rpc_meta = {
        "enabled": True,
        "chain_id": None,
        "receipts_checked": 0,
        "receipts_found": 0,
        "receipts_missing": 0,
        "gas_wei": 0,
        "wallet_token_deltas": {},
    }
    try:
        chain_id = str(rpc.call("eth_chainId", []))
        rpc_meta["chain_id"] = chain_id
        if chain_id.lower() != _BASE_CHAIN_ID:
            findings.append(
                _finding(
                    "critical",
                    "rpc_wrong_chain",
                    "RPC is not Base mainnet",
                    "rpc:eth_chainId",
                )
            )
            report["sources"]["rpc"] = rpc_meta
            return report
    except Exception as error:
        findings.append(
            _finding(
                "critical",
                "rpc_unavailable",
                f"RPC chain check failed: {type(error).__name__}",
                "rpc:eth_chainId",
            )
        )
        report["sources"]["rpc"] = rpc_meta
        return report

    for transaction in report.get("trades", {}).get("transactions", []):
        tx_hash = transaction.get("hash")
        if not isinstance(tx_hash, str):
            continue
        rpc_meta["receipts_checked"] += 1
        try:
            receipt = rpc.receipt(tx_hash)
        except Exception as error:
            findings.append(
                _finding(
                    "warning",
                    "rpc_receipt_unavailable",
                    f"receipt lookup failed: {type(error).__name__}",
                    f"tx:{tx_hash}",
                )
            )
            continue
        if receipt is None:
            rpc_meta["receipts_missing"] += 1
            transaction["rpc_receipt"] = None
            continue
        rpc_meta["receipts_found"] += 1
        status = _int_hex(receipt.get("status"))
        gas_used = _int_hex(receipt.get("gasUsed"))
        gas_price = _int_hex(receipt.get("effectiveGasPrice"))
        gas_wei = (
            gas_used * gas_price
            if gas_used is not None and gas_price is not None
            else None
        )
        if gas_wei is not None:
            rpc_meta["gas_wei"] += gas_wei
        transaction["rpc_receipt"] = {
            "status": status,
            "block_number": _int_hex(receipt.get("blockNumber")),
            "gas_used": gas_used,
            "effective_gas_price": gas_price,
            "gas_wei": gas_wei,
            "log_count": len(receipt.get("logs", []))
            if isinstance(receipt.get("logs"), list)
            else 0,
        }
        transaction["transfers"] = _decode_transfers(receipt, wallet_address)
        for transfer in transaction["transfers"]:
            delta = transfer.get("wallet_delta")
            if delta is None:
                continue
            token = str(transfer.get("token") or "unknown")
            deltas = rpc_meta["wallet_token_deltas"]
            deltas[token] = round(float(deltas.get(token, 0.0)) + delta, 18)
        try:
            chain_transaction = rpc.transaction(tx_hash)
        except Exception as error:
            findings.append(
                _finding(
                    "warning",
                    "rpc_transaction_unavailable",
                    f"transaction lookup failed: {type(error).__name__}",
                    f"tx:{tx_hash}",
                )
            )
        else:
            transaction["rpc_transaction"] = (
                {
                    "from": bool(
                        wallet_address
                        and str(chain_transaction.get("from", "")).lower()
                        == wallet_address.lower()
                    ),
                    "to_present": bool(chain_transaction.get("to")),
                    "nonce": _int_hex(chain_transaction.get("nonce")),
                    "block_number": _int_hex(chain_transaction.get("blockNumber")),
                }
                if chain_transaction is not None
                else None
            )
        if status == 0:
            findings.append(
                _finding(
                    "critical",
                    "rpc_transaction_reverted",
                    "transaction receipt status is reverted",
                    f"tx:{tx_hash}",
                )
            )

    report["sources"]["rpc"] = rpc_meta
    report["trades"]["pnl"]["costs"]["gas_wei"] = rpc_meta["gas_wei"] or None
    report["trades"]["pnl"]["missing_evidence"] = [
        evidence
        for evidence in report["trades"]["pnl"]["missing_evidence"]
        if evidence != "gas_cost_usd"
    ] + ["gas_cost_usd"]
    return report


def source_sha256(path: str) -> Optional[str]:
    """Return a file digest for audit provenance, or None when unavailable."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def load_journal_records(path: str) -> list[dict]:
    """Read an existing SQLite journal without creating or migrating it."""
    target = Path(path)
    if not target.exists():
        return []
    uri = f"file:{target.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY created_at, execution_id"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    records: list[dict] = []
    for row in rows:
        result = dict(row)
        for key in ("metadata_json", "steps_json", "receipt_json", "state_event_json"):
            raw = result.pop(key, None)
            if raw:
                try:
                    result[key.removesuffix("_json")] = json.loads(raw)
                except json.JSONDecodeError:
                    result[key.removesuffix("_json")] = raw
            else:
                result[key.removesuffix("_json")] = None
        records.append(result)
    return records


def build_report(
    entries: list[dict],
    journal_records: list[dict],
    *,
    trades_source: Optional[dict] = None,
    journal_source: Optional[dict] = None,
    since: Optional[str] = None,
) -> dict:
    """Build the complete offline audit report."""
    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "trades": trades_source or {"records": len(entries)},
            "journal": journal_source or {"executions": len(journal_records)},
            "rpc": {"enabled": False, "chain_id": None},
        },
        "cycles": classify_cycles(entries, since),
        "trades": reconcile_trades(entries, journal_records),
    }
    report["coverage"] = report["trades"]["coverage"]
    return report
