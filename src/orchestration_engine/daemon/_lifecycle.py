"""Daemon process lifecycle helpers.

PID-file management, liveness probing, fatal-exit, and log-file bootstrap for
the background daemon process.  Extracted verbatim from
:mod:`orchestration_engine.daemon` (wave a of #1034); the public surface is
re-exported by the package facade, so callers continue to import these names
from ``orchestration_engine.daemon``.
"""

# ruff: noqa: E501

import logging
import os
import sys
from pathlib import Path
from typing import Any

from ..timestamps import now_utc

logger = logging.getLogger(__name__)


def _write_pid_file(output_dir: Path) -> Path:
    """Write current PID to output_dir/.orch-daemon.pid and return the path."""
    pid_path = output_dir / ".orch-daemon.pid"
    pid_path.write_text(str(os.getpid()))
    return pid_path


def _remove_pid_file(pid_path: Path) -> None:
    """Remove the PID file, ignoring errors."""
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def is_process_alive(pid: int) -> bool:
    """Return True if a process with *pid* is running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it.
        return True


def _fail(db: Any, run_id: str, pid_path: Path, message: str) -> None:
    """Mark run as failed and exit."""
    logger.error("FAIL: %s", message)
    try:
        db.update_pipeline_run(
            run_id,
            status="failed",
            completed_at=now_utc().isoformat(),
            error_message=message,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update DB on fail: %s", exc)
    _remove_pid_file(pid_path)
    sys.exit(1)


def _setup_logging(log_path: Path) -> None:
    """Configure root logger to write to the daemon log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
