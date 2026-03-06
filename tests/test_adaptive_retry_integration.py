"""Integration tests for #3.2.3: daemon retry hook + budget guard + retry cap.

Covers:
- Database.count_retries_for_run()
- 'escalated' in TERMINAL_STATUSES
- AdaptiveRetryEngine.estimate_cost() static method
- AdaptiveRetryEngine.plan_and_execute() full orchestration
- Budget guard (input_json.budget_usd)
- Retry cap enforcement
- Non-retryable failure escalation
- Chained retry tracing (retry-of-retry traces to original)
- Custom max_retries parameter
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.adaptive_retry import AdaptiveRetryEngine, RetryStrategy
from orchestration_engine.db import Database, TERMINAL_STATUSES
from orchestration_engine.diagnosis import DiagnosisResult, FailureClass, Remediation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_diagnosis(failure_class: FailureClass = FailureClass.QUALITY_GAP) -> DiagnosisResult:
    """Return a minimal DiagnosisResult for the given failure_class."""
    return DiagnosisResult(
        failure_class=failure_class,
        remediation=Remediation.RETRY_ESCALATED_MODEL,
        explanation="Test diagnosis",
        confidence=0.9,
    )


def _base_run(
    run_id: str = "orig-001",
    input_json: Dict[str, Any] | None = None,
    retry_of_run_id: str | None = None,
    model_override: str | None = None,
) -> Dict[str, Any]:
    """Return a minimal pipeline_run dict matching DB row structure."""
    if input_json is None:
        input_json = {"budget_usd": 1.0}
    if model_override is not None:
        input_json = dict(input_json, model_override=model_override)
    return {
        "run_id": run_id,
        "template_path": "/tmp/template.yaml",
        "template_id": "t1",
        "input_json": json.dumps(input_json),
        "mode": "dry_run",
        "output_dir": f"/tmp/out/{run_id}",
        "status": "failed",
        "gateway_url": None,
        "skip_scoring": 0,
        "retry_of_run_id": retry_of_run_id,
        "retry_strategy": None,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory database with all migrations applied."""
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def db_with_original_run(db):
    """DB with a single original pipeline_run row."""
    db.insert_pipeline_run({
        "run_id": "orig-001",
        "template_path": "/tmp/template.yaml",
        "template_id": "t1",
        "input_json": json.dumps({"budget_usd": 1.0}),
        "mode": "dry_run",
        "output_dir": "/tmp/out/orig-001",
    })
    return db


# ---------------------------------------------------------------------------
# TestCountRetriesForRun
# ---------------------------------------------------------------------------


class TestCountRetriesForRun:
    """Unit tests for Database.count_retries_for_run()."""

    def test_zero_when_no_retries(self, db_with_original_run):
        assert db_with_original_run.count_retries_for_run("orig-001") == 0

    def test_counts_one_retry(self, db_with_original_run):
        db_with_original_run.insert_pipeline_run({
            "run_id": "retry-001",
            "template_path": "/tmp/t.yaml",
            "template_id": "t1",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-001",
            "retry_of_run_id": "orig-001",
        })
        assert db_with_original_run.count_retries_for_run("orig-001") == 1

    def test_counts_multiple_retries(self, db_with_original_run):
        for i in range(3):
            db_with_original_run.insert_pipeline_run({
                "run_id": f"retry-00{i}",
                "template_path": "/tmp/t.yaml",
                "template_id": "t1",
                "input_json": "{}",
                "mode": "dry_run",
                "output_dir": f"/tmp/out/retry-00{i}",
                "retry_of_run_id": "orig-001",
            })
        assert db_with_original_run.count_retries_for_run("orig-001") == 3

    def test_ignores_other_runs(self, db_with_original_run):
        """Retries for a different run ID should not be counted."""
        db_with_original_run.insert_pipeline_run({
            "run_id": "other-run",
            "template_path": "/tmp/t.yaml",
            "template_id": "t1",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/other-run",
        })
        db_with_original_run.insert_pipeline_run({
            "run_id": "retry-for-other",
            "template_path": "/tmp/t.yaml",
            "template_id": "t1",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-for-other",
            "retry_of_run_id": "other-run",
        })
        # orig-001 should still have 0 retries
        assert db_with_original_run.count_retries_for_run("orig-001") == 0

    def test_nonexistent_run_returns_zero(self, db):
        assert db.count_retries_for_run("does-not-exist") == 0


