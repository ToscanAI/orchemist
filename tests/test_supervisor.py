"""Tests for supervisor hook between phases (Issue #194).

Covers:
- APPROVE flow: supervisor approves → pipeline continues normally
- REVISE flow: supervisor requests revision → phase re-runs with feedback
- ABORT flow: supervisor aborts → pipeline fails immediately
- Max retries exceeded: too many REVISE cycles → pipeline aborts
- Supervisor disabled (default): no change to existing behaviour
- YAML field parsing: all five supervisor fields parsed from YAML
- parse_supervisor_response: case-insensitive, first-word matching
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.schemas import TaskResult, TaskState, TaskType, Priority
from orchestration_engine.sequencer import PhaseSequencer, _parse_supervisor_response
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate, TemplateEngine


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_sequencer.py conventions)
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str = "test_phase",
    prompt: str = "Do {input}",
    supervisor: bool = False,
    supervisor_prompt: Optional[str] = None,
    supervisor_model: Optional[str] = None,
    supervisor_rubric: Optional[str] = None,
    supervisor_max_retries: int = 2,
    depends_on: Optional[List[str]] = None,
) -> PhaseDefinition:
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        depends_on=depends_on or [],
        supervisor=supervisor,
        supervisor_prompt=supervisor_prompt,
        supervisor_model=supervisor_model,
        supervisor_rubric=supervisor_rubric,
        supervisor_max_retries=supervisor_max_retries,
    )


def _make_template(phases: List[PhaseDefinition]) -> PipelineTemplate:
    return PipelineTemplate(id="supervisor-test", name="Supervisor Test", phases=phases)


def _success_result(task_spec, text: str = "Great output.") -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": text},
    )


def _failure_result(task_spec, message: str = "Simulated failure") -> TaskResult:
    from orchestration_engine.schemas import TaskError
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.FAILED,
        confidence=0.0,
        result={"text": ""},
        errors=[TaskError(code="EXEC_ERR", message=message, severity="error")],
    )


def _build_runner(execute_fn: Callable) -> MagicMock:
    """Build a mock TaskRunner wired to a custom execute side-effect."""
    runner = MagicMock()
    _store: Dict[str, Any] = {}

    def submit_task(spec):
        _store[spec.id] = spec
        return spec.id

    def get_task(tid):
        return _store.get(tid)

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = get_task
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()

    executor = MagicMock()
    executor.can_handle.return_value = True
    executor.execute.side_effect = execute_fn
    runner.executors = [executor]
    return runner


# ---------------------------------------------------------------------------
# 1. _parse_supervisor_response unit tests
# ---------------------------------------------------------------------------


class TestParseSupervisorResponse:
    """Unit tests for the static response parser."""

    def test_approve_simple(self):
        verdict, reason = _parse_supervisor_response("APPROVE: looks good")
        assert verdict == "APPROVE"
        assert "looks good" in reason

    def test_approve_case_insensitive(self):
        verdict, _ = _parse_supervisor_response("approve: fine")
        assert verdict == "APPROVE"

    def test_revise_simple(self):
        verdict, reason = _parse_supervisor_response("REVISE: needs more detail")
        assert verdict == "REVISE"
        assert "needs more detail" in reason

    def test_abort_simple(self):
        verdict, reason = _parse_supervisor_response("ABORT: completely wrong")
        assert verdict == "ABORT"
        assert "completely wrong" in reason

    def test_multiline_finds_first_verdict(self):
        text = "Thinking about it...\nREVISE: please add examples\nMore text"
        verdict, reason = _parse_supervisor_response(text)
        assert verdict == "REVISE"
        assert "add examples" in reason

    def test_no_verdict_defaults_to_approve(self):
        verdict, reason = _parse_supervisor_response("This is some random text.")
        assert verdict == "APPROVE"
        assert "no verdict" in reason.lower() or "defaulting" in reason.lower()

    def test_abort_mixed_case(self):
        verdict, _ = _parse_supervisor_response("Abort: quality too low")
        assert verdict == "ABORT"

    def test_revise_no_colon(self):
        verdict, _ = _parse_supervisor_response("REVISE the output please")
        assert verdict == "REVISE"

    def test_empty_string_defaults_to_approve(self):
        verdict, _ = _parse_supervisor_response("")
        assert verdict == "APPROVE"


# ---------------------------------------------------------------------------
# 2. PhaseDefinition supervisor fields
# ---------------------------------------------------------------------------


class TestPhaseDefinitionSupervisorFields:
    """PhaseDefinition correctly stores and normalises supervisor fields."""

    def test_defaults(self):
        phase = PhaseDefinition(id="p", name="p", prompt_template="x")
        assert phase.supervisor is False
        assert phase.supervisor_prompt is None
        assert phase.supervisor_model is None
        assert phase.supervisor_rubric is None
        assert phase.supervisor_max_retries == 2

    def test_set_all_fields(self):
        phase = PhaseDefinition(
            id="p", name="p", prompt_template="x",
            supervisor=True,
            supervisor_prompt="Custom prompt",
            supervisor_model="haiku",
            supervisor_rubric="Be concise",
            supervisor_max_retries=3,
        )
        assert phase.supervisor is True
        assert phase.supervisor_prompt == "Custom prompt"
        assert phase.supervisor_model == "haiku"
        assert phase.supervisor_rubric == "Be concise"
        assert phase.supervisor_max_retries == 3

    def test_max_retries_none_normalised(self):
        phase = PhaseDefinition(
            id="p", name="p", prompt_template="x",
            supervisor_max_retries=None,  # type: ignore[arg-type]
        )
        assert phase.supervisor_max_retries == 2

    def test_max_retries_negative_clamped(self):
        phase = PhaseDefinition(
            id="p", name="p", prompt_template="x",
            supervisor_max_retries=-1,
        )
        assert phase.supervisor_max_retries == 0

    def test_max_retries_float_coerced(self):
        phase = PhaseDefinition(
            id="p", name="p", prompt_template="x",
            supervisor_max_retries=1.9,  # type: ignore[arg-type]
        )
        assert phase.supervisor_max_retries == 1
        assert isinstance(phase.supervisor_max_retries, int)


# ---------------------------------------------------------------------------
# 3. YAML parsing of supervisor fields
# ---------------------------------------------------------------------------


class TestYamlSupervisorFields:
    """Supervisor fields are parsed from YAML and not flagged as unknown."""

    def test_yaml_with_all_supervisor_fields(self, tmp_path, caplog):
        yaml_content = """
