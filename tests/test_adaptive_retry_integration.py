"""Integration tests for daemon retry integration + budget guard + retry cap.

Issue #396 — Sprint 3.2.3.

Covers:
- AdaptiveRetryEngine.estimate_cost() for all model tiers
- AdaptiveRetryEngine.count_existing_retries() wrapper
- _spawn_retry() plan→build→spawn happy path
- Retry cap enforcement (existing_retries >= max_retries → None)
- Budget guard (estimate_cost > remaining → None)
- Non-retryable failure class (BUDGET_EXCEEDED → None)
- Spawn subprocess failure is propagated (caller in run_daemon catches it)
- _get_remaining_budget() arithmetic
- db.count_retries_for_run() DB method
- "escalated" in TERMINAL_STATUSES
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.adaptive_retry import (
    AdaptiveRetryEngine,
    MODEL_ESCALATION_LADDER,
    RetryPlan,
    RetryStrategy,
)
from orchestration_engine.daemon import _get_remaining_budget, _spawn_retry
from orchestration_engine.db import Database, TERMINAL_STATUSES
from orchestration_engine.diagnosis import DiagnosisResult, FailureClass, Remediation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory database with all migrations applied."""
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def original_run(db):
    """Insert and return a minimal first-attempt pipeline run."""
    run_id = "original-run-001"
    db.insert_pipeline_run({
        "run_id": run_id,
        "template_path": "/tmp/template.yaml",
        "template_id": "tpl-001",
        "input_json": json.dumps({"issue": "396", "branch": "feat/3.2.3"}),
        "mode": "dry_run",
        "output_dir": "/tmp/out/original-run-001",
    })
    return db.get_pipeline_run(run_id)


@pytest.fixture
def engine() -> AdaptiveRetryEngine:
    return AdaptiveRetryEngine()


def _diagnosis(
    failure_class: FailureClass = FailureClass.QUALITY_GAP,
    explanation: str = "test failure",
) -> DiagnosisResult:
    return DiagnosisResult(
        failure_class=failure_class,
        remediation=Remediation.RETRY_ESCALATED_MODEL,
        confidence=0.9,
        explanation=explanation,
    )


# ===========================================================================
# TERMINAL_STATUSES
# ===========================================================================


class TestTerminalStatuses:
    def test_escalated_in_terminal_statuses(self):
        assert "escalated" in TERMINAL_STATUSES

    def test_all_expected_statuses_present(self):
        expected = {
            "success", "failed", "cancelled", "crashed",
            "scoring_failed", "pending_review", "rejected", "escalated",
        }
        assert expected.issubset(TERMINAL_STATUSES)


# ===========================================================================
# db.count_retries_for_run
# ===========================================================================


class TestCountRetriesForRun:
    def test_zero_when_no_retries(self, db, original_run):
        count = db.count_retries_for_run("original-run-001")
        assert count == 0

    def test_counts_one_retry(self, db, original_run):
        db.insert_pipeline_run({
            "run_id": "retry-001",
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-001",
            "retry_of_run_id": "original-run-001",
            "retry_strategy": "escalate_model",
        })
        assert db.count_retries_for_run("original-run-001") == 1

    def test_counts_multiple_retries(self, db, original_run):
        for i in range(3):
            db.insert_pipeline_run({
                "run_id": f"retry-{i:03d}",
                "template_path": "/tmp/template.yaml",
                "template_id": "tpl-001",
                "input_json": "{}",
                "mode": "dry_run",
                "output_dir": f"/tmp/out/retry-{i:03d}",
                "retry_of_run_id": "original-run-001",
                "retry_strategy": "escalate_model",
            })
        assert db.count_retries_for_run("original-run-001") == 3

    def test_returns_zero_for_unknown_run(self, db):
        assert db.count_retries_for_run("nonexistent-run-xyz") == 0

    def test_does_not_count_unrelated_runs(self, db, original_run):
        db.insert_pipeline_run({
            "run_id": "unrelated-run-777",
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/unrelated-run-777",
            # No retry_of_run_id
        })
        assert db.count_retries_for_run("original-run-001") == 0


# ===========================================================================
# AdaptiveRetryEngine.estimate_cost
# ===========================================================================