# ---------------------------------------------------------------------------
# TestEscalatedStatus
# ---------------------------------------------------------------------------


class TestEscalatedStatus:
    """Verify 'escalated' is in TERMINAL_STATUSES and is writable."""

    def test_escalated_in_terminal_statuses(self):
        assert "escalated" in TERMINAL_STATUSES

    def test_update_run_to_escalated(self, db_with_original_run):
        db_with_original_run.update_pipeline_run("orig-001", status="escalated")
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row is not None
        assert row["status"] == "escalated"


# ---------------------------------------------------------------------------
# TestEstimateCost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    """Unit tests for AdaptiveRetryEngine.estimate_cost() static method."""

    def test_haiku_cost(self):
        assert AdaptiveRetryEngine.estimate_cost("claude-haiku-4-5-20241022") == 0.05

    def test_sonnet_cost(self):
        assert AdaptiveRetryEngine.estimate_cost("claude-sonnet-4-6") == 0.15

    def test_opus_cost(self):
        assert AdaptiveRetryEngine.estimate_cost("claude-opus-4-6") == 0.50

    def test_none_model_returns_max(self):
        assert AdaptiveRetryEngine.estimate_cost(None) == 0.50

    def test_unknown_model_returns_max(self):
        assert AdaptiveRetryEngine.estimate_cost("gpt-99") == 0.50

    def test_callable_as_static(self):
        """estimate_cost() must be callable without an instance."""
        cost = AdaptiveRetryEngine.estimate_cost("claude-sonnet-4-6")
        assert cost == 0.15


# ---------------------------------------------------------------------------
# TestAdaptiveRetryEnginePlanAndExecute
# ---------------------------------------------------------------------------


