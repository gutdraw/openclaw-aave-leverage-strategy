"""Exit non-zero when the bot heartbeat or execution journal needs attention."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.journal import ExecutionJournal

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--max-age", type=float, default=3900)
    args = parser.parse_args()

    heartbeat_path = Path(args.heartbeat)
    if not heartbeat_path.exists():
        log.error("unhealthy: heartbeat missing: %s", heartbeat_path)
        return 2
    try:
        heartbeat = json.loads(heartbeat_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("unhealthy: heartbeat unreadable: %s", exc)
        return 2

    age = time.time() - heartbeat_path.stat().st_mtime
    if age > args.max_age:
        log.error("unhealthy: heartbeat age %.0fs exceeds %.0fs", age, args.max_age)
        return 2
    if heartbeat.get("status") == "error":
        log.error("unhealthy: last cycle failed: %s", heartbeat.get("error", "unknown"))
        return 2

    journal_path = Path(args.journal)
    if not journal_path.exists():
        log.error("unhealthy: execution journal missing: %s", journal_path)
        return 2
    unresolved = ExecutionJournal(args.journal).recoverable()
    if unresolved:
        ids = ", ".join(str(row.get("execution_id")) for row in unresolved)
        log.error("unhealthy: unresolved executions: %s", ids)
        return 2

    log.info(
        "healthy: "
        f"last_decision={heartbeat.get('last_decision')} "
        f"heartbeat_age={age:.0f}s"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