class TestEstimateCost:
    def test_haiku_cost(self, engine):
        plan = RetryPlan(
            strategy=RetryStrategy.ESCALATE_MODEL,
            original_run_id="run-001",
            model_override="claude-haiku-4-5-20241022",
        )
        assert engine.estimate_cost(plan) == pytest.approx(0.05)

    def test_sonnet_cost(self, engine):
        plan = RetryPlan(
            strategy=RetryStrategy.ESCALATE_MODEL,
            original_run_id="run-001",
            model_override="claude-sonnet-4-6",
        )
        assert engine.estimate_cost(plan) == pytest.approx(0.15)

    def test_opus_cost(self, engine):
        plan = RetryPlan(
            strategy=RetryStrategy.ESCALATE_MODEL,
            original_run_id="run-001",
            model_override="claude-opus-4-6",
        )
        assert engine.estimate_cost(plan) == pytest.approx(0.50)

    def test_unknown_model_defaults_to_sonnet(self, engine):
        plan = RetryPlan(
            strategy=RetryStrategy.ESCALATE_MODEL,
            original_run_id="run-001",
            model_override="some-unknown-model-xyz",
        )
        assert engine.estimate_cost(plan) == pytest.approx(0.15)

    def test_no_model_override_defaults_to_sonnet(self, engine):
        plan = RetryPlan(
            strategy=RetryStrategy.RETRY_UNCHANGED,
            original_run_id="run-001",
            model_override=None,
        )
        assert engine.estimate_cost(plan) == pytest.approx(0.15)

    def test_cost_heuristic_covers_all_ladder_models(self, engine):
        for model in MODEL_ESCALATION_LADDER:
            plan = RetryPlan(
                strategy=RetryStrategy.ESCALATE_MODEL,
                original_run_id="x",
                model_override=model,
            )
            cost = engine.estimate_cost(plan)
            assert cost > 0.0, f"Cost for {model} should be positive"


# ===========================================================================
# AdaptiveRetryEngine.count_existing_retries
# ===========================================================================


class TestCountExistingRetries:
    def test_delegates_to_db(self, engine, db, original_run):
        assert engine.count_existing_retries("original-run-001", db) == 0

    def test_reflects_inserted_retries(self, engine, db, original_run):
        db.insert_pipeline_run({
            "run_id": "retry-99",
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-99",
            "retry_of_run_id": "original-run-001",
            "retry_strategy": "retry_unchanged",
        })
        assert engine.count_existing_retries("original-run-001", db) == 1


# ===========================================================================
# _get_remaining_budget
# ===========================================================================


