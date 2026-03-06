"""Tests for DiagnosisEngine — LLM-powered failure analysis (Issue #3.1.2)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.diagnosis import (
    DIAGNOSIS_PROMPT_TEMPLATE,
    DiagnosisEngine,
    DiagnosisResult,
    FailureClass,
    Remediation,
)
from orchestration_engine.db import Database
from orchestration_engine.schemas import TaskState, TaskType


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """In-memory database with schema applied."""
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def db_with_run(db):
    """In-memory database with a minimal pipeline_run row."""
    db.insert_pipeline_run(
        {
            "run_id": "run-diag-001",
            "template_path": "/tmp/t.yaml",
            "template_id": "t1",
            "input_json": "{}",
            "mode": "dry_run",
            "output_dir": "/tmp/out",
        }
    )
    return db


def _make_task_result(
    text: str = '{"failure_class": "timeout", "remediation": "retry_same", "confidence": 0.9, "explanation": "test"}',
    state: str = "success",
    model_used: str = "claude-haiku-4-5-20241022",
    tokens: int = 250,
):
    """Helper to build a mock TaskResult."""
    result = MagicMock()
    result.state = TaskState.SUCCESS if state == "success" else TaskState.FAILED
    result.result = {"text": text}
    result.model_used = model_used
    result.tokens_consumed = tokens
    return result


@pytest.fixture
def mock_executor():
    """A mock executor that returns a successful haiku diagnosis by default."""
    executor = MagicMock()
    executor.execute.return_value = _make_task_result()
    return executor


@pytest.fixture
def engine(mock_executor, db_with_run):
    """DiagnosisEngine wired to mock executor and in-memory DB."""
    return DiagnosisEngine(executor=mock_executor, db=db_with_run)


# ---------------------------------------------------------------------------
# TestCollectPhaseContext
# ---------------------------------------------------------------------------


class TestCollectPhaseContext:
    """Tests for DiagnosisEngine._collect_phase_context."""

    def test_falsy_output_dir_returns_placeholder(self):
        assert DiagnosisEngine._collect_phase_context(None) == "(no phase outputs available)"
        assert DiagnosisEngine._collect_phase_context("") == "(no phase outputs available)"

    def test_nonexistent_dir_returns_placeholder(self):
        result = DiagnosisEngine._collect_phase_context("/no/such/path/xyz123")
        assert result == "(no phase outputs available)"

    def test_empty_dir_returns_no_files_placeholder(self, tmp_path):
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert result == "(no phase output files found)"

    def test_ignores_non_matching_files(self, tmp_path):
        (tmp_path / "output.csv").write_text("col1,col2")
        (tmp_path / "notes.log").write_text("some log")
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert result == "(no phase output files found)"

    def test_reads_txt_file(self, tmp_path):
        (tmp_path / "phase1.txt").write_text("hello world")
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "### phase1.txt" in result
        assert "hello world" in result

    def test_reads_md_file(self, tmp_path):
        (tmp_path / "output.md").write_text("# Header")
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "### output.md" in result
        assert "# Header" in result

    def test_reads_json_file(self, tmp_path):
        (tmp_path / "result.json").write_text('{"key": "value"}')
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "### result.json" in result
        assert '"key"' in result

    def test_multiple_files_all_included(self, tmp_path):
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.md").write_text("bbb")
        (tmp_path / "c.json").write_text('{"c": 1}')
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "### a.txt" in result
        assert "### b.md" in result
        assert "### c.json" in result

    def test_truncates_long_content(self, tmp_path):
        long_content = "x" * 5000
        (tmp_path / "big.txt").write_text(long_content)
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "... [truncated]" in result
        # Should contain exactly 4000 x's then the marker
        assert "x" * 4000 in result
        assert "x" * 4001 not in result

    def test_short_content_not_truncated(self, tmp_path):
        (tmp_path / "small.txt").write_text("abc")
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "... [truncated]" not in result
        assert "abc" in result

    def test_files_sorted_deterministically(self, tmp_path):
        (tmp_path / "z.txt").write_text("zzz")
        (tmp_path / "a.txt").write_text("aaa")
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        # a.txt should appear before z.txt
        assert result.index("### a.txt") < result.index("### z.txt")

    def test_non_recursive_does_not_descend_subdirs(self, tmp_path):
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content")
        result = DiagnosisEngine._collect_phase_context(str(tmp_path))
        assert "nested.txt" not in result

    def test_accepts_pathlib_path(self, tmp_path):
        (tmp_path / "test.txt").write_text("works")
        # Should also work when output_dir is a Path object
        result = DiagnosisEngine._collect_phase_context(tmp_path)
        assert "works" in result


# ---------------------------------------------------------------------------
# TestBuildPrompt
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """Tests for DiagnosisEngine._build_prompt."""

    def test_returns_string(self):
        result = DiagnosisEngine._build_prompt("error msg", "context")
        assert isinstance(result, str)

    def test_error_message_included(self):
        result = DiagnosisEngine._build_prompt("Something exploded", "ctx")
        assert "Something exploded" in result

    def test_phase_context_included(self):
        result = DiagnosisEngine._build_prompt("err", "phase output here")
        assert "phase output here" in result

    def test_empty_error_message_uses_placeholder(self):
        result = DiagnosisEngine._build_prompt("", "ctx")
        assert "(no error message provided)" in result

    def test_none_error_message_uses_placeholder(self):
        result = DiagnosisEngine._build_prompt(None, "ctx")  # type: ignore[arg-type]
        assert "(no error message provided)" in result

    def test_contains_classification_instruction(self):
        result = DiagnosisEngine._build_prompt("err", "ctx")
        assert "failure_class" in result
        assert "remediation" in result
        assert "confidence" in result

    def test_uses_diagnosis_prompt_template(self):
        result = DiagnosisEngine._build_prompt("my error", "my context")
        # Verify it's the template (not some custom string)
        assert "pipeline failure analyst" in result

    def test_no_double_braces_in_output(self):
        result = DiagnosisEngine._build_prompt("err", "ctx")
        # After formatting, no raw {{ or }} should remain
        assert "{{" not in result
        assert "}}" not in result


# ---------------------------------------------------------------------------
# TestParseLlmResponse
# ---------------------------------------------------------------------------


class TestParseLlmResponse:
    """Tests for DiagnosisEngine._parse_llm_response."""

    def _valid_json(self, **overrides) -> str:
        data = {
            "failure_class": "timeout",
            "remediation": "retry_same",
            "confidence": 0.85,
            "explanation": "The phase timed out.",
        }
        data.update(overrides)
        return json.dumps(data)

    def test_valid_response_returns_diagnosis_result(self):
        result = DiagnosisEngine._parse_llm_response(self._valid_json())
        assert isinstance(result, DiagnosisResult)

    def test_failure_class_parsed(self):
        result = DiagnosisEngine._parse_llm_response(self._valid_json(failure_class="bad_prompt"))
        assert result.failure_class is FailureClass.BAD_PROMPT

    def test_remediation_parsed(self):
        result = DiagnosisEngine._parse_llm_response(
            self._valid_json(remediation="retry_escalated_model")
        )
        assert result.remediation is Remediation.RETRY_ESCALATED_MODEL

    def test_confidence_parsed(self):
        result = DiagnosisEngine._parse_llm_response(self._valid_json(confidence=0.72))
        assert abs(result.confidence - 0.72) < 1e-6

    def test_explanation_parsed(self):
        result = DiagnosisEngine._parse_llm_response(
            self._valid_json(explanation="Memory limit hit.")
        )
        assert result.explanation == "Memory limit hit."

    def test_explanation_optional(self):
        data = {"failure_class": "timeout", "remediation": "retry_same", "confidence": 0.5}
        result = DiagnosisEngine._parse_llm_response(json.dumps(data))
        assert result.explanation is None

    def test_invalid_json_falls_back_to_escalate(self):
        result = DiagnosisEngine._parse_llm_response("not valid json {{{")
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN

    def test_invalid_json_failure_class_is_infra_issue(self):
        result = DiagnosisEngine._parse_llm_response("garbage")
        assert result.failure_class is FailureClass.INFRA_ISSUE

    def test_invalid_json_confidence_is_zero(self):
        result = DiagnosisEngine._parse_llm_response("")
        assert result.confidence == 0.0

    def test_invalid_failure_class_value_falls_back(self):
        bad = self._valid_json(failure_class="unknown_class")
        result = DiagnosisEngine._parse_llm_response(bad)
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN

    def test_invalid_remediation_value_falls_back(self):
        bad = self._valid_json(remediation="not_a_remediation")
        result = DiagnosisEngine._parse_llm_response(bad)
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN

    def test_missing_required_key_falls_back(self):
        data = {"remediation": "retry_same", "confidence": 0.8}
        result = DiagnosisEngine._parse_llm_response(json.dumps(data))
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN

    def test_all_failure_classes_parse(self):
        for fc in FailureClass:
            payload = self._valid_json(failure_class=fc.value)
            result = DiagnosisEngine._parse_llm_response(payload)
            assert result.failure_class is fc

    def test_all_remediations_parse(self):
        for rem in Remediation:
            payload = self._valid_json(remediation=rem.value)
            result = DiagnosisEngine._parse_llm_response(payload)
            assert result.remediation is rem

    def test_whitespace_around_json_handled(self):
        raw = "  \n" + self._valid_json() + "\n  "
        result = DiagnosisEngine._parse_llm_response(raw)
        assert result.failure_class is FailureClass.TIMEOUT

    def test_fallback_explanation_mentions_error(self):
        result = DiagnosisEngine._parse_llm_response("not json")
        assert result.explanation is not None
        assert len(result.explanation) > 0


# ---------------------------------------------------------------------------
# TestDiagnosisEngineInit
# ---------------------------------------------------------------------------


class TestDiagnosisEngineInit:
    def test_stores_executor_and_db(self, mock_executor, db):
        engine = DiagnosisEngine(executor=mock_executor, db=db)
        assert engine._executor is mock_executor
        assert engine._db is db

    def test_default_model_tier_is_haiku(self):
        assert DiagnosisEngine.DEFAULT_MODEL_TIER == "haiku"


# ---------------------------------------------------------------------------
# TestDiagnoseMethod
# ---------------------------------------------------------------------------


class TestDiagnoseMethod:
    """Integration-style tests for DiagnosisEngine.diagnose()."""

    def test_returns_diagnosis_result(self, engine):
        result = engine.diagnose("run-diag-001", error_message="Timed out", output_dir=None)
        assert isinstance(result, DiagnosisResult)

    def test_calls_executor_once(self, engine, mock_executor):
        engine.diagnose("run-diag-001")
        mock_executor.execute.assert_called_once()

    def test_executor_called_with_analysis_task(self, engine, mock_executor):
        engine.diagnose("run-diag-001")
        call_args = mock_executor.execute.call_args
        task = call_args[0][0]
        assert task.type is TaskType.ANALYSIS

    def test_executor_called_with_haiku_tier(self, engine, mock_executor):
        engine.diagnose("run-diag-001")
        call_kwargs = mock_executor.execute.call_args
        # model_tier should be "haiku" regardless of positional/keyword
        args, kwargs = call_kwargs
        tier = kwargs.get("model_tier") or (args[2] if len(args) > 2 else None)
        assert tier == "haiku"

    def test_prompt_contains_error_message(self, engine, mock_executor):
        engine.diagnose("run-diag-001", error_message="disk full error")
        call_args = mock_executor.execute.call_args
        task = call_args[0][0]
        assert "disk full error" in task.payload["prompt"]

    def test_result_failure_class_from_llm(self, engine):
        result = engine.diagnose("run-diag-001")
        assert result.failure_class is FailureClass.TIMEOUT

    def test_result_remediation_from_llm(self, engine):
        result = engine.diagnose("run-diag-001")
        assert result.remediation is Remediation.RETRY_SAME

    def test_result_model_used_from_executor(self, engine):
        result = engine.diagnose("run-diag-001")
        assert result.model_used == "claude-haiku-4-5-20241022"

    def test_result_tokens_consumed_from_executor(self, engine):
        result = engine.diagnose("run-diag-001")
        assert result.tokens_consumed == 250

    def test_persists_result_in_db(self, engine, db_with_run):
        engine.diagnose("run-diag-001")
        stored = db_with_run.get_diagnosis_by_run_id("run-diag-001")
        assert stored is not None
        assert stored["run_id"] == "run-diag-001"

    def test_persisted_failure_class_matches(self, engine, db_with_run):
        engine.diagnose("run-diag-001")
        stored = db_with_run.get_diagnosis_by_run_id("run-diag-001")
        assert stored["failure_class"] == "timeout"

    def test_persisted_remediation_matches(self, engine, db_with_run):
        engine.diagnose("run-diag-001")
        stored = db_with_run.get_diagnosis_by_run_id("run-diag-001")
        assert stored["remediation"] == "retry_same"

    def test_executor_failure_returns_escalate_fallback(self, db_with_run):
        bad_executor = MagicMock()
        bad_executor.execute.side_effect = RuntimeError("network unreachable")
        engine = DiagnosisEngine(executor=bad_executor, db=db_with_run)
        result = engine.diagnose("run-diag-001")
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN
        assert result.failure_class is FailureClass.INFRA_ISSUE

    def test_executor_failure_persists_fallback(self, db_with_run):
        bad_executor = MagicMock()
        bad_executor.execute.side_effect = ConnectionError("timeout")
        engine = DiagnosisEngine(executor=bad_executor, db=db_with_run)
        engine.diagnose("run-diag-001")
        stored = db_with_run.get_diagnosis_by_run_id("run-diag-001")
        assert stored is not None
        assert stored["remediation"] == "escalate_to_human"

    def test_non_success_executor_state_escalates(self, db_with_run):
        fail_executor = MagicMock()
        fail_executor.execute.return_value = _make_task_result(state="failed")
        engine = DiagnosisEngine(executor=fail_executor, db=db_with_run)
        result = engine.diagnose("run-diag-001")
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN

    def test_bad_llm_json_falls_back_to_escalate(self, db_with_run):
        bad_executor = MagicMock()
        bad_executor.execute.return_value = _make_task_result(text="not json at all !!!")
        engine = DiagnosisEngine(executor=bad_executor, db=db_with_run)
        result = engine.diagnose("run-diag-001")
        assert result.remediation is Remediation.ESCALATE_TO_HUMAN

    def test_phase_context_included_when_output_dir_provided(self, mock_executor, db_with_run, tmp_path):
        (tmp_path / "phase.txt").write_text("execution log here")
        engine = DiagnosisEngine(executor=mock_executor, db=db_with_run)
        engine.diagnose("run-diag-001", output_dir=str(tmp_path))
        call_args = mock_executor.execute.call_args
        task = call_args[0][0]
        assert "execution log here" in task.payload["prompt"]

    def test_no_output_dir_still_succeeds(self, engine):
        result = engine.diagnose("run-diag-001", error_message="err", output_dir=None)
        assert result is not None

    def test_llm_result_as_direct_json_dict(self, db_with_run):
        """Handles case where AnthropicExecutor already parsed the JSON response."""
        executor = MagicMock()
        exec_result = MagicMock()
        exec_result.state = TaskState.SUCCESS
        exec_result.result = {
            "failure_class": "quality_gap",
            "remediation": "retry_escalated_model",
            "confidence": 0.77,
            "explanation": "Score below threshold",
        }
        exec_result.model_used = "claude-haiku-4-5-20241022"
        exec_result.tokens_consumed = 300
        executor.execute.return_value = exec_result

        engine = DiagnosisEngine(executor=executor, db=db_with_run)
        result = engine.diagnose("run-diag-001")
        # Should successfully parse from the re-serialised JSON
        assert result.failure_class is FailureClass.QUALITY_GAP
        assert result.remediation is Remediation.RETRY_ESCALATED_MODEL


# ---------------------------------------------------------------------------
# TestPromptTemplate
# ---------------------------------------------------------------------------


class TestPromptTemplate:
    """Sanity checks on the DIAGNOSIS_PROMPT_TEMPLATE constant."""

    def test_template_is_string(self):
        assert isinstance(DIAGNOSIS_PROMPT_TEMPLATE, str)

    def test_template_has_error_message_placeholder(self):
        assert "{error_message}" in DIAGNOSIS_PROMPT_TEMPLATE

    def test_template_has_phase_context_placeholder(self):
        assert "{phase_context}" in DIAGNOSIS_PROMPT_TEMPLATE

    def test_template_lists_all_failure_classes(self):
        for fc in FailureClass:
            assert fc.value in DIAGNOSIS_PROMPT_TEMPLATE

    def test_template_lists_all_remediations(self):
        for rem in Remediation:
            assert rem.value in DIAGNOSIS_PROMPT_TEMPLATE

    def test_template_mentions_confidence(self):
        assert "confidence" in DIAGNOSIS_PROMPT_TEMPLATE

    def test_template_mentions_explanation(self):
        assert "explanation" in DIAGNOSIS_PROMPT_TEMPLATE
