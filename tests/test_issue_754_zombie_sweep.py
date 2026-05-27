"""Acceptance tests for #754 — sweep zombie pipeline runs.

When a daemon process dies abnormally (crash, OOM, manual kill, host
reboot), the corresponding ``pipeline_runs`` row is left with a
non-terminal status (typically ``'running'``) and the row's ``pid``
column points at a dead PID. These zombies count against the
``ORCH_MAX_DAEMONS=8`` backpressure cap shipped in #839, eventually
wedging the launch path with HTTP 429 even when no daemon is alive.

These tests verify the new ``Database.sweep_zombie_runs()`` method
correctly:
  1. Transitions dead-PID rows to ``status='crashed'`` with a
     diagnostic ``error_message`` and a ``completed_at`` timestamp.
  2. Leaves live-PID rows untouched.
  3. Is idempotent — re-running on the same state changes nothing.
  4. Never touches rows whose status is already terminal.
  5. Handles NULL ``pid`` columns by falling back to the PID file at
     ``<output_dir>/.orch-daemon.pid``.
  6. Sweeps ``pending_review`` rows whose daemons died, not just
     ``running``.
  7. Contains exceptions per-row (one bad row does not crash the sweep).
  8. Is wired into ``count_active_pipeline_runs()`` so the backpressure
     cap reflects post-sweep reality.
  9. Documents the PID-reuse trade-off in its docstring (per spec §5.1).
 10. Runs once on FastAPI startup and logs the result (INFO if N>=1,
     DEBUG if N==0).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest

from tests.conftest import read_src


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# #863: ``fresh_db`` retained as a thin alias of the canonical ``db`` fixture.
from tests._helpers import insert_pipeline_run as _insert_pipeline_run_helper


@pytest.fixture
def fresh_db(db):
    """A fresh, on-disk SQLite Database for the test."""
    return db


def _dead_pid() -> int:
    """Return a PID that is guaranteed to be dead.

    We spawn a child via ``os.fork()`` (or fallback to a subprocess that
    exits immediately) and wait for it to exit, then return its PID.
    The kernel may not have reused that PID by the time the test runs
    its assertion (PID reuse is unlikely on a workstation in the test
    interval), but if it does the test asserts a documented escape:
    the row is left untouched (A.14).
    """
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()  # ensure the child has exited
    return proc.pid


def _live_pid() -> int:
    """Return the current process's PID — guaranteed alive for the
    duration of the test."""
    return os.getpid()


def _insert_run(
    db,
    status: str,
    run_id: str,
    pid=None,
    output_dir: str = "/tmp/orch-test",
):
    """Insert a minimal ``pipeline_runs`` row.

    The schema requires ``output_dir`` (NOT NULL). When ``pid`` is
    omitted, the column is left NULL — exercising the PID-file fallback
    path.
    """
    # #862: route through the canonical helper but keep this file's
    # historical defaults (template_id="tpl", output_dir="/tmp/orch-test")
    # so the contract under test does not shift.
    _insert_pipeline_run_helper(
        db,
        run_id=run_id,
        status=status,
        pid=pid,
        template_id="tpl",
        output_dir=output_dir,
    )


# ---------------------------------------------------------------------------
# A.1 — Dead PID → crashed with diagnostic message
# ---------------------------------------------------------------------------


class TestDeadPidSwept:
    """Behavioral contract A.1: dead-PID rows transition to 'crashed'
    with an error_message containing the canonical phrase AND the PID."""

    def test_dead_pid_row_becomes_crashed(self, fresh_db):
        dead = _dead_pid()
        _insert_run(fresh_db, "running", "zomb0001", pid=dead)

        n = fresh_db.sweep_zombie_runs()

        assert n >= 1
        row = fresh_db.get_pipeline_run("zomb0001")
        assert row is not None
        assert row["status"] == "crashed"
        assert row["error_message"] is not None
        assert "daemon process exited without updating status" in row["error_message"]
        assert str(dead) in row["error_message"]
        assert row["completed_at"] is not None


# ---------------------------------------------------------------------------
# A.2 — Live PID → row preserved
# ---------------------------------------------------------------------------


class TestLivePidPreserved:
    """Behavioral contract A.2: live-PID rows are byte-identical
    after sweep."""

    def test_live_pid_row_unchanged(self, fresh_db):
        live = _live_pid()
        _insert_run(fresh_db, "running", "live0001", pid=live)

        before = fresh_db.get_pipeline_run("live0001")
        fresh_db.sweep_zombie_runs()
        after = fresh_db.get_pipeline_run("live0001")

        assert after["status"] == before["status"] == "running"
        assert after["error_message"] == before["error_message"]
        assert after["completed_at"] == before["completed_at"]


# ---------------------------------------------------------------------------
# A.3 — Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Behavioral contract A.3: re-sweep returns 0 and changes nothing."""

    def test_second_sweep_is_noop(self, fresh_db):
        dead = _dead_pid()
        _insert_run(fresh_db, "running", "idem0001", pid=dead)

        n1 = fresh_db.sweep_zombie_runs()
        assert n1 >= 1

        # Snapshot the swept row
        row_after_first = fresh_db.get_pipeline_run("idem0001")

        n2 = fresh_db.sweep_zombie_runs()
        assert n2 == 0

        row_after_second = fresh_db.get_pipeline_run("idem0001")
        # Status, error_message, completed_at unchanged by second sweep
        assert row_after_second["status"] == row_after_first["status"]
        assert row_after_second["error_message"] == row_after_first["error_message"]
        assert row_after_second["completed_at"] == row_after_first["completed_at"]


