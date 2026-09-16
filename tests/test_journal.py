from pathlib import Path

from bot.journal import ExecutionJournal, journal_execution


def test_journal_requires_state_recording_after_confirmation(tmp_path: Path) -> None:
    journal = ExecutionJournal(str(tmp_path / "executions.sqlite3"))

    with journal_execution(journal, "open", "cbBTC/USDC", "long") as (
        execution_id_2,
        on_broadcast,
    ):
        assert execution_id_2
        on_broadcast(0, "0xabc", 17)
        journal.mark_confirmed(execution_id_2, "0xabc", {"status": 1})

    assert journal.recoverable()[0]["tx_hash"] == "0xabc"
    journal.mark_state_recorded(execution_id_2, {"type": "trade", "action": "open"})
    assert journal.recoverable() == []

    failed_id = journal.prepare("open", "cbBTC/USDC", "long", {"amount": 1})
    journal.mark_broadcast(failed_id, "0xdef", 1)
    journal.mark_failed(failed_id, "process interrupted")
    assert journal.get(failed_id)["status"] == "unknown"


def test_journal_records_failed_prepare_without_transaction(tmp_path: Path) -> None:
    journal = ExecutionJournal(str(tmp_path / "executions.sqlite3"))
    execution_id = journal.prepare("close", "cbBTC/USDC", "long")
    journal.mark_failed(execution_id, "quote unavailable")

    row = journal.get(execution_id)
    assert row is not None
    assert row["status"] == "failed"
    assert journal.recoverable() == []


def test_journal_preserves_all_broadcast_steps_and_receipts(tmp_path: Path) -> None:
    journal = ExecutionJournal(str(tmp_path / "executions.sqlite3"))
    execution_id = journal.prepare("open", "cbBTC/USDC", "long")
    first_hash = "a" * 64
    second_hash = "b" * 64

    journal.mark_broadcast(execution_id, first_hash, 0, 12)
    journal.mark_step_confirmed(execution_id, first_hash, {"status": 1})
    journal.mark_broadcast(execution_id, second_hash, 1, 13)
    journal.mark_step_confirmed(execution_id, second_hash, {"status": 1})
    journal.mark_confirmed(execution_id, second_hash, {"status": 1})

    row = journal.get(execution_id)
    assert row is not None
    assert row["tx_hash"] == second_hash
    assert [step["tx_hash"] for step in row["steps"]] == [first_hash, second_hash]
    assert all(step["receipt"]["status"] == 1 for step in row["steps"])


def test_journal_marks_mined_revert_terminal_and_keeps_receipt(tmp_path: Path) -> None:
    journal = ExecutionJournal(str(tmp_path / "executions.sqlite3"))
    execution_id = journal.prepare("close", "cbBTC/USDC", "long")
    tx_hash = "c" * 64
    journal.mark_broadcast(execution_id, tx_hash, 0)
    journal.mark_reverted(execution_id, receipt={"status": 0})

    row = journal.get(execution_id)
    assert row is not None
    assert row["status"] == "reverted"
    assert row["receipt"]["status"] == 0
    assert journal.recoverable() == []
    assert journal.recent_failures()[0]["status"] == "reverted"
