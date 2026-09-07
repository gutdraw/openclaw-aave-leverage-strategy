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
