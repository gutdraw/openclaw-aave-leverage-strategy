"""Stable runtime provenance for bot heartbeats and audit reports."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Optional

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_PROCESS_STARTED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit(repo_root: Path) -> str:
    """Return the checked-out commit without exposing repository contents."""
    configured = os.environ.get("OPENCLAW_COMMIT_SHA", "").strip().lower()
    if _COMMIT_RE.fullmatch(configured):
        return configured
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip().lower()
    return commit if _COMMIT_RE.fullmatch(commit) else "unknown"


def _config_sha256(config_path: str) -> str:
    """Hash the loaded config for identity checks without persisting secrets."""
    try:
        digest = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    except OSError:
        return "unavailable"
    return digest


def build_runtime_provenance(
    config_path: Optional[str],
    repo_root: Optional[Path] = None,
    pid: Optional[int] = None,
) -> dict[str, object]:
    """Build immutable process, code, and config identity metadata."""
    root = repo_root or Path(__file__).resolve().parents[1]
    return {
        "schema_version": 1,
        "process_started_at": _PROCESS_STARTED_AT,
        "pid": pid if pid is not None else os.getpid(),
        "code_commit": _git_commit(root),
        "config_sha256": _config_sha256(config_path) if config_path else "unavailable",
    }
