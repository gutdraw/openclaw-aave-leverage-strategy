"""Crash-safe execution journal for live transaction state.

The JSONL trade log is intentionally kept as a human-readable audit export.  This
module is the durable boundary around a live transaction: an execution is written
before broadcast, updated after each broadcast/receipt, and only marked complete
after the corresponding trade event has been persisted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ExecutionJournal:
    """A small SQLite state machine for transaction recovery.

    SQLite is used instead of trying to make a multi-step JSONL append atomic.
    WAL plus ``synchronous=FULL`` keeps the journal durable across process loss,
    while the unique execution id prevents a recovered action from being recorded
    twice.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    position_id TEXT,
                    direction TEXT,
                    status TEXT NOT NULL,
                    tx_hash TEXT,
                    nonce INTEGER,
                    last_step INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    receipt_json TEXT,
                    state_event_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_executions_recovery
                    ON executions(status, state_event_json);
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(executions)").fetchall()
            }
            if "steps_json" not in columns:
                conn.execute(
                    "ALTER TABLE executions ADD COLUMN steps_json TEXT NOT NULL DEFAULT '[]'"
                )

    def prepare(
        self,
        action: str,
        position_id: Optional[str] = None,
        direction: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        execution_id = uuid.uuid4().hex
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO executions (
                    execution_id, created_at, updated_at, action, position_id,
                    direction, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)
                """,
                (
                    execution_id,
                    now,
                    now,
                    action,
                    position_id,
                    direction,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
        return execution_id

    def mark_broadcast(
        self,
        execution_id: str,
        tx_hash: str,
        step_index: int,
        nonce: Optional[int] = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT steps_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            steps = self._decode_steps(row["steps_json"] if row else None)
            steps.append(
                {
                    "step_index": step_index,
                    "tx_hash": tx_hash,
                    "nonce": nonce,
                    "broadcast_at": _now(),
                }
            )
            conn.execute(
                """
                UPDATE executions
                   SET status = ?, tx_hash = ?, last_step = ?, nonce = ?,
                       steps_json = ?, updated_at = ?
                 WHERE execution_id = ?
                """,
                (
                    "broadcast",
                    tx_hash,
                    step_index,
                    nonce,
                    json.dumps(steps, sort_keys=True, default=str),
                    _now(),
                    execution_id,
                ),
            )

    def mark_step_confirmed(
        self, execution_id: str, tx_hash: str, receipt: dict
    ) -> None:
        """Attach one transaction receipt without closing the execution."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT steps_json FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            steps = self._decode_steps(row["steps_json"] if row else None)
            for step in reversed(steps):
                if step.get("tx_hash") == tx_hash:
                    step["confirmed_at"] = _now()
                    step["receipt"] = receipt
                    break
            conn.execute(
                "UPDATE executions SET steps_json = ?, updated_at = ? WHERE execution_id = ?",
                (
                    json.dumps(steps, sort_keys=True, default=str),
                    _now(),
                    execution_id,
                ),
            )

    def mark_confirmed(
        self,
        execution_id: str,
        tx_hash: Optional[str] = None,
        receipt: Optional[dict] = None,
    ) -> None:
        if tx_hash and receipt is not None:
            self.mark_step_confirmed(execution_id, tx_hash, receipt)
        self._update(
            execution_id,
            status="confirmed",
            tx_hash=tx_hash,
            receipt_json=json.dumps(receipt, default=str, sort_keys=True)
            if receipt is not None
            else None,
        )

    def mark_failed(self, execution_id: str, error: str) -> None:
        """Mark a never-broadcast action failed, or a sent action unknown."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tx_hash FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            status = "unknown" if row and row["tx_hash"] else "failed"
            conn.execute(
                """
                UPDATE executions
                   SET status = ?, updated_at = ?, error = ?
                 WHERE execution_id = ?
                """,
                (status, _now(), error[:2000], execution_id),
            )

    def mark_reverted(
        self,
        execution_id: str,
        error: str = "transaction reverted",
        receipt: Optional[dict] = None,
    ) -> None:
        """Record a mined revert as terminal, while retaining its receipt."""
        self._update(
            execution_id,
            status="reverted",
            receipt_json=(
                json.dumps(receipt, default=str, sort_keys=True)
                if receipt is not None
                else None
            ),
            error=error[:2000],
        )

    def mark_state_recorded(
        self, execution_id: str, event: Optional[dict] = None
    ) -> None:
        self._update(
            execution_id,
            status="complete",
            state_event_json=json.dumps(event or {}, sort_keys=True, default=str),
        )

    def _update(self, execution_id: str, **values: object) -> None:
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [values[key] for key in values]
        params.append(execution_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE executions SET {assignments} WHERE execution_id = ?",
                params,
            )

    def get(self, execution_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def recoverable(self) -> list[dict]:
        """Return actions that require receipt/state reconciliation on startup."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM executions
                 WHERE status IN ('prepared', 'broadcast', 'unknown', 'confirmed')
                   AND state_event_json IS NULL
                 ORDER BY created_at
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def recent_failures(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM executions
                 WHERE status IN ('failed', 'unknown', 'reverted')
                 ORDER BY updated_at DESC
                 LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def all(self) -> list[dict]:
        """Return all journal rows in creation order for read-only audits."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY created_at, execution_id"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _decode_steps(raw: object) -> list[dict]:
        if not raw:
            return []
        try:
            decoded = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(decoded, list):
            return []
        return [step for step in decoded if isinstance(step, dict)]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        for key in (
            "metadata_json",
            "steps_json",
            "receipt_json",
            "state_event_json",
        ):
            raw = result.pop(key, None)
            if raw:
                try:
                    result[key.removesuffix("_json")] = json.loads(raw)
                except json.JSONDecodeError:
                    result[key.removesuffix("_json")] = raw
            else:
                result[key.removesuffix("_json")] = None
        return result


@contextmanager
def journal_execution(
    journal: Optional[ExecutionJournal],
    action: str,
    position_id: Optional[str] = None,
    direction: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Iterator[tuple[Optional[str], object]]:
    """Create an execution and yield its id plus a broadcast callback.

    The callback is intentionally tiny: the signer invokes it immediately after
    every successful broadcast, so a later exception is recorded as ``unknown``
    instead of being mistaken for a safe failure.
    """
    if journal is None:
        yield None, lambda _step, _tx_hash, _nonce=None: None
        return

    execution_id = journal.prepare(action, position_id, direction, metadata)

    def on_broadcast(
        step_index: int, tx_hash: str, nonce: Optional[int] = None
    ) -> None:
        journal.mark_broadcast(execution_id, tx_hash, step_index, nonce)

    try:
        yield execution_id, on_broadcast
    except BaseException as exc:
        journal.mark_failed(execution_id, repr(exc))
        raise
