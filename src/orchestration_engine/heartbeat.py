"""Non-TTY progress heartbeat for long-running pipelines.

When ``orch run`` executes in a non-TTY environment (background process, piped
output, cron) there is no progress indication.  This module provides a
``ProgressHeartbeat`` that emits a status line to *stdout* every 30 seconds so
operators can confirm the pipeline is alive without any output-buffering issues.

The heartbeat is *only* active when ``sys.stdout.isatty()`` returns ``False``
(i.e. stdout is not connected to an interactive terminal).  In TTY mode the
existing Rich progress display is used unchanged.

Usage (in ``cli.py``)::

    heartbeat = ProgressHeartbeat(
        total_phases=len(template.phases),
        start_time=run_start,
    )
    heartbeat.start()
    try:
        result = sequencer.execute(initial_input)
    finally:
        heartbeat.stop()

The ``PhaseSequencer`` callbacks ``on_phase_start`` and ``on_phase_complete``
are wired to ``heartbeat.set_current_phase()`` and
``heartbeat.on_phase_complete()`` respectively so the heartbeat always shows
accurate state.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional


class ProgressHeartbeat:
    """Background thread that periodically prints pipeline progress to stdout.

    Designed for non-TTY contexts (piped output, cron, CI).  Emits a line like::

        [2m30s] Running phase 3/7 'fact-check'... (2 completed)

    every *interval_seconds* while the pipeline is running.

    Thread-safety: all mutable state is updated under ``_lock`` so that the
    background thread always sees a consistent snapshot.

    Args:
        total_phases:     Total number of phases in the pipeline.
        start_time:       ``time.time()`` value at pipeline start.
        interval_seconds: How often to emit a heartbeat line (default: 30).
        stream:           Output stream (default: ``sys.stdout``).
        force:            If *True* emit heartbeats even when stdout *is* a
                          terminal.  Intended for testing only.
    """

    def __init__(
        self,
        total_phases: int,
        start_time: Optional[float] = None,
        interval_seconds: float = 30.0,
        stream=None,
        force: bool = False,
    ) -> None:
        self.total_phases = total_phases
        self.start_time: float = start_time if start_time is not None else time.time()
        self.interval_seconds = interval_seconds
        self._stream = stream if stream is not None else sys.stdout
        self._force = force

        # Mutable state — protected by _lock
        self._lock = threading.Lock()
        self._current_phase: str = "starting"
        self._completed: int = 0

        # Thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Decide whether to actually emit heartbeats
        self._active = self._force or not self._stream.isatty()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat background thread.

        No-op when stdout is a terminal (and *force* is False).
        """
        if not self._active:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ProgressHeartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the heartbeat background thread and wait for it to finish."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=self.interval_seconds + 5)
        self._thread = None

    def set_current_phase(self, phase_name: str) -> None:
        """Update the currently executing phase name.

        Should be called from the ``on_phase_start`` callback.
        """
        with self._lock:
            self._current_phase = phase_name

    def on_phase_complete(self) -> None:
        """Increment the completed-phase counter.

        Should be called from the ``on_phase_complete`` callback.
        """
        with self._lock:
            self._completed += 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Background thread body: emit heartbeat every *interval_seconds*."""
        # Wait for the first interval before printing anything — the CLI
        # already prints startup information for the first few seconds.
        self._stop_event.wait(timeout=self.interval_seconds)
        while not self._stop_event.is_set():
            self._emit()
            self._stop_event.wait(timeout=self.interval_seconds)

    def _emit(self) -> None:
        """Format and print a single heartbeat line."""
        with self._lock:
            phase_name = self._current_phase
            completed = self._completed

        elapsed_str = _format_elapsed(time.time() - self.start_time)
        phase_num = completed + 1  # 1-indexed "currently running" phase
        # Clamp to total in case of race between counter increment and display
        phase_num = min(phase_num, self.total_phases)

        line = (
            f"[{elapsed_str}] Running phase {phase_num}/{self.total_phases}"
            f" '{phase_name}'... ({completed} completed)"
        )
        try:
            print(line, file=self._stream, flush=True)
        except Exception:
            # Never crash the pipeline due to I/O errors in the heartbeat
            pass

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ProgressHeartbeat":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_elapsed(seconds: float) -> str:
    """Convert *seconds* to a compact human-readable string.

    Examples::

        _format_elapsed(45)     → '45s'
        _format_elapsed(90)     → '1m30s'
        _format_elapsed(3661)   → '1h1m1s'
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"