# ---------------------------------------------------------------------------
# A.4 — Terminal states never scanned (set-equality check)
# ---------------------------------------------------------------------------


class TestTerminalStatesPreserved:
    """Behavioral contract A.4: rows in TERMINAL_STATUSES minus
    pending_review are never touched, even with dead PIDs."""

    @pytest.mark.parametrize("terminal", [
        "success",
        "failed",
        "cancelled",
        "crashed",
        "scoring_failed",
        "rejected",
        "escalated",
    ])
    def test_terminal_status_with_dead_pid_unchanged(self, fresh_db, terminal):
        dead = _dead_pid()
        _insert_run(fresh_db, terminal, f"term{terminal[:4]}", pid=dead)

        before = fresh_db.get_pipeline_run(f"term{terminal[:4]}")
        fresh_db.sweep_zombie_runs()
        after = fresh_db.get_pipeline_run(f"term{terminal[:4]}")

        assert after["status"] == before["status"] == terminal
        assert after["error_message"] == before["error_message"]
        assert after["completed_at"] == before["completed_at"]


# ---------------------------------------------------------------------------
# A.5 — NULL pid + missing PID file → crashed with 'no PID recorded'
# ---------------------------------------------------------------------------


class TestMissingPidFile:
    """Behavioral contract A.5: NULL pid AND no resolvable PID file."""

    def test_null_pid_no_file_swept(self, fresh_db, tmp_path):
        outdir = tmp_path / "run-out-no-file"
        outdir.mkdir()
        _insert_run(fresh_db, "running", "nopid001", pid=None,
                    output_dir=str(outdir))

        n = fresh_db.sweep_zombie_runs()

        assert n >= 1
        row = fresh_db.get_pipeline_run("nopid001")
        assert row["status"] == "crashed"
        assert "no PID recorded" in (row["error_message"] or "")
        assert row["completed_at"] is not None

    def test_null_pid_empty_file_swept(self, fresh_db, tmp_path):
        outdir = tmp_path / "run-out-empty"
        outdir.mkdir()
        (outdir / ".orch-daemon.pid").write_text("")
        _insert_run(fresh_db, "running", "nopid002", pid=None,
                    output_dir=str(outdir))

        n = fresh_db.sweep_zombie_runs()

        assert n >= 1
        row = fresh_db.get_pipeline_run("nopid002")
        assert row["status"] == "crashed"
        assert "no PID recorded" in (row["error_message"] or "")

    def test_null_pid_garbage_file_swept(self, fresh_db, tmp_path):
        outdir = tmp_path / "run-out-garbage"
        outdir.mkdir()
        (outdir / ".orch-daemon.pid").write_text("not-a-number\n")
        _insert_run(fresh_db, "running", "nopid003", pid=None,
                    output_dir=str(outdir))

        n = fresh_db.sweep_zombie_runs()

        assert n >= 1
        row = fresh_db.get_pipeline_run("nopid003")
        assert row["status"] == "crashed"
        assert "no PID recorded" in (row["error_message"] or "")


# ---------------------------------------------------------------------------
# A.6 — NULL pid + readable PID file → PID checked from file
# ---------------------------------------------------------------------------


