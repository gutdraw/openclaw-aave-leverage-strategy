"""Atomic bot heartbeat writer used by process supervision and alerting."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write(path: str, payload: dict) -> None:
    """Atomically replace a heartbeat file and make the rename durable."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems do not allow directory fsync; the file rename is
            # still atomic and the monitor can use its mtime as a fallback.
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
