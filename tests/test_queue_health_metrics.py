"""tests/test_queue_health_metrics.py — Issue #932 item 1: queue-health aggregates.

Durable in-repo coverage for the six task_runs/tasks roll-ups that back the
queue-health surface. The 30-test acceptance suite for #932 lives outside the
repo (pipeline scaffolding, not collected by CI); this module gives CI a
focused, offline, deterministic guard over the subtle cases that a naive
implementation gets wrong:

* total-cost-today is a drift-free ``Decimal`` summed over current-UTC-date
  completed rows, excluding other-date and in-flight (NULL ``completed_at``)
  rows; no rows -> ``Decimal('0.00')``.
* staleness is format-robust: a running task whose ``started_at`` was stored in
  the ``T``-separated ISO form is still detected (a raw string ``<`` compare
  silently misses it because ``'T'`` > ``' '``); >30 min -> True, <=30 -> False,
  no running tasks -> False.
* tokens roll up all-time and per-task (task-scoped across attempts, unrelated
  task excluded); zero/NULL -> ``0`` (int).
* the model recorded on a failed attempt prefers explicit > preferred_model >
  the ``'unknown'`` floor.
* per-task execution time is float seconds = completed - started; either
  endpoint missing -> ``0.0``.

Rows are seeded directly (distinct ``attempt_number`` per task, respecting the
production ``UNIQUE(task_id, attempt_number)`` constraint) so behavior is
exercised independently of the write paths.

Test classes
------------
    TestTotalCostToday          — Decimal fidelity, date/in-flight filtering
    TestStaleRunningTasks       — threshold + ISO-format robustness
    TestTokenRollups            — all-time and per-task SUM, COALESCE floor
    TestFailedAttemptModel      — explicit > preferred > 'unknown'
    TestExecutionTimeSeconds    — elapsed seconds, missing-endpoint floor
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orchestration_engine.db import Database
from orchestration_engine.queue import TaskQueue
from orchestration_engine.schemas import (
    TaskSpec,
    TaskType,
    Priority,
    ModelTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tid() -> str:
    return str(uuid.uuid4())


def _now_utc_iso() -> str:
    """ISO timestamp string for 'right now' in UTC (T-separated form)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _minutes_ago_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=minutes)).isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=days)).isoformat()


def _seed_task(db: Database, task_id: str, *, status: str = "queued",
               preferred_model: str = None,
               started_at: str = None, completed_at: str = None) -> None:
    """Insert a minimal tasks row directly, bypassing business logic."""
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO tasks
                (id, type, priority, status, payload, max_retries,
                 preferred_model, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                TaskType.CODE.value,
                Priority.NORMAL.value,
                status,
                json.dumps({}),
                3,
                preferred_model,
                started_at,
                completed_at,
            ),
        )