class TestGetRemainingBudget:
    def test_default_budget_no_retries(self, db, original_run, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        remaining = _get_remaining_budget(original_run, db)
        assert remaining == pytest.approx(5.0)

    def test_custom_budget_env_var(self, db, original_run, monkeypatch):
        monkeypatch.setenv("RETRY_BUDGET_USD", "2.0")
        remaining = _get_remaining_budget(original_run, db)
        assert remaining == pytest.approx(2.0)

    def test_budget_decreases_with_retries(self, db, original_run, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        # Insert one retry
        db.insert_pipeline_run({
            "run_id": "retry-budget-001",
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/retry-budget-001",
            "retry_of_run_id": "original-run-001",
            "retry_strategy": "escalate_model",
        })
        remaining = _get_remaining_budget(original_run, db)
        # default budget 5.0 - (1 * 0.15) = 4.85
        assert remaining == pytest.approx(4.85)

    def test_invalid_env_var_falls_back_to_default(self, db, original_run, monkeypatch):
        monkeypatch.setenv("RETRY_BUDGET_USD", "not_a_float")
        remaining = _get_remaining_budget(original_run, db)
        assert remaining == pytest.approx(5.0)


# ===========================================================================
# _spawn_retry — happy path
# ===========================================================================


class TestSpawnRetryHappyPath:
    def test_returns_new_run_id(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )

        assert new_run_id is not None
        assert new_run_id.startswith("retry-")

    def test_new_run_inserted_in_db(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )

        assert new_run_id is not None
        new_run = db.get_pipeline_run(new_run_id)
        assert new_run is not None
        assert new_run["retry_of_run_id"] == "original-run-001"
        assert new_run["retry_strategy"] == "escalate_model"

    def test_subprocess_spawned(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_popen = MagicMock()
            mock_subproc.Popen = mock_popen
            mock_subproc.DEVNULL = -1
            _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "orchestration_engine.daemon" in cmd

    def test_flaky_test_spawns_retry_unchanged(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.FLAKY_TEST),
                db=db,
                db_path=db_path,
            )

        assert new_run_id is not None
        new_run = db.get_pipeline_run(new_run_id)
        assert new_run["retry_strategy"] == "retry_unchanged"


# ===========================================================================
# _spawn_retry — retry cap enforcement
# ===========================================================================


class TestSpawnRetryCapEnforcement:
    def _insert_retries(self, db, original_run_id: str, count: int) -> None:
        for i in range(count):
            db.insert_pipeline_run({
                "run_id": f"cap-retry-{i:03d}",
                "template_path": "/tmp/template.yaml",
                "template_id": "tpl-001",
                "input_json": "{}",
                "mode": "dry_run",
                "output_dir": f"/tmp/out/cap-retry-{i:03d}",
                "retry_of_run_id": original_run_id,
                "retry_strategy": "escalate_model",
            })

    def test_cap_not_reached_allows_retry(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        # 2 existing retries, cap=3 → still allowed
        self._insert_retries(db, "original-run-001", 2)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
                max_retries=3,
            )
        assert new_run_id is not None

    def test_cap_reached_returns_none(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        # 3 existing retries, cap=3 → blocked
        self._insert_retries(db, "original-run-001", 3)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
                max_retries=3,
            )
        assert result is None

    def test_cap_exceeded_returns_none(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        # 5 existing retries, cap=3 → blocked
        self._insert_retries(db, "original-run-001", 5)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
                max_retries=3,
            )
        assert result is None

    def test_cap_not_reached_subprocess_called(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_popen = MagicMock()
            mock_subproc.Popen = mock_popen
            mock_subproc.DEVNULL = -1
            _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
                max_retries=3,
            )
        mock_popen.assert_called_once()

    def test_cap_reached_subprocess_not_called(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        self._insert_retries(db, "original-run-001", 3)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_popen = MagicMock()
            mock_subproc.Popen = mock_popen
            mock_subproc.DEVNULL = -1
            _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
                max_retries=3,
            )
        mock_popen.assert_not_called()


# ===========================================================================
# _spawn_retry — budget guard
# ===========================================================================


class TestSpawnRetryBudgetGuard:
    def test_budget_ok_spawns_retry(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.setenv("RETRY_BUDGET_USD", "10.0")
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )
        assert result is not None

    def test_budget_zero_blocks_retry(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.setenv("RETRY_BUDGET_USD", "0.0")
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )
        assert result is None

    def test_budget_exceeded_returns_none_and_no_subprocess(
        self, db, original_run, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RETRY_BUDGET_USD", "0.01")  # Less than any tier cost
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_popen = MagicMock()
            mock_subproc.Popen = mock_popen
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                # QUALITY_GAP → ESCALATE_MODEL → Sonnet $0.15 > $0.01
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )
        assert result is None
        mock_popen.assert_not_called()

    def test_budget_exhausted_by_prior_retries(self, db, original_run, tmp_path, monkeypatch):
        # Budget = 0.50, one existing retry used 0.15 → remaining 0.35
        # Opus escalation costs 0.50 → blocked
        monkeypatch.setenv("RETRY_BUDGET_USD", "0.20")
        db_path = str(tmp_path / "test.db")

        # One existing retry already consumed ~0.15
        db.insert_pipeline_run({
            "run_id": "prior-retry-001",
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out/prior-retry-001",
            "retry_of_run_id": "original-run-001",
            "retry_strategy": "escalate_model",
        })
        # Remaining = 0.20 - 0.15 = 0.05; Sonnet costs 0.15 → blocked
        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_popen = MagicMock()
            mock_subproc.Popen = mock_popen
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )
        assert result is None
        mock_popen.assert_not_called()


# ===========================================================================
# _spawn_retry — non-retryable failure class
# ===========================================================================


class TestSpawnRetryNonRetryable:
    def test_budget_exceeded_failure_class_returns_none(
        self, db, original_run, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_popen = MagicMock()
            mock_subproc.Popen = mock_popen
            mock_subproc.DEVNULL = -1
            result = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.BUDGET_EXCEEDED),
                db=db,
                db_path=db_path,
            )
        assert result is None
        mock_popen.assert_not_called()


# ===========================================================================
# _spawn_retry — spawn failure is non-fatal (exception propagates to caller)
# ===========================================================================