class TestAdaptiveRetryEnginePlanAndExecute:
    """Integration tests for plan_and_execute() end-to-end orchestration."""

    @patch("subprocess.Popen")
    def test_ac1_quality_gap_spawns_retry(self, mock_popen, db_with_original_run):
        """AC-1: QUALITY_GAP diagnosis → retry spawned with escalated model."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        run = _base_run(
            run_id="orig-001",
            input_json={"budget_usd": 1.0},
            model_override="claude-haiku-4-5-20241022",
        )
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        # subprocess.Popen must have been called
        assert mock_popen.call_count == 1

        # A new retry row must exist
        rows = db_with_original_run.list_pipeline_runs()
        retry_rows = [r for r in rows if r.get("retry_of_run_id") == "orig-001"]
        assert len(retry_rows) == 1

        retry_row = retry_rows[0]
        assert retry_row["status"] == "pending"
        assert retry_row["retry_strategy"] == RetryStrategy.ESCALATE_MODEL.value

        # Escalated model in input_json
        retry_input = json.loads(retry_row["input_json"])
        assert retry_input.get("model_override") == "claude-sonnet-4-6"

    @patch("subprocess.Popen")
    def test_ac3_timeout_spawns_retry_with_2x_timeout(self, mock_popen, db_with_original_run):
        """AC-3: TIMEOUT diagnosis → retry with doubled timeout_seconds."""
        mock_proc = MagicMock()
        mock_proc.pid = 12346
        mock_popen.return_value = mock_proc

        run = _base_run(
            run_id="orig-001",
            input_json={"budget_usd": 1.0, "timeout_seconds": 60},
        )
        diagnosis = _make_diagnosis(FailureClass.TIMEOUT)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        assert mock_popen.call_count == 1

        retry_rows = [
            r for r in db_with_original_run.list_pipeline_runs()
            if r.get("retry_of_run_id") == "orig-001"
        ]
        assert len(retry_rows) == 1
        retry_input = json.loads(retry_rows[0]["input_json"])
        # timeout_seconds should be doubled (60 * 2.0 = 120)
        assert retry_input.get("timeout_seconds") == 120

    @patch("subprocess.Popen")
    def test_ac3_timeout_tight_budget_allows_retry(self, mock_popen, db_with_original_run):
        """AC-3 budget guard: TIMEOUT on Haiku + $0.20 budget → retry ALLOWED.

        INCREASE_TIMEOUT does not set model_override.  The budget guard must
        use the *current* model (Haiku, ~$0.05) rather than the fallback
        ($0.50 for None), so the retry should be spawned, not escalated.
        """
        mock_proc = MagicMock()
        mock_proc.pid = 12347
        mock_popen.return_value = mock_proc

        run = _base_run(
            run_id="orig-001",
            input_json={
                "budget_usd": 0.20,
                "timeout_seconds": 60,
                "model_override": "claude-haiku-4-5-20241022",
            },
        )
        diagnosis = _make_diagnosis(FailureClass.TIMEOUT)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        # Haiku cost ($0.05) < budget ($0.20) → Popen must be called once
        assert mock_popen.call_count == 1
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] != "escalated"

    @patch("subprocess.Popen")
    def test_ac4_budget_exceeded_escalates(self, mock_popen, db_with_original_run):
        """AC-4: Opus retry cost ($0.50) > budget ($0.10) → escalated, no Popen."""
        run = _base_run(
            run_id="orig-001",
            input_json={"budget_usd": 0.10},
            model_override="claude-opus-4-6",  # plan would stay at Opus (already at top)
        )
        # Use QUALITY_GAP which escalates model; opus is already top → will still cost 0.50
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        mock_popen.assert_not_called()
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] == "escalated"

    @patch("subprocess.Popen")
    def test_ac4_zero_budget_skips_check(self, mock_popen, db_with_original_run):
        """AC-4: No budget_usd in input_json → budget guard skipped → retry spawned."""
        mock_proc = MagicMock()
        mock_proc.pid = 12347
        mock_popen.return_value = mock_proc

        run = _base_run(
            run_id="orig-001",
            input_json={},  # No budget key
            model_override="claude-haiku-4-5-20241022",
        )
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        assert mock_popen.call_count == 1

    @patch("subprocess.Popen")
    def test_ac5_retry_cap_reached_escalates(self, mock_popen, db_with_original_run):
        """AC-5: 3 existing retries → cap reached → escalated, no Popen."""
        # Pre-insert 3 retry runs
        for i in range(3):
            db_with_original_run.insert_pipeline_run({
                "run_id": f"retry-pre-{i:03d}",
                "template_path": "/tmp/t.yaml",
                "template_id": "t1",
                "input_json": "{}",
                "mode": "dry_run",
                "output_dir": f"/tmp/out/retry-pre-{i:03d}",
                "retry_of_run_id": "orig-001",
            })

        run = _base_run(run_id="orig-001", input_json={"budget_usd": 10.0})
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        mock_popen.assert_not_called()
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] == "escalated"

    @patch("subprocess.Popen")
    def test_retry_cap_not_reached_spawns(self, mock_popen, db_with_original_run):
        """2 existing retries (< 3 cap) → retry is spawned."""
        mock_proc = MagicMock()
        mock_proc.pid = 12348
        mock_popen.return_value = mock_proc

        for i in range(2):
            db_with_original_run.insert_pipeline_run({
                "run_id": f"retry-pre-{i:03d}",
                "template_path": "/tmp/t.yaml",
                "template_id": "t1",
                "input_json": "{}",
                "mode": "dry_run",
                "output_dir": f"/tmp/out/retry-pre-{i:03d}",
                "retry_of_run_id": "orig-001",
            })

        run = _base_run(run_id="orig-001", input_json={"budget_usd": 10.0},
                        model_override="claude-haiku-4-5-20241022")
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        assert mock_popen.call_count == 1

    @patch("subprocess.Popen")
    def test_non_retryable_failure_escalates(self, mock_popen, db_with_original_run):
        """Non-retryable failure (BUDGET_EXCEEDED) → no Popen, status='escalated'."""
        run = _base_run(run_id="orig-001")
        diagnosis = _make_diagnosis(FailureClass.BUDGET_EXCEEDED)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        mock_popen.assert_not_called()
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] == "escalated"

    @patch("subprocess.Popen")
    def test_retry_run_has_correct_db_fields(self, mock_popen, db_with_original_run):
        """After successful spawn, new run row has correct linkage fields."""
        mock_proc = MagicMock()
        mock_proc.pid = 12349
        mock_popen.return_value = mock_proc

        run = _base_run(run_id="orig-001", input_json={"budget_usd": 5.0},
                        model_override="claude-haiku-4-5-20241022")
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        all_rows = db_with_original_run.list_pipeline_runs()
        retry_rows = [r for r in all_rows if r.get("retry_of_run_id") == "orig-001"]
        assert len(retry_rows) == 1

        r = retry_rows[0]
        assert r["retry_of_run_id"] == "orig-001"
        assert r["retry_strategy"] == RetryStrategy.ESCALATE_MODEL.value
        assert r["status"] == "pending"
        assert r["template_path"] == "/tmp/template.yaml"

    @patch("subprocess.Popen")
    def test_chained_retry_traces_to_original(self, mock_popen, db_with_original_run):
        """When retrying a retry, the new row's retry_of_run_id points at the root."""
        mock_proc = MagicMock()
        mock_proc.pid = 12350
        mock_popen.return_value = mock_proc

        # Insert a first retry that itself points at orig-001
        db_with_original_run.insert_pipeline_run({
            "run_id": "retry-001",
            "template_path": "/tmp/template.yaml",
            "template_id": "t1",
            "input_json": json.dumps({"budget_usd": 5.0}),
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-001",
            "retry_of_run_id": "orig-001",
        })

        # Now plan_and_execute for the first retry run
        run_for_retry = _base_run(
            run_id="retry-001",
            input_json={"budget_usd": 5.0},
            retry_of_run_id="orig-001",  # This run is itself a retry
            model_override="claude-sonnet-4-6",
        )
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run_for_retry, "retry-001")

        all_rows = db_with_original_run.list_pipeline_runs()
        # The new retry should link to the ORIGINAL run, not to retry-001
        new_retries = [
            r for r in all_rows
            if r.get("retry_of_run_id") == "orig-001"
            and r["run_id"] != "retry-001"
        ]
        assert len(new_retries) == 1, (
            "Expected exactly one new retry row pointing to orig-001 "
            f"but got {[r['run_id'] for r in new_retries]}"
        )

    def test_requires_db_raises_when_none(self):
        """plan_and_execute() raises RuntimeError when db is not provided."""
        engine = AdaptiveRetryEngine()  # no db
        run = _base_run()
        diagnosis = _make_diagnosis()

        with pytest.raises(RuntimeError, match="requires db and db_path"):
            engine.plan_and_execute(diagnosis, run, "orig-001")