id: sv-pipeline
name: Supervisor Pipeline
version: "1.0.0"
description: test
author: test
phases:
  - id: step1
    name: Step One
    prompt_template: "Do work: {input}"
    supervisor: true
    supervisor_prompt: "Check this: {phase_output}"
    supervisor_model: haiku
    supervisor_rubric: Be concise and accurate
    supervisor_max_retries: 3
"""
        tpl_file = tmp_path / "sv.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.templates"):
            template = engine.load_template(tpl_file)

        phase = template.phases[0]
        assert phase.supervisor is True
        assert "Check this" in (phase.supervisor_prompt or "")
        assert phase.supervisor_model == "haiku"
        assert phase.supervisor_rubric == "Be concise and accurate"
        assert phase.supervisor_max_retries == 3

        # None of the supervisor fields should appear in unknown-fields warning
        unknown_warns = [
            r.message for r in caplog.records
            if "unknown fields" in r.message.lower()
        ]
        for msg in unknown_warns:
            for field in ("supervisor", "supervisor_prompt", "supervisor_model",
                          "supervisor_rubric", "supervisor_max_retries"):
                assert field not in msg, f"'{field}' wrongly flagged as unknown: {msg}"

    def test_yaml_without_supervisor_fields_defaults(self, tmp_path):
        yaml_content = """
id: plain
name: Plain Pipeline
version: "1.0.0"
description: test
author: test
phases:
  - id: step1
    name: Step One
    prompt_template: "Go"
