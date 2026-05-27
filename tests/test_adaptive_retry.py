"""Tests for the adaptive retry strategy engine (Issue #3.2.1).

Covers:
- RetryStrategy enum
- RetryPlan dataclass (to_json / from_json)
- AdaptiveRetryEngine.plan() for each FailureClass
- DB migration 011 — retry columns on pipeline_runs
- insert_pipeline_run / update_pipeline_run with retry fields
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration_engine.adaptive_retry import (
    DEFAULT_STRATEGY_MAP,
    MODEL_ESCALATION_LADDER,
    AdaptiveRetryEngine,
    RetryPlan,
    RetryStrategy,
)
from orchestration_engine.db import Database
from orchestration_engine.diagnosis import DiagnosisResult, FailureClass, Remediation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """Fresh in-memory database with all migrations applied."""
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def db_with_run(db):
    """DB with a minimal pipeline_run row for FK satisfaction."""
    from tests._helpers import pipeline_run_dict
    db.insert_pipeline_run(pipeline_run_dict(
        "original-run-001",
        template_path="/tmp/t.yaml",
        template_id="t1",
        mode="dry_run",
        output_dir="/tmp/out",
    ))
    return db


def _make_diagnosis(
    failure_class: FailureClass,
    remediation: Remediation = Remediation.RETRY_SAME,
    confidence: float = 0.9,
    explanation: str = "test diagnosis",
) -> DiagnosisResult:
    return DiagnosisResult(
        failure_class=failure_class,
        remediation=remediation,
        confidence=confidence,
        explanation=explanation,
    )


@pytest.fixture
def engine() -> AdaptiveRetryEngine:
    return AdaptiveRetryEngine()


# ===========================================================================
# TestRetryStrategyEnum
# ===========================================================================


class TestRetryStrategyEnum:
    def test_all_six_values_exist(self):
        assert len(RetryStrategy) == 6

    def test_is_str_subclass(self):
        assert isinstance(RetryStrategy.ESCALATE_MODEL, str)

    def test_individual_values(self):
        expected = {
            "escalate_model", "add_context", "split_task",
            "rephrase_prompt", "retry_unchanged", "increase_timeout",
        }
        assert {s.value for s in RetryStrategy} == expected

    def test_from_string_roundtrip(self):
        assert RetryStrategy("escalate_model") is RetryStrategy.ESCALATE_MODEL

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            RetryStrategy("nonexistent")


# ===========================================================================
# TestRetryPlanDataclass
# ===========================================================================


class TestRetryPlanDataclass:
    def test_required_fields(self):
        plan = RetryPlan(
            strategy=RetryStrategy.ESCALATE_MODEL,
            original_run_id="run-001",
        )
        assert plan.strategy is RetryStrategy.ESCALATE_MODEL
        assert plan.original_run_id == "run-001"

    def test_defaults(self):
        plan = RetryPlan(
            strategy=RetryStrategy.RETRY_UNCHANGED,
            original_run_id="run-002",
        )
        assert plan.model_override is None
        assert plan.extra_context is None
        assert plan.timeout_multiplier == 1.0

    def test_to_json_is_valid_json(self):
        plan = RetryPlan(
            strategy=RetryStrategy.ESCALATE_MODEL,
            original_run_id="run-abc",
            model_override="claude-opus-4-6",
            timeout_multiplier=1.5,
        )
        raw = plan.to_json()
        d = json.loads(raw)
        assert d["strategy"] == "escalate_model"
        assert d["original_run_id"] == "run-abc"
        assert d["model_override"] == "claude-opus-4-6"
        assert d["timeout_multiplier"] == 1.5

    def test_to_json_strategy_is_string(self):
        plan = RetryPlan(
            strategy=RetryStrategy.ADD_CONTEXT,
            original_run_id="run-xyz",
        )
        d = json.loads(plan.to_json())
        assert isinstance(d["strategy"], str)

    def test_from_json_roundtrip(self):
        plan = RetryPlan(
            strategy=RetryStrategy.REPHRASE_PROMPT,
            original_run_id="run-round",
            extra_context="some context",
            timeout_multiplier=2.0,
        )
        restored = RetryPlan.from_json(plan.to_json())
        assert restored.strategy is RetryStrategy.REPHRASE_PROMPT
        assert restored.original_run_id == "run-round"
        assert restored.extra_context == "some context"
        assert restored.timeout_multiplier == 2.0

    def test_from_json_invalid_raises(self):
        with pytest.raises((ValueError, KeyError, TypeError)):
            RetryPlan.from_json('{"strategy": "bad_strategy", "original_run_id": "x"}')


# ===========================================================================
# TestDefaultStrategyMap
# ===========================================================================


class TestDefaultStrategyMap:
    def test_all_failure_classes_covered(self):
        """Every FailureClass must appear in the default strategy map."""
        for fc in FailureClass:
            assert fc in DEFAULT_STRATEGY_MAP, f"{fc} not in DEFAULT_STRATEGY_MAP"

    def test_budget_exceeded_is_none(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.BUDGET_EXCEEDED] is None

    def test_quality_gap_escalates_model(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.QUALITY_GAP] is RetryStrategy.ESCALATE_MODEL

    def test_wrong_model_escalates_model(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.WRONG_MODEL] is RetryStrategy.ESCALATE_MODEL

    def test_insufficient_context_adds_context(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.INSUFFICIENT_CONTEXT] is RetryStrategy.ADD_CONTEXT

    def test_bad_prompt_rephrases(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.BAD_PROMPT] is RetryStrategy.REPHRASE_PROMPT

    def test_flaky_test_retry_unchanged(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.FLAKY_TEST] is RetryStrategy.RETRY_UNCHANGED

    def test_infra_issue_retry_unchanged(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.INFRA_ISSUE] is RetryStrategy.RETRY_UNCHANGED

    def test_timeout_increases_timeout(self):
        assert DEFAULT_STRATEGY_MAP[FailureClass.TIMEOUT] is RetryStrategy.INCREASE_TIMEOUT


# ===========================================================================
# TestAdaptiveRetryEngine
# ===========================================================================


class TestAdaptiveRetryEngine:

    # ------------------------------------------------------------------
    # Non-retryable cases
    # ------------------------------------------------------------------

    def test_budget_exceeded_returns_none(self, engine):
        diag = _make_diagnosis(FailureClass.BUDGET_EXCEEDED)
        plan = engine.plan(diag, original_run_id="run-001")
        assert plan is None

    # ------------------------------------------------------------------
    # Model escalation
    # ------------------------------------------------------------------

    def test_quality_gap_produces_escalate_model(self, engine):
        diag = _make_diagnosis(FailureClass.QUALITY_GAP)
        plan = engine.plan(diag, original_run_id="run-002")
        assert plan is not None
        assert plan.strategy is RetryStrategy.ESCALATE_MODEL

    def test_quality_gap_sets_model_override(self, engine):
        diag = _make_diagnosis(FailureClass.QUALITY_GAP)
        plan = engine.plan(diag, original_run_id="run-002",
                           current_model="claude-haiku-4-5-20241022")
        assert plan is not None
        assert plan.model_override == "claude-sonnet-4-6"

    def test_escalation_from_sonnet_to_opus(self, engine):
        diag = _make_diagnosis(FailureClass.WRONG_MODEL)
        plan = engine.plan(diag, original_run_id="run-003",
                           current_model="claude-sonnet-4-6")
        assert plan is not None
        assert plan.model_override == "claude-opus-4-6"

    def test_escalation_at_top_stays_at_opus(self, engine):
        diag = _make_diagnosis(FailureClass.QUALITY_GAP)
        plan = engine.plan(diag, original_run_id="run-004",
                           current_model="claude-opus-4-6")
        assert plan is not None
        assert plan.model_override == "claude-opus-4-6"

    def test_escalation_unknown_model_falls_back_to_top(self, engine):
        diag = _make_diagnosis(FailureClass.QUALITY_GAP)
        plan = engine.plan(diag, original_run_id="run-005",
                           current_model="unknown-model-xyz")
        assert plan is not None
        assert plan.model_override == MODEL_ESCALATION_LADDER[-1]

    def test_escalation_no_current_model_falls_back_to_top(self, engine):
        diag = _make_diagnosis(FailureClass.QUALITY_GAP)
        plan = engine.plan(diag, original_run_id="run-006", current_model=None)
        assert plan is not None
        assert plan.model_override == MODEL_ESCALATION_LADDER[-1]

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def test_insufficient_context_produces_add_context(self, engine):
        diag = _make_diagnosis(FailureClass.INSUFFICIENT_CONTEXT,
                               explanation="Missing file references")
        plan = engine.plan(diag, original_run_id="run-007")
        assert plan is not None
        assert plan.strategy is RetryStrategy.ADD_CONTEXT

    def test_insufficient_context_includes_explanation(self, engine):
        diag = _make_diagnosis(FailureClass.INSUFFICIENT_CONTEXT,
                               explanation="Missing file references")
        plan = engine.plan(diag, original_run_id="run-007")
        assert plan is not None
        assert plan.extra_context is not None
        assert "insufficient_context" in plan.extra_context
        assert "Missing file references" in plan.extra_context

    def test_insufficient_context_fallback_text_when_no_explanation(self, engine):
        diag = DiagnosisResult(
            failure_class=FailureClass.INSUFFICIENT_CONTEXT,
            remediation=Remediation.RETRY_WITH_CONTEXT,
            confidence=0.8,
            explanation=None,
        )
        plan = engine.plan(diag, original_run_id="run-008")
        assert plan is not None
        assert plan.extra_context is not None
        assert len(plan.extra_context) > 0

    # ------------------------------------------------------------------
    # Timeout increase
    # ------------------------------------------------------------------

    def test_timeout_produces_increase_timeout(self, engine):
        diag = _make_diagnosis(FailureClass.TIMEOUT)
        plan = engine.plan(diag, original_run_id="run-009")
        assert plan is not None
        assert plan.strategy is RetryStrategy.INCREASE_TIMEOUT

    def test_timeout_sets_multiplier(self, engine):
        diag = _make_diagnosis(FailureClass.TIMEOUT)
        plan = engine.plan(diag, original_run_id="run-009")
        assert plan is not None
        assert plan.timeout_multiplier > 1.0

    def test_custom_timeout_multiplier(self):
        engine = AdaptiveRetryEngine(timeout_multiplier=3.0)
        diag = _make_diagnosis(FailureClass.TIMEOUT)
        plan = engine.plan(diag, original_run_id="run-010")
        assert plan is not None
        assert plan.timeout_multiplier == 3.0

    # ------------------------------------------------------------------
    # Prompt rephrasing
    # ------------------------------------------------------------------

    def test_bad_prompt_produces_rephrase(self, engine):
        diag = _make_diagnosis(FailureClass.BAD_PROMPT)
        plan = engine.plan(diag, original_run_id="run-011")
        assert plan is not None
        assert plan.strategy is RetryStrategy.REPHRASE_PROMPT

    # ------------------------------------------------------------------
    # Unchanged retry
    # ------------------------------------------------------------------

    def test_flaky_test_produces_retry_unchanged(self, engine):
        diag = _make_diagnosis(FailureClass.FLAKY_TEST)
        plan = engine.plan(diag, original_run_id="run-012")
        assert plan is not None
        assert plan.strategy is RetryStrategy.RETRY_UNCHANGED

    def test_infra_issue_produces_retry_unchanged(self, engine):
        diag = _make_diagnosis(FailureClass.INFRA_ISSUE)
        plan = engine.plan(diag, original_run_id="run-013")
        assert plan is not None
        assert plan.strategy is RetryStrategy.RETRY_UNCHANGED

    # ------------------------------------------------------------------
    # Plan attributes
    # ------------------------------------------------------------------

    def test_plan_carries_original_run_id(self, engine):
        diag = _make_diagnosis(FailureClass.FLAKY_TEST)
        plan = engine.plan(diag, original_run_id="my-original-run")
        assert plan is not None
        assert plan.original_run_id == "my-original-run"

    def test_retry_unchanged_has_no_model_override(self, engine):
        diag = _make_diagnosis(FailureClass.FLAKY_TEST)
        plan = engine.plan(diag, original_run_id="run-014")
        assert plan is not None
        assert plan.model_override is None

    def test_retry_unchanged_has_no_extra_context(self, engine):
        diag = _make_diagnosis(FailureClass.FLAKY_TEST)
        plan = engine.plan(diag, original_run_id="run-015")
        assert plan is not None
        assert plan.extra_context is None

    def test_retry_unchanged_has_multiplier_one(self, engine):
        diag = _make_diagnosis(FailureClass.FLAKY_TEST)
        plan = engine.plan(diag, original_run_id="run-016")
        assert plan is not None
        assert plan.timeout_multiplier == 1.0

    # ------------------------------------------------------------------
    # Custom strategy map
    # ------------------------------------------------------------------

    def test_custom_strategy_map(self):
        custom_map = {fc: RetryStrategy.RETRY_UNCHANGED for fc in FailureClass}
        custom_map[FailureClass.BUDGET_EXCEEDED] = None
        engine = AdaptiveRetryEngine(strategy_map=custom_map)

        diag = _make_diagnosis(FailureClass.QUALITY_GAP)
        plan = engine.plan(diag, original_run_id="run-custom")
        assert plan is not None
        assert plan.strategy is RetryStrategy.RETRY_UNCHANGED


# ===========================================================================
# TestMigration011RetryColumns
# ===========================================================================


class TestMigration011RetryColumns:
    def test_columns_exist(self, db):
        conn = db.get_connection()
        rows = conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()
        columns = {row[1] for row in rows}
        assert "retry_of_run_id" in columns
        assert "retry_strategy" in columns

    def test_index_exists(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_pipeline_runs_retry_of'"
        ).fetchone()
        assert row is not None

    def test_migration_recorded(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM migrations WHERE name='011_add_retry_columns'"
        ).fetchone()
        assert row is not None

    def test_columns_default_to_null(self, db_with_run):
        run = db_with_run.get_pipeline_run("original-run-001")
        assert run is not None
        assert run["retry_of_run_id"] is None
        assert run["retry_strategy"] is None

    def test_idempotent_migration(self, db):
        conn = db.get_connection()
        # Running again must not raise
        db._migration_011_add_retry_columns(conn)


# ===========================================================================
# TestInsertPipelineRunWithRetryFields
# ===========================================================================


class TestInsertPipelineRunWithRetryFields:
    def test_insert_with_retry_fields(self, db_with_run):
        from tests._helpers import pipeline_run_dict
        db_with_run.insert_pipeline_run(pipeline_run_dict(
            "retry-run-001",
            template_path="/tmp/t.yaml",
            template_id="t1",
            mode="dry_run",
            output_dir="/tmp/out",
            retry_of_run_id="original-run-001",
            retry_strategy="escalate_model",
        ))
        run = db_with_run.get_pipeline_run("retry-run-001")
        assert run is not None
        assert run["retry_of_run_id"] == "original-run-001"
        assert run["retry_strategy"] == "escalate_model"

    def test_insert_without_retry_fields_defaults_to_null(self, db):
        from tests._helpers import pipeline_run_dict
        db.insert_pipeline_run(pipeline_run_dict(
            "plain-run-001",
            template_path="/tmp/t.yaml",
            template_id="t1",
            mode="dry_run",
            output_dir="/tmp/out",
        ))
        run = db.get_pipeline_run("plain-run-001")
        assert run is not None
        assert run["retry_of_run_id"] is None
        assert run["retry_strategy"] is None


# ===========================================================================
# TestUpdatePipelineRunWithRetryFields
# ===========================================================================


class TestUpdatePipelineRunWithRetryFields:
    def test_update_retry_of_run_id(self, db_with_run):
        from tests._helpers import pipeline_run_dict
        db_with_run.insert_pipeline_run(pipeline_run_dict(
            "retry-run-002",
            template_path="/tmp/t.yaml",
            template_id="t1",
            mode="dry_run",
            output_dir="/tmp/out",
        ))
        result = db_with_run.update_pipeline_run(
            "retry-run-002",
            retry_of_run_id="original-run-001",
            retry_strategy="add_context",
        )
        assert result is True
        run = db_with_run.get_pipeline_run("retry-run-002")
        assert run is not None
        assert run["retry_of_run_id"] == "original-run-001"
        assert run["retry_strategy"] == "add_context"

    def test_update_unknown_field_ignored(self, db_with_run):
        result = db_with_run.update_pipeline_run(
            "original-run-001",
            nonexistent_field="value",
        )
        assert result is False
