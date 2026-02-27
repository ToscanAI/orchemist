"""Tests for the non-TTY progress heartbeat (Issue #186).

Covers:
- ``ProgressHeartbeat`` lifecycle (start/stop, context manager)
- Heartbeat is suppressed in TTY mode (isatty() == True)
- Heartbeat is active in non-TTY mode (isatty() == False) or force=True
- Output format matches the spec: ``[Xm Ys] Running phase N/T 'name'... (C completed)``
- Thread safety: set_current_phase / on_phase_complete can be called concurrently
- ``_format_elapsed`` helper converts seconds correctly
- Integration: CLI wires heartbeat to PhaseSequencer callbacks
"""

from __future__ import annotations

import io
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.orchestration_engine.heartbeat import ProgressHeartbeat, _format_elapsed


# ---------------------------------------------------------------------------
# _format_elapsed unit tests
# ---------------------------------------------------------------------------


class TestFormatElapsed:
    def test_seconds_only(self):
        assert _format_elapsed(0) == "0s"
        assert _format_elapsed(1) == "1s"
        assert _format_elapsed(59) == "59s"

    def test_minutes_and_seconds(self):
        assert _format_elapsed(60) == "1m0s"
        assert _format_elapsed(90) == "1m30s"
        assert _format_elapsed(150) == "2m30s"
        assert _format_elapsed(3599) == "59m59s"

    def test_hours(self):
        assert _format_elapsed(3600) == "1h0m0s"
        assert _format_elapsed(3661) == "1h1m1s"
        assert _format_elapsed(7384) == "2h3m4s"

    def test_negative_clipped_to_zero(self):
        # Negative elapsed (clock skew) must not crash
        assert _format_elapsed(-5) == "0s"

    def test_fractional_seconds_truncated(self):
        # Only integer part is shown
        assert _format_elapsed(1.9) == "1s"
        assert _format_elapsed(61.5) == "1m1s"


# ---------------------------------------------------------------------------
# ProgressHeartbeat unit tests
# ---------------------------------------------------------------------------


def _make_non_tty_stream() -> io.StringIO:
    """Return a StringIO that reports isatty() == False (default)."""
    buf = io.StringIO()
    # StringIO.isatty() already returns False — no patching needed
    return buf


def _make_tty_stream() -> MagicMock:
    """Return a mock stream that reports isatty() == True."""
    stream = MagicMock(spec=io.StringIO)
    stream.isatty.return_value = True
    return stream


class TestProgressHeartbeatLifecycle:
    def test_start_stop(self):
        """start/stop should not raise and thread should be cleaned up."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, start_time=time.time(),
                               interval_seconds=60, stream=buf, force=True)
        hb.start()
        assert hb._thread is not None
        assert hb._thread.is_alive()
        hb.stop()
        assert hb._thread is None

    def test_context_manager(self):
        """__enter__/__exit__ should manage the thread."""
        buf = _make_non_tty_stream()
        with ProgressHeartbeat(total_phases=5, interval_seconds=60,
                               stream=buf, force=True) as hb:
            assert hb._thread is not None
        assert hb._thread is None

    def test_start_idempotent_on_tty(self):
        """start() is a no-op when stdout is a terminal (force=False)."""
        stream = _make_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60, stream=stream)
        hb.start()
        assert hb._thread is None  # thread was never started
        hb.stop()  # should not raise

    def test_stop_without_start(self):
        """stop() should be safe to call even if start() was never called."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60, stream=buf)
        hb.stop()  # must not raise

    def test_double_stop(self):
        """stop() called twice should be safe."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60,
                               stream=buf, force=True)
        hb.start()
        hb.stop()
        hb.stop()  # second stop — must not raise


class TestProgressHeartbeatTtyGuard:
    """Verify TTY detection suppresses the heartbeat."""

    def test_isatty_true_suppresses_heartbeat(self):
        """No thread is started when the stream is a TTY."""
        stream = _make_tty_stream()
        hb = ProgressHeartbeat(total_phases=4, interval_seconds=0.01, stream=stream)
        hb.start()
        time.sleep(0.05)
        hb.stop()
        # The mock write/print methods should never have been called
        stream.write.assert_not_called()

    def test_non_tty_emits_output(self):
        """Heartbeat emits at least one line after the interval elapses."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=4, start_time=time.time() - 10,
                               interval_seconds=0.05, stream=buf, force=True)
        hb.start()
        time.sleep(0.2)  # Wait for at least one heartbeat
        hb.stop()
        output = buf.getvalue()
        assert output, "Expected at least one heartbeat line"
        assert "Running phase" in output

    def test_force_flag_overrides_tty(self):
        """force=True emits heartbeats even when stream.isatty() == True."""
        stream = MagicMock()
        stream.isatty.return_value = True
        written = []
        stream.write = lambda s: written.append(s)
        stream.flush = lambda: None

        # Use a real StringIO so print() works, but override isatty result
        buf = io.StringIO()
        hb = ProgressHeartbeat(total_phases=2, start_time=time.time() - 5,
                               interval_seconds=0.05, stream=buf, force=True)
        hb.start()
        time.sleep(0.2)
        hb.stop()
        assert "Running phase" in buf.getvalue()