# ---------------------------------------------------------------------------
# TestRetryEnginePlanCustomMaxRetries
# ---------------------------------------------------------------------------


class TestRetryEnginePlanCustomMaxRetries:
    """Test custom max_retries argument to plan_and_execute()."""

    @patch("subprocess.Popen")
    def test_custom_max_retries(self, mock_popen, db_with_original_run):
        """max_retries=1 with 1 existing retry → escalated."""
        db_with_original_run.insert_pipeline_run({
            "run_id": "retry-pre-000",
            "template_path": "/tmp/t.yaml",
            "template_id": "t1",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-pre-000",
            "retry_of_run_id": "orig-001",
        })

        run = _base_run(run_id="orig-001", input_json={"budget_usd": 10.0})
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001", max_retries=1)

        mock_popen.assert_not_called()
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] == "escalated"

    @patch("subprocess.Popen")
    def test_custom_max_retries_zero(self, mock_popen, db_with_original_run):
        """max_retries=0 → always escalated (even with 0 existing retries)."""
        run = _base_run(run_id="orig-001", input_json={"budget_usd": 10.0})
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001", max_retries=0)

        mock_popen.assert_not_called()
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] == "escalated"

    @patch("subprocess.Popen")
    def test_cost_limit_usd_key_works(self, mock_popen, db_with_original_run):
        """budget_usd fallback: cost_limit_usd key is also honoured."""
        # Sonnet retry costs $0.15; cost_limit_usd = 0.05 → escalated
        run = _base_run(
            run_id="orig-001",
            input_json={"cost_limit_usd": 0.05},
            model_override="claude-haiku-4-5-20241022",
        )
        # QUALITY_GAP → escalate to Sonnet ($0.15) > limit ($0.05)
        diagnosis = _make_diagnosis(FailureClass.QUALITY_GAP)
        engine = AdaptiveRetryEngine(db=db_with_original_run, db_path=":memory:")

        engine.plan_and_execute(diagnosis, run, "orig-001")

        mock_popen.assert_not_called()
        row = db_with_original_run.get_pipeline_run("orig-001")
        assert row["status"] == "escalated"