class TestPidFileFallback:
    """Behavioral contract A.6: NULL pid + PID file is read and checked."""

    def test_pid_file_dead_pid_swept(self, fresh_db, tmp_path):
        outdir = tmp_path / "run-out-file-dead"
        outdir.mkdir()
        dead = _dead_pid()
        (outdir / ".orch-daemon.pid").write_text(str(dead))
        _insert_run(fresh_db, "running", "file0001", pid=None,
                    output_dir=str(outdir))

        n = fresh_db.sweep_zombie_runs()

        assert n >= 1
        row = fresh_db.get_pipeline_run("file0001")
        assert row["status"] == "crashed"
        assert str(dead) in (row["error_message"] or "")

    def test_pid_file_live_pid_preserved(self, fresh_db, tmp_path):
        outdir = tmp_path / "run-out-file-live"
        outdir.mkdir()
        live = _live_pid()
        (outdir / ".orch-daemon.pid").write_text(str(live))
        _insert_run(fresh_db, "running", "file0002", pid=None,
                    output_dir=str(outdir))

        before = fresh_db.get_pipeline_run("file0002")
        fresh_db.sweep_zombie_runs()
        after = fresh_db.get_pipeline_run("file0002")

        assert after["status"] == before["status"] == "running"
        assert after["error_message"] == before["error_message"]
        assert after["completed_at"] == before["completed_at"]


# ---------------------------------------------------------------------------
# A.7 — pending_review with dead PID is swept
# ---------------------------------------------------------------------------


class TestPendingReviewSwept:
    """Behavioral contract A.7: pending_review rows with dead PIDs
    are also transitioned to crashed."""

    def test_pending_review_dead_pid_swept(self, fresh_db):
        dead = _dead_pid()
        _insert_run(fresh_db, "pending_review", "prev0001", pid=dead)

        n = fresh_db.sweep_zombie_runs()

        assert n >= 1
        row = fresh_db.get_pipeline_run("prev0001")
        assert row["status"] == "crashed"


# ---------------------------------------------------------------------------
# A.8 — count_active_pipeline_runs reflects post-sweep state
# ---------------------------------------------------------------------------


class TestCountActiveAfterSweep:
    """Behavioral contract A.8: count_active drops zombies."""

    def test_count_excludes_swept_zombies(self, fresh_db):
        # K=3 zombies, M=1 live
        dead = _dead_pid()
        live = _live_pid()
        for i in range(3):
            _insert_run(fresh_db, "running", f"zk{i:04d}", pid=dead)
        _insert_run(fresh_db, "running", "lm0001", pid=live)

        n = fresh_db.count_active_pipeline_runs()

        assert n == 1, (
            f"count_active should return only the M=1 live run, "
            f"not the K=3 zombies; got {n}"
        )
        # And the 3 zombies are now crashed
        for i in range(3):
            row = fresh_db.get_pipeline_run(f"zk{i:04d}")
            assert row["status"] == "crashed"


# ---------------------------------------------------------------------------
# A.9 — Launch path bypasses cap after sweep frees slots
# ---------------------------------------------------------------------------