"""
        tpl_file = tmp_path / "plain.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)
        phase = template.phases[0]
        assert phase.supervisor is False
        assert phase.supervisor_max_retries == 2


# ---------------------------------------------------------------------------
# 4. Supervisor disabled (default) — behaviour unchanged
# ---------------------------------------------------------------------------


class TestSupervisorDisabled:
    """When supervisor=False (default) pipeline behaves exactly as before."""

    def test_no_supervisor_call_on_default_phase(self):
        phase = _make_phase("p1", supervisor=False)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        # Executor called exactly once — no supervisor task was added
        assert call_count == 1
        assert not result.get("aborted", False)

    def test_failed_phase_no_supervisor(self):
        """Failed phase with no supervisor still aborts pipeline normally."""
        phase = _make_phase("p1", supervisor=False)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("failed_phase") == "p1"


# ---------------------------------------------------------------------------
# 5. APPROVE flow
# ---------------------------------------------------------------------------


class TestSupervisorApprove:
    """Supervisor returns APPROVE → pipeline continues to next phase."""

    def test_approve_pipeline_completes(self):
        phase = _make_phase("p1", supervisor=True)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "APPROVE: output looks great")
            return _success_result(task_spec, "Phase output here.")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        # Executor called twice: once for the phase, once for the supervisor
        assert call_count == 2

    def test_approve_with_two_phases_both_complete(self):
        p1 = _make_phase("p1", supervisor=True)
        p2 = _make_phase("p2", depends_on=["p1"])
        template = _make_template([p1, p2])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "APPROVE: good")
            return _success_result(task_spec, "output text")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert "p1" in result["phase_outputs"]
        assert "p2" in result["phase_outputs"]

    def test_approve_result_stored_in_phase_outputs(self):
        phase = _make_phase("p1", supervisor=True)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "APPROVE: all good")
            return _success_result(task_spec, "The final approved text.")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        p1_out = result["phase_outputs"]["p1"]
        assert p1_out["state"] == TaskState.SUCCESS.value

    def test_supervisor_uses_default_prompt_when_none(self):
        """Default prompt is used when supervisor_prompt is None."""
        phase = _make_phase("p1", supervisor=True, supervisor_prompt=None,
                             supervisor_rubric="Be accurate")
        template = _make_template([phase])

        captured_prompts: List[str] = []

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                captured_prompts.append(task_spec.payload.get("prompt", ""))
                return _success_result(task_spec, "APPROVE: fine")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        seq.execute({})

        assert len(captured_prompts) == 1
        # Default prompt should contain RUBRIC and OUTPUT labels
        assert "RUBRIC" in captured_prompts[0]
        assert "OUTPUT" in captured_prompts[0]
        assert "Be accurate" in captured_prompts[0]

    def test_supervisor_uses_custom_prompt_when_set(self):
        custom_prompt = "Evaluate: {phase_output}\nRubric: {rubric}\nVerdict:"
        phase = _make_phase("p1", supervisor=True,
                             supervisor_prompt=custom_prompt,
                             supervisor_rubric="clarity")
        template = _make_template([phase])

        captured_prompts: List[str] = []

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                captured_prompts.append(task_spec.payload.get("prompt", ""))
                return _success_result(task_spec, "APPROVE: ok")
            return _success_result(task_spec, "my output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        seq.execute({})

        assert len(captured_prompts) == 1
        assert "Evaluate:" in captured_prompts[0]
        assert "clarity" in captured_prompts[0]


# ---------------------------------------------------------------------------
# 6. REVISE flow
# ---------------------------------------------------------------------------


class TestSupervisorRevise:
    """Supervisor returns REVISE → phase re-runs with feedback, then supervisor re-evaluates."""

    def test_revise_then_approve_pipeline_completes(self):
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=2)
        template = _make_template([phase])

        sup_call_count = 0
        phase_call_count = 0

        def execute(task_spec, **kw):
            nonlocal sup_call_count, phase_call_count
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                sup_call_count += 1
                if sup_call_count == 1:
                    return _success_result(task_spec, "REVISE: needs more examples")
                return _success_result(task_spec, "APPROVE: much better now")
            else:
                phase_call_count += 1
                return _success_result(task_spec, f"Phase output v{phase_call_count}")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        # Phase ran twice (original + 1 revision), supervisor ran twice
        assert phase_call_count == 2
        assert sup_call_count == 2

    def test_revise_feedback_injected_into_revised_prompt(self):
        """Feedback from REVISE appears in the revised phase's prompt."""
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=1)
        template = _make_template([phase])

        captured_prompts: List[str] = []
        sup_count = 0

        def execute(task_spec, **kw):
            nonlocal sup_count
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                sup_count += 1
                if sup_count == 1:
                    return _success_result(task_spec, "REVISE: add more bullet points")
                return _success_result(task_spec, "APPROVE: ok")
            else:
                captured_prompts.append(task_spec.payload.get("prompt", ""))
                return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert len(captured_prompts) == 2  # original + 1 revised
        # The second (revised) prompt must contain supervisor feedback
        assert "bullet points" in captured_prompts[1] or "Supervisor" in captured_prompts[1]

    def test_revise_counter_decrements_correctly(self):
        """Each REVISE decrements the retry counter; 2 revises succeed with max_retries=2."""
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=2)
        template = _make_template([phase])

        sup_call_count = 0

        def execute(task_spec, **kw):
            nonlocal sup_call_count
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                sup_call_count += 1
                if sup_call_count <= 2:
                    return _success_result(task_spec, f"REVISE: iteration {sup_call_count}")
                return _success_result(task_spec, "APPROVE: finally good")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert sup_call_count == 3  # 2 REVISE + 1 APPROVE


