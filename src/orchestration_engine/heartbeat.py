"""Non-TTY progress heartbeat for long-running pipelines.

When ``orch run`` executes in a non-TTY environment (background process, piped
output, cron) there is no progress indication.  This module provides a
``ProgressHeartbeat`` that emits a status line to *stdout* every 30 seconds so
operators can confirm the pipeline is alive without any output-buffering issues.

The heartbeat is *only* active when ``sys.stdout.isatty()`` returns ``False``
(i.e. stdout is not connected to an interactive terminal).  In TTY mode the
existing Rich progress display is used unchanged.

Parallel phase support (Issue #102)
-------------------------------------
``ProgressHeartbeat`` now tracks a **set** of concurrently active phase names
(``active_phases``) rather than a single ``current_phase`` string.  The legacy
``set_current_phase(name)`` API still works — it adds the name to the set —
and a new ``remove_active_phase(name)`` API removes a phase once it completes.

The :meth:`on_phase_complete` method accepts an optional ``phase_name``
parameter to remove the specific phase from the active set.  When called
without a name (backward-compat), the set is **not** modified (the legacy
behaviour of just incrementing the counter is preserved).

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
from typing import FrozenSet, Optional, Set


class ProgressHeartbeat:
    """Background thread that periodically prints pipeline progress to stdout.

    Designed for non-TTY contexts (piped output, cron, CI).  Emits a line like::

        [2m30s] Running phases 3/7 'fact-check, edit'... (2 completed)

    every *interval_seconds* while the pipeline is running.

    With parallel execution the output shows **all active phases**::

        [1m5s] Running phases 5/10 'write, fact-check, review'... (4 completed)

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
        self._current_phase: str = "starting"  # legacy compat
        self._active_phases: Set[str] = set()  # Issue #102: multi-phase support
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
        """Record that *phase_name* has started executing.

        Backward-compatible API: in the original implementation this replaced
        ``_current_phase`` with a single string.  In the parallel-aware
        implementation it **adds** the name to the ``active_phases`` set so
        multiple concurrent phases are tracked simultaneously.

        Should be called from the ``on_phase_start`` callback.

        Args:
            phase_name: Display name of the phase that just started.
        """
        with self._lock:
            self._current_phase = phase_name  # legacy field (keep for compat)
            self._active_phases.add(phase_name)

    def remove_active_phase(self, phase_name: str) -> None:
        """Remove *phase_name* from the set of active phases.

        Should be called when a phase completes (success or failure) to keep
        the ``active_phases`` display accurate during parallel execution.

        Args:
            phase_name: Display name of the phase that just finished.
        """
        with self._lock:
            self._active_phases.discard(phase_name)

    def on_phase_complete(self, phase_name: Optional[str] = None) -> None:
        """Increment the completed-phase counter and optionally deregister the phase.

        Should be called from the ``on_phase_complete`` callback.

        Args:
            phase_name: If provided, also removes the phase from
                        ``active_phases`` (useful when the caller passes the
                        phase ID here instead of calling
                        :meth:`remove_active_phase` separately).  When
                        ``None`` (the backward-compatible default) the active
                        set is **not** modified — only the counter is
                        incremented.
        """
        with self._lock:
            self._completed += 1
            if phase_name is not None:
                self._active_phases.discard(phase_name)

    @property
    def active_phases(self) -> FrozenSet[str]:
        """Return an immutable snapshot of the currently active phase names.

        Thread-safe: takes the internal lock so callers always see a
        consistent set (no torn reads from concurrent ``set_current_phase``
        or ``remove_active_phase`` calls).

        Returns:
            A :class:`frozenset` of phase name strings.  Empty when no phases
            are currently running.
        """
        with self._lock:
            return frozenset(self._active_phases)

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
        """Format and print a single heartbeat line.

        When multiple phases are active (parallel execution) the line lists
        all of them::

            [1m5s] Running phases 5/10 'write, fact-check, review'... (4 completed)

        With only one active phase (or no phase tracked yet) the output is
        identical to the original format::

            [2m30s] Running phase 3/7 'fact-check'... (2 completed)
        """
        with self._lock:
            active = frozenset(self._active_phases)
            completed = self._completed
            legacy_name = self._current_phase

        elapsed_str = _format_elapsed(time.time() - self.start_time)
        phase_num = completed + 1  # 1-indexed "currently running" phase
        # Clamp to total in case of race between counter increment and display
        phase_num = min(phase_num, self.total_phases)

        if active:
            # Sort for deterministic output
            names = ", ".join(sorted(active))
            phase_word = "phases" if len(active) > 1 else "phase"
        else:
            # Fallback to legacy _current_phase when active set is empty
            names = legacy_name
            phase_word = "phase"

        line = (
            f"[{elapsed_str}] Running {phase_word} {phase_num}/{self.total_phases}"
            f" '{names}'... ({completed} completed)"
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