class TestLaunchPathAfterSweep:
    """Behavioral contract A.9: an HTTP launch attempt that would
    have 429'd with K=cap zombies now succeeds (or fails for an
    unrelated reason — never 429) after sweep frees the slots."""

    def test_launch_not_blocked_by_zombies(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from orchestration_engine.web.api import create_api_app
        from orchestration_engine.db import Database

        db_path = tmp_path / "engine.db"
        monkeypatch.setenv("ORCH_DB_PATH", str(db_path))
        monkeypatch.setenv("ORCH_MAX_DAEMONS", "4")

        # Seed 4 zombie rows (would hit the cap pre-fix)
        db = Database(db_path)
        dead = _dead_pid()
        for i in range(4):
            _insert_run(db, "running", f"zmb{i:04d}", pid=dead)
        assert db.count_active_pipeline_runs() == 0, (
            "count_active should have swept the 4 zombies before returning"
        )

        client = TestClient(create_api_app(db_path=str(db_path)))
        resp = client.post("/api/v1/runs", json={
            "template": "definitely-not-a-real-template-xyz-754",
            "input": {},
            "mode": "dry-run",
        })
        # With 0 active runs after sweep, we should NEVER see 429.
        # The request will 404 on the template lookup or 422 on
        # validation; either is acceptable.
        assert resp.status_code != 429, (
            f"launch returned 429 even though zombies should have been swept "
            f"(status={resp.status_code}, body={resp.text[:200]})"
        )


# ---------------------------------------------------------------------------
# A.10 — Startup sweep logs at INFO when N>=1
# ---------------------------------------------------------------------------


class TestStartupSweepLogs:
    """Behavioral contract A.10: startup hook logs 'Startup sweep: marked N'
    at INFO when N>=1, DEBUG when N==0."""

    def test_startup_logs_info_when_zombies_swept(self, tmp_path, monkeypatch, caplog):
        from fastapi.testclient import TestClient
        from orchestration_engine.web.api import create_api_app
        from orchestration_engine.db import Database

        db_path = tmp_path / "engine.db"
        monkeypatch.setenv("ORCH_DB_PATH", str(db_path))

        # Seed 2 zombies
        db = Database(db_path)
        dead = _dead_pid()
        _insert_run(db, "running", "logzmb01", pid=dead)
        _insert_run(db, "running", "logzmb02", pid=dead)

        caplog.set_level(logging.INFO)

        # Creating the app + entering the TestClient context triggers
        # the lifespan/startup hook.
        with TestClient(create_api_app(db_path=str(db_path))) as _client:
            pass  # startup ran; we just want the logs

        # Find the startup-sweep INFO message
        startup_msgs = [
            r.getMessage() for r in caplog.records
            if "Startup sweep: marked" in r.getMessage()
        ]
        assert len(startup_msgs) >= 1, (
            f"expected 'Startup sweep: marked' INFO log; got records: "
            f"{[r.getMessage() for r in caplog.records[-10:]]}"
        )
        # And it must mention a positive count
        assert any(re.search(r"Startup sweep: marked \d+", m) for m in startup_msgs)


# ---------------------------------------------------------------------------
# A.13 — Sweep does not signal/kill processes (sends only signal 0)
# ---------------------------------------------------------------------------


class TestSweepSendsNoSignal:
    """Behavioral contract A.13: sweep does not send any non-zero
    signal to any process. We verify this by checking that the
    sweep's only os.kill calls use signal 0."""

    def test_sweep_uses_only_signal_zero(self, fresh_db, monkeypatch):
        observed_signals: list[int] = []
        real_kill = os.kill

        def spy_kill(pid, sig):
            observed_signals.append(sig)
            return real_kill(pid, sig)

        monkeypatch.setattr("os.kill", spy_kill)

        live = _live_pid()
        _insert_run(fresh_db, "running", "sigchk01", pid=live)
        fresh_db.sweep_zombie_runs()

        # Every os.kill call from the sweep path MUST use signal 0.
        # (Other test infrastructure may call os.kill too; we only
        # assert that NONE of the calls during this sweep used a
        # non-zero signal.)
        non_zero = [s for s in observed_signals if s != 0]
        assert non_zero == [], (
            f"sweep sent non-zero signals: {non_zero} — sweep must be "
            f"read-only from the OS-process perspective"
        )


# ---------------------------------------------------------------------------
# A.14 — PID-reuse race documented in docstring
# ---------------------------------------------------------------------------


class TestPidReuseDocumented:
    """Behavioral contract A.14: the docstring of sweep_zombie_runs
    must contain the literal substring 'PID reuse' so the trade-off
    is discoverable by future maintainers."""

    def test_docstring_mentions_pid_reuse(self, fresh_db):
        doc = fresh_db.sweep_zombie_runs.__doc__ or ""
        assert "PID reuse" in doc, (
            f"sweep_zombie_runs docstring must mention 'PID reuse' "
            f"trade-off; got: {doc[:300]!r}"
        )


# ---------------------------------------------------------------------------
# Source-wiring belt-and-suspenders
# ---------------------------------------------------------------------------


class TestSourceWiring:
    """Belt-and-suspenders: assert the sweep is wired into the
    expected call sites (count_active and the API startup hook)."""

    def test_count_active_calls_sweep(self):
        db_src = read_src("db.py")
        # Match the wiring: count_active_pipeline_runs must call sweep.
        # We look for "sweep_zombie_runs" within the body region of
        # count_active_pipeline_runs.
        start = db_src.find("def count_active_pipeline_runs")
        assert start != -1, "count_active_pipeline_runs not found"
        # Find the next def (end of method)
        nxt = db_src.find("\n    def ", start + 1)
        body = db_src[start:nxt if nxt != -1 else len(db_src)]
        assert "sweep_zombie_runs" in body, (
            "count_active_pipeline_runs must call sweep_zombie_runs() "
            "so the backpressure cap reflects post-sweep state"
        )

    def test_api_module_references_sweep(self):
        api_src = read_src("web/api.py")
        assert "sweep_zombie_runs" in api_src, (
            "web/api.py no longer references sweep_zombie_runs — "
            "the startup sweep hook is gone"
        )