# ---------------------------------------------------------------------------
# 7. ABORT flow
# ---------------------------------------------------------------------------


class TestSupervisorAbort:
    """Supervisor returns ABORT → pipeline fails immediately."""

    def test_abort_marks_pipeline_as_aborted(self):
        phase = _make_phase("p1", supervisor=True)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "ABORT: output is factually incorrect")
            return _success_result(task_spec, "Bad output.")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True

    def test_abort_contains_failed_phase_key(self):
        phase = _make_phase("p1", supervisor=True)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "ABORT: wrong topic")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("failed_phase") == "p1"

    def test_abort_downstream_phase_does_not_execute(self):
        p1 = _make_phase("p1", supervisor=True)
        p2 = _make_phase("p2", depends_on=["p1"])
        template = _make_template([p1, p2])

        p2_called = [False]

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if pid == "p2":
                p2_called[0] = True
            if "__supervisor" in pid:
                return _success_result(task_spec, "ABORT: terrible output")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert p2_called[0] is False, "Downstream phase must not execute after supervisor ABORT"

    def test_abort_sets_supervisor_abort_flag(self):
        phase = _make_phase("p1", supervisor=True)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "ABORT: too short")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("supervisor_abort") is True


# ---------------------------------------------------------------------------
# 8. Max retries exceeded
# ---------------------------------------------------------------------------


class TestSupervisorMaxRetriesExceeded:
    """When max_retries REVISE cycles complete without APPROVE → pipeline aborts."""

    def test_max_retries_0_aborts_on_first_revise(self):
        """supervisor_max_retries=0 means no revisions allowed."""
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=0)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "REVISE: needs work")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("supervisor_abort") is True

    def test_max_retries_1_aborts_after_one_revision(self):
        """supervisor_max_retries=1: 1 revision allowed; 2nd REVISE → abort."""
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=1)
        template = _make_template([phase])

        sup_count = 0

        def execute(task_spec, **kw):
            nonlocal sup_count
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                sup_count += 1
                # Always return REVISE — should exhaust after 1 revision
                return _success_result(task_spec, "REVISE: still not good enough")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("supervisor_abort") is True
        # Supervisor called twice: once initially, once after the 1 allowed revision
        assert sup_count == 2

    def test_max_retries_exhausted_sets_supervisor_abort(self):
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=2)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "REVISE: keep improving")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("supervisor_abort") is True

    def test_max_retries_exhausted_logs_error(self, caplog):
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=1)
        template = _make_template([phase])

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                return _success_result(task_spec, "REVISE: not there yet")
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)

        with caplog.at_level(logging.ERROR, logger="orchestration_engine.sequencer"):
            seq.execute({})

        error_msgs = [
            r.message for r in caplog.records
            if r.levelno == logging.ERROR and "p1" in r.message
        ]
        assert any("max_retries" in m or "exhausted" in m for m in error_msgs), (
            f"Expected max_retries error log; got: {error_msgs}"
        )

    def test_exact_phase_call_count_for_max_retries_2(self):
        """With max_retries=2: phase runs 3 times (orig + 2 revisions), sup 3 times."""
        phase = _make_phase("p1", supervisor=True, supervisor_max_retries=2)
        template = _make_template([phase])

        phase_calls = [0]
        sup_calls = [0]

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if "__supervisor" in pid:
                sup_calls[0] += 1
                return _success_result(task_spec, "REVISE: do better")
            phase_calls[0] += 1
            return _success_result(task_spec, "output")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        # phase: original + 2 revisions = 3
        assert phase_calls[0] == 3
        # supervisor: 3 checks (after original + after each revision)
        # On the 3rd check (after the 2nd revision), max retries is hit before calling supervisor
        # Wait — let me re-think:
        # revise_count starts at 0, max_retries=2
        # Iteration 1: sup says REVISE → revise_count 0 < 2 → revise_count becomes 1 → re-run phase
        # Iteration 2: sup says REVISE → revise_count 1 < 2 → revise_count becomes 2 → re-run phase
        # Iteration 3: sup says REVISE → revise_count 2 >= 2 → ABORT without re-running
        # So supervisor called 3 times, phase called 3 times (original + 2 revisions)
        assert sup_calls[0] == 3