def _seed_run(db: Database, task_id: str, *,
              tokens_used: int = None,
              cost_usd: float = None,
              completed_at: str = None,
              started_at: str = None,
              model: str = "test-model",
              status: str = "success") -> str:
    """Insert a task_runs row with an auto-assigned distinct attempt_number.

    attempt_number is MAX(attempt_number)+1 for the task so repeated seeds for
    one task never collide on the restored UNIQUE(task_id, attempt_number).
    """
    run_id = _tid()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n "
            "FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        next_attempt = row[0]
        conn.execute(
            """
            INSERT INTO task_runs
                (id, task_id, attempt_number, model, status,
                 tokens_used, cost_usd, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_id,
                next_attempt,
                model,
                status,
                tokens_used,
                cost_usd,
                started_at or _now_utc_iso(),
                completed_at,
            ),
        )
    return run_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """File-backed Database (schema initialized) per test."""
    database = Database(tmp_path / "engine.db")
    yield database
    database.close()


@pytest.fixture
def queue(db):
    return TaskQueue(db)


# ===========================================================================
# Total cost today — Decimal fidelity + date / in-flight filtering
# ===========================================================================

class TestTotalCostToday:

    def test_decimal_no_binary_float_drift(self, db, queue):
        """0.10 + 0.20 sums to exactly Decimal('0.30'), not 0.30000000000000004."""
        tid = _tid()
        _seed_task(db, tid)
        _seed_run(db, tid, cost_usd=0.10, completed_at=_now_utc_iso())
        _seed_run(db, tid, cost_usd=0.20, completed_at=_now_utc_iso())

        stats = queue.get_queue_stats()

        assert isinstance(stats.total_cost_today_usd, Decimal)
        assert stats.total_cost_today_usd == Decimal('0.30')
        # Guard against an in-engine SUM(cost_usd) leaking IEEE-754 drift.
        assert float(stats.total_cost_today_usd) != 0.30000000000000004

    def test_only_current_utc_date_counts(self, db, queue):
        """Two runs completed today sum; a two-days-ago row is excluded."""
        tid = _tid()
        _seed_task(db, tid)
        _seed_run(db, tid, cost_usd=1.0, completed_at=_now_utc_iso())
        _seed_run(db, tid, cost_usd=2.0, completed_at=_now_utc_iso())
        _seed_run(db, tid, cost_usd=4.0, completed_at=_days_ago_iso(2))

        stats = queue.get_queue_stats()

        assert stats.total_cost_today_usd == Decimal('3.00')

    def test_in_flight_rows_excluded(self, db, queue):
        """Cost is realized at completion: a NULL-completed_at row never counts."""
        tid = _tid()
        _seed_task(db, tid)
        _seed_run(db, tid, cost_usd=5.0, completed_at=None)
        _seed_run(db, tid, cost_usd=2.0, completed_at=_now_utc_iso())

        stats = queue.get_queue_stats()

        assert stats.total_cost_today_usd == Decimal('2.00')

    def test_no_rows_today_is_decimal_zero(self, db, queue):
        """Empty / all-other-date data -> Decimal('0.00'), no raise."""
        tid = _tid()
        _seed_task(db, tid)
        _seed_run(db, tid, cost_usd=9.0, completed_at=_days_ago_iso(1))

        stats = queue.get_queue_stats()

        assert isinstance(stats.total_cost_today_usd, Decimal)
        assert stats.total_cost_today_usd == Decimal('0.00')


# ===========================================================================
# Stale running tasks — threshold + ISO-format robustness
# ===========================================================================

class TestStaleRunningTasks:

    def test_over_threshold_flags_true(self, db, queue):
        """A task running 31 minutes (> 30) flags the warning True (bool)."""
        tid = _tid()
        _seed_task(db, tid, status="running", started_at=_minutes_ago_iso(31))

        stats = queue.get_queue_stats()

        assert stats.stale_tasks_warning is True
        assert isinstance(stats.stale_tasks_warning, bool)

    def test_under_threshold_is_false(self, db, queue):
        """A task running 29 minutes (<= 30) does not flag the warning."""
        tid = _tid()
        _seed_task(db, tid, status="running", started_at=_minutes_ago_iso(29))

        stats = queue.get_queue_stats()

        assert stats.stale_tasks_warning is False
        assert isinstance(stats.stale_tasks_warning, bool)

    def test_t_separated_format_still_detected(self, db, queue):
        """A 45-min-stale task stored in 'T'-separated ISO form is still detected.

        This is the regression a raw string '<' comparison misses: 'T' (0x54)
        sorts above ' ' (0x20), so a T-form ``started_at`` compared lexically
        against a space-separated 'now' would wrongly look "newer" than the
        threshold. julianday() on both sides is the only correct comparison.
        """
        ts_t_form = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=45)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        assert "T" in ts_t_form  # sanity: this is the T-separated form

        tid = _tid()
        _seed_task(db, tid, status="running", started_at=ts_t_form)

        stats = queue.get_queue_stats()

        assert stats.stale_tasks_warning is True

    def test_no_running_tasks_is_false(self, db, queue):
        """Old completed/failed/queued tasks never flag staleness; empty -> False."""
        very_old = _minutes_ago_iso(120)
        _seed_task(db, _tid(), status="success", started_at=very_old)
        _seed_task(db, _tid(), status="failed", started_at=very_old)
        _seed_task(db, _tid(), status="queued", started_at=very_old)

        stats = queue.get_queue_stats()

        assert stats.stale_tasks_warning is False
        assert isinstance(stats.stale_tasks_warning, bool)


# ===========================================================================
# Token roll-ups — all-time and per-task SUM, COALESCE floor
# ===========================================================================

class TestTokenRollups:

    def test_all_time_total_sums_across_tasks_ignoring_null(self, db, queue):
        """total_tokens_consumed sums every attempt across tasks; NULL -> nothing."""
        tid_a = _tid()
        tid_b = _tid()
        _seed_task(db, tid_a)
        _seed_task(db, tid_b)
        _seed_run(db, tid_a, tokens_used=10)
        _seed_run(db, tid_a, tokens_used=20)
        _seed_run(db, tid_b, tokens_used=5)
        _seed_run(db, tid_b, tokens_used=None)

        stats = queue.get_queue_stats()

        assert stats.total_tokens_consumed == 35
        assert isinstance(stats.total_tokens_consumed, int)

    def test_per_task_sums_only_that_task(self, db, queue):
        """Per-task tokens sum that task's attempts; an unrelated task is excluded."""
        tid_t = _tid()
        tid_other = _tid()
        _seed_task(db, tid_t)
        _seed_task(db, tid_other)
        _seed_run(db, tid_t, tokens_used=30)
        _seed_run(db, tid_t, tokens_used=70)
        _seed_run(db, tid_other, tokens_used=999)

        status = queue.get_task_status(tid_t)

        assert status is not None
        assert status.tokens_consumed == 100
        assert isinstance(status.tokens_consumed, int)

    def test_no_runs_coalesces_to_int_zero(self, db, queue):
        """No attempt records anywhere -> all-time 0 (int); a task with none -> 0."""
        stats = queue.get_queue_stats()
        assert stats.total_tokens_consumed == 0
        assert isinstance(stats.total_tokens_consumed, int)

        tid = _tid()
        _seed_task(db, tid)
        status = queue.get_task_status(tid)
        assert status.tokens_consumed == 0
        assert isinstance(status.tokens_consumed, int)


# ===========================================================================
# Model on a failed attempt — explicit > preferred_model > 'unknown'
# ===========================================================================

class TestFailedAttemptModel:

    def _latest_run_model(self, db: Database, task_id: str) -> str:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT model FROM task_runs WHERE task_id = ? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert row is not None, f"no task_run for {task_id}"
        return row[0]

    def test_explicit_model_wins_over_preferred(self, db, queue):
        """An explicit model arg beats the task's preferred_model."""
        spec = TaskSpec(type=TaskType.CODE, payload={},
                        preferred_model=ModelTier.HAIKU)
        tid = queue.submit_task(spec)
        queue.get_next_task("worker-1")

        assert queue.fail_task(tid, "boom", model="claude-sonnet-4") is True

        model_val = self._latest_run_model(db, tid)
        assert model_val == "claude-sonnet-4"
        assert model_val != ModelTier.HAIKU.value

    def test_falls_back_to_preferred_model(self, db, queue):
        """No explicit model -> the task's preferred_model is recorded."""
        spec = TaskSpec(type=TaskType.CODE, payload={},
                        preferred_model=ModelTier.HAIKU)
        tid = queue.submit_task(spec)
        queue.get_next_task("worker-1")

        assert queue.fail_task(tid, "boom") is True

        assert self._latest_run_model(db, tid) == ModelTier.HAIKU.value

    def test_unknown_floor_when_nothing_known(self, db, queue):
        """No explicit model and no preferred_model -> the 'unknown' floor."""
        spec = TaskSpec(type=TaskType.CODE, payload={}, preferred_model=None)
        tid = queue.submit_task(spec)
        queue.get_next_task("worker-1")

        assert queue.fail_task(tid, "boom") is True

        assert self._latest_run_model(db, tid) == "unknown"


# ===========================================================================
# Per-task execution time — elapsed seconds, missing-endpoint floor
# ===========================================================================

class TestExecutionTimeSeconds:

    def test_elapsed_seconds_when_both_endpoints_present(self, db, queue):
        """execution_time_seconds == completed - started, as a float."""
        base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        start_ts = base.isoformat()
        end_ts = (base + timedelta(seconds=120)).isoformat()

        tid = _tid()
        _seed_task(db, tid, status="success",
                   started_at=start_ts, completed_at=end_ts)

        status = queue.get_task_status(tid)

        assert status is not None
        assert isinstance(status.execution_time_seconds, float)
        assert status.execution_time_seconds == pytest.approx(120.0, abs=0.5)

    def test_still_running_returns_zero(self, db, queue):
        """A started-but-not-completed task -> 0.0 (float)."""
        tid = _tid()
        _seed_task(db, tid, status="running",
                   started_at=_minutes_ago_iso(5), completed_at=None)

        status = queue.get_task_status(tid)

        assert status.execution_time_seconds == 0.0
        assert isinstance(status.execution_time_seconds, float)

    def test_completed_without_start_returns_zero(self, db, queue):
        """completed_at present but no started_at -> 0.0 (both endpoints required)."""
        tid = _tid()
        _seed_task(db, tid, status="success",
                   started_at=None, completed_at=_now_utc_iso())

        status = queue.get_task_status(tid)

        assert status.execution_time_seconds == 0.0
        assert isinstance(status.execution_time_seconds, float)