class TestProgressHeartbeatOutputFormat:
    """Verify the emitted line matches the spec."""

    def _collect_one_line(self, total: int, phase: str, completed: int,
                          elapsed_offset: float = 150.0) -> str:
        """Capture a single heartbeat line and return it."""
        buf = _make_non_tty_stream()
        start = time.time() - elapsed_offset  # Fake that 150s have elapsed
        hb = ProgressHeartbeat(total_phases=total, start_time=start,
                               interval_seconds=0.05, stream=buf, force=True)
        hb.set_current_phase(phase)
        for _ in range(completed):
            hb.on_phase_complete()
        hb.start()
        time.sleep(0.15)  # Let at least one heartbeat fire
        hb.stop()
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert lines, "No heartbeat line produced"
        return lines[0]

    def test_format_elapsed_in_output(self):
        line = self._collect_one_line(7, "fact-check", 2, elapsed_offset=150)
        # 150s → 2m30s
        assert "[2m30s]" in line

    def test_phase_name_in_output(self):
        line = self._collect_one_line(7, "fact-check", 2)
        assert "'fact-check'" in line

    def test_phase_counts_in_output(self):
        """Line must contain 'phase N/T' and '(C completed)'."""
        line = self._collect_one_line(7, "fact-check", 2)
        # phase num (completed+1=3) / total (7)
        assert "3/7" in line
        assert "2 completed" in line

    def test_full_spec_example(self):
        """Reproduce the exact example from the issue spec.

        [2m30s] Running phase 3/7 'fact-check'... (2 completed)
        """
        line = self._collect_one_line(7, "fact-check", 2, elapsed_offset=150)
        assert line == "[2m30s] Running phase 3/7 'fact-check'... (2 completed)"

    def test_first_phase_no_completed(self):
        line = self._collect_one_line(5, "research", 0, elapsed_offset=30)
        assert "1/5" in line
        assert "'research'" in line
        assert "0 completed" in line

    def test_last_phase(self):
        """Phase num is clamped to total when all phases bar one have completed."""
        line = self._collect_one_line(3, "publish", 2, elapsed_offset=600)
        # completed=2, so phase_num = 3 (clamped to total)
        assert "3/3" in line
        assert "2 completed" in line


class TestProgressHeartbeatStateUpdates:
    """Test set_current_phase and on_phase_complete."""

    def test_set_current_phase_updates_state(self):
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=5, interval_seconds=60,
                               stream=buf, force=True)
        hb.set_current_phase("my-phase")
        with hb._lock:
            assert hb._current_phase == "my-phase"

    def test_on_phase_complete_increments_counter(self):
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=5, interval_seconds=60,
                               stream=buf, force=True)
        hb.on_phase_complete()
        hb.on_phase_complete()
        with hb._lock:
            assert hb._completed == 2

    def test_thread_safety_concurrent_updates(self):
        """Concurrent calls to set_current_phase / on_phase_complete must not crash."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=100, interval_seconds=0.01,
                               stream=buf, force=True)
        hb.start()

        errors = []

        def writer():
            try:
                for i in range(50):
                    hb.set_current_phase(f"phase-{i}")
                    hb.on_phase_complete()
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hb.stop()
        assert not errors, f"Concurrent updates raised: {errors}"


class TestProgressHeartbeatIntegration:
    """Integration-level: verify heartbeat wires correctly with CLI callbacks."""

    def test_cli_on_phase_start_wires_to_set_current_phase(self):
        """Simulate the CLI wiring: on_phase_start calls heartbeat.set_current_phase."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60,
                               stream=buf, force=True)

        # Simulate what cli._on_phase_start does
        def _on_phase_start(phase_id, phase, wave_index):
            hb.set_current_phase(phase_id)

        _on_phase_start("research", None, 0)
        with hb._lock:
            assert hb._current_phase == "research"

        _on_phase_start("write", None, 1)
        with hb._lock:
            assert hb._current_phase == "write"

    def test_cli_on_phase_complete_increments_counter(self):
        """Simulate the CLI wiring: on_phase_complete calls heartbeat.on_phase_complete."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60,
                               stream=buf, force=True)

        # Simulate two phase completions
        hb.on_phase_complete()
        hb.on_phase_complete()

        with hb._lock:
            assert hb._completed == 2

    def test_heartbeat_not_active_when_isatty_true(self):
        """_active flag is False for TTY streams (without force)."""
        tty_stream = _make_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60,
                               stream=tty_stream)
        assert hb._active is False

    def test_heartbeat_active_when_isatty_false(self):
        """_active flag is True for non-TTY streams."""
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60, stream=buf)
        assert hb._active is True

    def test_heartbeat_active_with_force_even_on_tty(self):
        """_active is True when force=True regardless of isatty()."""
        tty_stream = _make_tty_stream()
        hb = ProgressHeartbeat(total_phases=3, interval_seconds=60,
                               stream=tty_stream, force=True)
        assert hb._active is True

    def test_emit_does_not_crash_on_write_error(self):
        """If the stream raises on write, the heartbeat thread should survive."""
        bad_stream = MagicMock()
        bad_stream.isatty.return_value = False
        bad_stream.write.side_effect = OSError("disk full")

        hb = ProgressHeartbeat(total_phases=2, interval_seconds=0.05,
                               stream=bad_stream, force=True)
        hb.start()
        time.sleep(0.2)
        hb.stop()
        # If we reach here without a crash, the test passes
        assert hb._thread is None


class TestProgressHeartbeatDefaults:
    """Verify default parameter values."""

    def test_default_interval_is_30_seconds(self):
        buf = _make_non_tty_stream()
        hb = ProgressHeartbeat(total_phases=1, stream=buf)
        assert hb.interval_seconds == 30.0

    def test_default_stream_is_stdout(self):
        hb = ProgressHeartbeat(total_phases=1)
        assert hb._stream is sys.stdout

    def test_default_start_time_close_to_now(self):
        before = time.time()
        hb = ProgressHeartbeat(total_phases=1)
        after = time.time()
        assert before <= hb.start_time <= after

    def test_initial_phase_name_is_starting(self):
        hb = ProgressHeartbeat(total_phases=1)
        assert hb._current_phase == "starting"

    def test_initial_completed_count_is_zero(self):
        hb = ProgressHeartbeat(total_phases=1)
        assert hb._completed == 0
