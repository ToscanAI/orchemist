"""Tests for diagnosis data model, enums, dataclass, DB migration and CRUD (Issue #3.1.1)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestration_engine.diagnosis import DiagnosisResult, FailureClass, Remediation
from orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def db_with_run(db):
    """DB with a minimal pipeline_run row for FK satisfaction."""
    from tests._helpers import pipeline_run_dict
    db.insert_pipeline_run(pipeline_run_dict(
        "test-run-001",
        template_path="/tmp/t.yaml",
        template_id="t1",
        mode="dry_run",
        output_dir="/tmp/out",
    ))
    return db


# ---------------------------------------------------------------------------
# TestFailureClassEnum
# ---------------------------------------------------------------------------

class TestFailureClassEnum:
    def test_all_eight_values_exist(self):
        assert len(FailureClass) == 8

    def test_is_str_subclass(self):
        assert isinstance(FailureClass.BAD_PROMPT, str)

    def test_string_comparison(self):
        assert FailureClass.BAD_PROMPT == "bad_prompt"
        assert FailureClass.INFRA_ISSUE == "infra_issue"

    def test_from_string_roundtrip(self):
        assert FailureClass("timeout") is FailureClass.TIMEOUT

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            FailureClass("nonexistent")

    def test_individual_values(self):
        expected = {
            "bad_prompt", "insufficient_context", "wrong_model", "flaky_test",
            "infra_issue", "quality_gap", "timeout", "budget_exceeded",
        }
        assert {m.value for m in FailureClass} == expected


# ---------------------------------------------------------------------------
# TestRemediationEnum
# ---------------------------------------------------------------------------

class TestRemediationEnum:
    def test_all_six_values_exist(self):
        assert len(Remediation) == 6

    def test_is_str_subclass(self):
        assert isinstance(Remediation.RETRY_SAME, str)

    def test_individual_values(self):
        expected = {
            "retry_same", "retry_escalated_model", "retry_with_context",
            "split_task", "escalate_to_human", "no_action",
        }
        assert {m.value for m in Remediation} == expected

    def test_from_string_roundtrip(self):
        assert Remediation("no_action") is Remediation.NO_ACTION

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            Remediation("unknown_action")


# ---------------------------------------------------------------------------
# TestDiagnosisResultDataclass
# ---------------------------------------------------------------------------

class TestDiagnosisResultDataclass:
    def test_required_fields_only(self):
        d = DiagnosisResult(
            failure_class=FailureClass.TIMEOUT,
            remediation=Remediation.RETRY_SAME,
            confidence=0.9,
        )
        assert d.failure_class is FailureClass.TIMEOUT
        assert d.remediation is Remediation.RETRY_SAME
        assert d.confidence == 0.9

    def test_defaults(self):
        d = DiagnosisResult(
            failure_class=FailureClass.BAD_PROMPT,
            remediation=Remediation.NO_ACTION,
            confidence=0.5,
        )
        assert d.explanation is None
        assert d.model_used is None
        assert d.tokens_consumed == 0

    def test_all_fields(self):
        d = DiagnosisResult(
            failure_class=FailureClass.QUALITY_GAP,
            remediation=Remediation.RETRY_ESCALATED_MODEL,
            confidence=0.75,
            explanation="Output score was below threshold",
            model_used="claude-haiku-4-5",
            tokens_consumed=1234,
        )
        assert d.explanation == "Output score was below threshold"
        assert d.model_used == "claude-haiku-4-5"
        assert d.tokens_consumed == 1234

    def test_confidence_is_float(self):
        d = DiagnosisResult(
            failure_class=FailureClass.TIMEOUT,
            remediation=Remediation.RETRY_SAME,
            confidence=0.75,
        )
        assert isinstance(d.confidence, float)

    def test_to_db_dict_keys(self):
        d = DiagnosisResult(
            failure_class=FailureClass.INFRA_ISSUE,
            remediation=Remediation.RETRY_SAME,
            confidence=0.8,
        )
        db_dict = d.to_db_dict("run-xyz")
        assert set(db_dict.keys()) == {
            "run_id", "failure_class", "remediation", "confidence",
            "explanation", "model_used", "tokens_consumed",
        }

    def test_to_db_dict_enum_values_are_strings(self):
        d = DiagnosisResult(
            failure_class=FailureClass.WRONG_MODEL,
            remediation=Remediation.ESCALATE_TO_HUMAN,
            confidence=0.6,
        )
        db_dict = d.to_db_dict("run-xyz")
        assert db_dict["failure_class"] == "wrong_model"
        assert db_dict["remediation"] == "escalate_to_human"
        assert isinstance(db_dict["failure_class"], str)
        assert isinstance(db_dict["remediation"], str)

    def test_to_db_dict_run_id(self):
        d = DiagnosisResult(
            failure_class=FailureClass.FLAKY_TEST,
            remediation=Remediation.RETRY_SAME,
            confidence=0.95,
        )
        assert d.to_db_dict("my-run-id")["run_id"] == "my-run-id"


# ---------------------------------------------------------------------------
# TestDiagnosisDBMigration
# ---------------------------------------------------------------------------

class TestDiagnosisDBMigration:
    def test_table_exists(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='diagnosis_results'"
        ).fetchone()
        assert row is not None

    def test_index_exists(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_diagnosis_results_run_id'"
        ).fetchone()
        assert row is not None

    def test_migration_recorded(self, db):
        conn = db.get_connection()
        row = conn.execute(
            "SELECT name FROM migrations WHERE name='009_add_diagnosis_tables'"
        ).fetchone()
        assert row is not None

    def test_column_names(self, db):
        conn = db.get_connection()
        rows = conn.execute("PRAGMA table_info(diagnosis_results)").fetchall()
        columns = {r["name"] for r in rows}
        expected = {
            "id", "run_id", "failure_class", "remediation", "confidence",
            "explanation", "model_used", "tokens_consumed", "created_at",
        }
        assert expected.issubset(columns)

    def test_idempotent_migration(self, db):
        conn = db.get_connection()
        # Running again must not raise
        db._migration_009_add_diagnosis_tables(conn)


# ---------------------------------------------------------------------------
# TestDiagnosisCRUD
# ---------------------------------------------------------------------------

class TestDiagnosisCRUD:
    def test_insert_returns_int_id(self, db_with_run):
        diag = DiagnosisResult(
            failure_class=FailureClass.TIMEOUT,
            remediation=Remediation.RETRY_SAME,
            confidence=0.9,
        )
        row_id = db_with_run.insert_diagnosis(diag.to_db_dict("test-run-001"))
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_get_by_run_id_basic(self, db_with_run):
        diag = DiagnosisResult(
            failure_class=FailureClass.BAD_PROMPT,
            remediation=Remediation.NO_ACTION,
            confidence=0.7,
        )
        db_with_run.insert_diagnosis(diag.to_db_dict("test-run-001"))
        result = db_with_run.get_diagnosis_by_run_id("test-run-001")
        assert result is not None
        assert result["run_id"] == "test-run-001"

    def test_get_by_run_id_field_values(self, db_with_run):
        diag = DiagnosisResult(
            failure_class=FailureClass.QUALITY_GAP,
            remediation=Remediation.RETRY_ESCALATED_MODEL,
            confidence=0.82,
            explanation="score below 0.8",
            model_used="claude-haiku-4-5",
            tokens_consumed=512,
        )
        db_with_run.insert_diagnosis(diag.to_db_dict("test-run-001"))
        result = db_with_run.get_diagnosis_by_run_id("test-run-001")
        assert result["failure_class"] == "quality_gap"
        assert result["remediation"] == "retry_escalated_model"
        assert abs(result["confidence"] - 0.82) < 1e-6
        assert result["explanation"] == "score below 0.8"
        assert result["tokens_consumed"] == 512

    def test_get_by_run_id_not_found(self, db_with_run):
        assert db_with_run.get_diagnosis_by_run_id("no-such-run") is None

    def test_get_by_run_id_returns_latest(self, db_with_run):
        db_with_run.insert_diagnosis({
            "run_id": "test-run-001",
            "failure_class": "timeout",
            "remediation": "retry_same",
            "confidence": 0.5,
        })
        db_with_run.insert_diagnosis({
            "run_id": "test-run-001",
            "failure_class": "bad_prompt",
            "remediation": "no_action",
            "confidence": 0.9,
        })
        result = db_with_run.get_diagnosis_by_run_id("test-run-001")
        assert result["failure_class"] == "bad_prompt"

    def test_list_diagnoses_returns_all(self, db_with_run):
        for fc in ["timeout", "bad_prompt", "quality_gap"]:
            db_with_run.insert_diagnosis({
                "run_id": "test-run-001",
                "failure_class": fc,
                "remediation": "no_action",
                "confidence": 0.5,
            })
        results = db_with_run.list_diagnoses()
        assert len(results) >= 3

    def test_list_diagnoses_filter_failure_class(self, db_with_run):
        db_with_run.insert_diagnosis({
            "run_id": "test-run-001", "failure_class": "timeout",
            "remediation": "retry_same", "confidence": 0.7,
        })
        db_with_run.insert_diagnosis({
            "run_id": "test-run-001", "failure_class": "bad_prompt",
            "remediation": "no_action", "confidence": 0.6,
        })
        results = db_with_run.list_diagnoses(failure_class="timeout")
        assert all(r["failure_class"] == "timeout" for r in results)

    def test_list_diagnoses_filter_remediation(self, db_with_run):
        db_with_run.insert_diagnosis({
            "run_id": "test-run-001", "failure_class": "timeout",
            "remediation": "retry_same", "confidence": 0.7,
        })
        db_with_run.insert_diagnosis({
            "run_id": "test-run-001", "failure_class": "bad_prompt",
            "remediation": "no_action", "confidence": 0.6,
        })
        results = db_with_run.list_diagnoses(remediation="no_action")
        assert all(r["remediation"] == "no_action" for r in results)

    def test_list_diagnoses_empty(self, db_with_run):
        results = db_with_run.list_diagnoses(failure_class="nonexistent_class")
        assert results == []

    def test_list_diagnoses_limit_offset(self, db_with_run):
        for i in range(3):
            db_with_run.insert_diagnosis({
                "run_id": "test-run-001",
                "failure_class": "timeout",
                "remediation": "retry_same",
                "confidence": 0.1 * (i + 1),
            })
        page1 = db_with_run.list_diagnoses(limit=2, offset=0)
        page2 = db_with_run.list_diagnoses(limit=2, offset=2)
        assert len(page1) == 2
        # IDs must not overlap
        page1_ids = {r["id"] for r in page1}
        page2_ids = {r["id"] for r in page2}
        assert page1_ids.isdisjoint(page2_ids)