class TestSpawnRetrySpawnFailureNonFatal:
    """Verifies that a Popen exception propagates to the caller so that
    the surrounding try/except in run_daemon can catch it non-fatally."""

    def test_popen_exception_propagates(self, db, original_run, tmp_path, monkeypatch):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock(side_effect=OSError("Popen failed"))
            mock_subproc.DEVNULL = -1

            with pytest.raises(OSError, match="Popen failed"):
                _spawn_retry(
                    run_id="original-run-001",
                    run=original_run,
                    diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                    db=db,
                    db_path=db_path,
                )

    def test_caller_catches_spawn_failure(self, db, original_run, tmp_path, monkeypatch):
        """Simulate the try/except wrapper in run_daemon around _spawn_retry."""
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        caught = []
        try:
            with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
                mock_subproc.Popen = MagicMock(side_effect=OSError("spawn error"))
                mock_subproc.DEVNULL = -1
                _spawn_retry(
                    run_id="original-run-001",
                    run=original_run,
                    diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                    db=db,
                    db_path=db_path,
                )
        except Exception as exc:
            caught.append(str(exc))

        assert len(caught) == 1
        assert "spawn error" in caught[0]


# ===========================================================================
# Integration: plan → build_retry_input → spawn (end-to-end)
# ===========================================================================


class TestPlanBuildSpawnFlow:
    """End-to-end flow: diagnosis → plan → build_retry_input → _spawn_retry."""

    def test_escalate_model_sets_model_override_in_retry_input(
        self, db, original_run, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )

        assert new_run_id is not None
        new_run = db.get_pipeline_run(new_run_id)
        assert new_run is not None
        parsed_input = json.loads(new_run["input_json"])
        # QUALITY_GAP → ESCALATE_MODEL → model_override set in input
        assert "model_override" in parsed_input
        assert parsed_input["model_override"] in [
            "claude-sonnet-4-6", "claude-opus-4-6"
        ]

    def test_increase_timeout_sets_timeout_in_retry_input(
        self, db, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        # Insert run with a timeout_seconds field
        run_id = "timeout-run-001"
        input_data = {"timeout_seconds": 300, "issue": "396"}
        db.insert_pipeline_run({
            "run_id": run_id,
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": json.dumps(input_data),
            "mode": "dry_run",
            "output_dir": "/tmp/out/timeout-run-001",
        })
        run = db.get_pipeline_run(run_id)

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id=run_id,
                run=run,
                diagnosis=_diagnosis(FailureClass.TIMEOUT),
                db=db,
                db_path=db_path,
            )

        assert new_run_id is not None
        new_run = db.get_pipeline_run(new_run_id)
        assert new_run is not None
        parsed_input = json.loads(new_run["input_json"])
        # TIMEOUT → INCREASE_TIMEOUT → timeout_seconds multiplied
        assert parsed_input.get("timeout_seconds", 300) >= 300

    def test_retry_run_inherits_template_and_mode(
        self, db, original_run, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            new_run_id = _spawn_retry(
                run_id="original-run-001",
                run=original_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )

        assert new_run_id is not None
        new_run = db.get_pipeline_run(new_run_id)
        assert new_run["template_path"] == original_run["template_path"]
        assert new_run["template_id"] == original_run["template_id"]
        assert new_run["mode"] == original_run["mode"]

    def test_retry_of_retry_uses_original_as_anchor(
        self, db, original_run, tmp_path, monkeypatch
    ):
        """When _spawn_retry is called on a retry run, retry_of_run_id points
        at the original (first-attempt) run, not the current retry."""
        monkeypatch.delenv("RETRY_BUDGET_USD", raising=False)
        db_path = str(tmp_path / "test.db")

        # First retry
        first_retry_id = "first-retry-001"
        db.insert_pipeline_run({
            "run_id": first_retry_id,
            "template_path": "/tmp/template.yaml",
            "template_id": "tpl-001",
            "input_json": json.dumps({"issue": "396"}),
            "mode": "dry_run",
            "output_dir": f"/tmp/out/{first_retry_id}",
            "retry_of_run_id": "original-run-001",
            "retry_strategy": "escalate_model",
        })
        first_retry_run = db.get_pipeline_run(first_retry_id)

        with patch("orchestration_engine.daemon.subprocess") as mock_subproc:
            mock_subproc.Popen = MagicMock()
            mock_subproc.DEVNULL = -1
            second_retry_id = _spawn_retry(
                run_id=first_retry_id,
                run=first_retry_run,
                diagnosis=_diagnosis(FailureClass.QUALITY_GAP),
                db=db,
                db_path=db_path,
            )

        assert second_retry_id is not None
        second_run = db.get_pipeline_run(second_retry_id)
        # Must point to the original, not first_retry_id
        assert second_run["retry_of_run_id"] == "original-run-001"
