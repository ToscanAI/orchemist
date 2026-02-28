"""Tests for Issue #192 — Retry Loops with Feedback.

Covers:
- First attempt has empty failure_context
- Retry receives sanitized error + partial output in failure_context
- Multiple retries accumulate attempt_history correctly
- All retries exhausted → retry_history + total_attempts on result
- Templates without {failure_context} work unchanged (backward compat)
- Curly braces in error messages are escaped before SafeDict injection
- _sanitize_error_for_prompt: traceback stripping, ANSI stripping, truncation
- _format_failure_context: correct markdown format, partial output truncation
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration_engine.schemas import TaskError, TaskResult, TaskState, TaskType
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str = "test_phase",
    prompt: str = "Hello {input}",
    retries: int = 0,
    retry_delay_seconds: int = 0,
    depends_on: Optional[List[str]] = None,
) -> PhaseDefinition:
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
        depends_on=depends_on or [],
    )


def _make_template(
    phases: List[PhaseDefinition],
    template_id: str = "retry-feedback-pipeline",
) -> PipelineTemplate:
    return PipelineTemplate(id=template_id, name="Retry Feedback Pipeline", phases=phases)


def _success_result(task_spec, text: str = "success output") -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": text},
    )


def _failure_result(task_spec, message: str = "Simulated failure", text: str = "") -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.FAILED,
        confidence=0.0,
        result={"text": text},
        errors=[TaskError(code="EXEC_ERR", message=message, severity="error")],
    )


def _make_sequencer(phase: PhaseDefinition, executor_side_effects: list):
    """Build a PhaseSequencer with a mock runner whose executor returns the given side effects."""
    template = _make_template([phase])
    mock_executor = MagicMock()
    mock_executor.can_handle.return_value = True
    mock_executor.execute.side_effect = executor_side_effects

    mock_queue = MagicMock()
    mock_queue.get_task.side_effect = lambda tid: MagicMock(
        id=tid,
        type=TaskType.CONTENT,
        payload={"prompt": "initial prompt", "phase_id": phase.id, "pipeline_id": template.id},
        max_retries=3,
    )

    mock_runner = MagicMock()
    mock_runner.executors = [mock_executor]
    mock_runner.queue = mock_queue

    seq = PhaseSequencer(template=template, runner=mock_runner)
    return seq, mock_executor, mock_queue


# ---------------------------------------------------------------------------
# _sanitize_error_for_prompt
# ---------------------------------------------------------------------------


class TestSanitizeErrorForPrompt:
    def test_strips_ansi_codes(self):
        raw = "\x1b[31mERROR\x1b[0m: something failed"
        result = PhaseSequencer._sanitize_error_for_prompt(raw)
        assert "\x1b" not in result
        assert "ERROR: something failed" in result

    def test_strips_python_traceback(self):
        raw = (
            "Traceback (most recent call last):\n"
            "  File \"/app/foo.py\", line 42, in run\n"
            "    raise ValueError('bad input')\n"
            "ValueError: bad input"
        )
        result = PhaseSequencer._sanitize_error_for_prompt(raw)
        assert "Traceback" not in result
        assert "File" not in result
        assert "ValueError: bad input" in result

    def test_strips_traceback_keeps_exception_line_only(self):
        raw = (
            "Traceback (most recent call last):\n"
            "  File \"/app/a.py\", line 1, in foo\n"
            "    bar()\n"
            "  File \"/app/b.py\", line 5, in bar\n"
            "    raise RuntimeError('oops')\n"
            "RuntimeError: oops"
        )
        result = PhaseSequencer._sanitize_error_for_prompt(raw)
        assert result.strip() == "RuntimeError: oops"

    def test_truncates_to_500_chars(self):
        long_error = "x" * 600
        result = PhaseSequencer._sanitize_error_for_prompt(long_error)
        assert len(result) == 500
        assert result.endswith("...")

    def test_short_error_unchanged(self):
        short = "timeout occurred"
        result = PhaseSequencer._sanitize_error_for_prompt(short)
        assert result == short

    def test_empty_string(self):
        result = PhaseSequencer._sanitize_error_for_prompt("")
        assert result == ""

    def test_strips_ansi_and_traceback_combined(self):
        raw = (
            "\x1b[33mTraceback (most recent call last):\x1b[0m\n"
            "  File \"foo.py\", line 1, in x\n"
            "    pass\n"
            "\x1b[31mValueError: \x1b[0mbad"
        )
        result = PhaseSequencer._sanitize_error_for_prompt(raw)
        assert "\x1b" not in result
        assert "Traceback" not in result
        assert "ValueError: bad" in result

    def test_no_traceback_passthrough(self):
        raw = "Connection timed out after 30s"
        result = PhaseSequencer._sanitize_error_for_prompt(raw)
        assert result == raw


# ---------------------------------------------------------------------------
# _format_failure_context
# ---------------------------------------------------------------------------


class TestFormatFailureContext:
    def test_basic_format(self):
        ctx = PhaseSequencer._format_failure_context(1, "timeout error", "partial text")
        assert "## Previous Attempt Failed" in ctx
        assert "**Attempt:** 1" in ctx
        assert "**Error:** timeout error" in ctx
        assert "partial text" in ctx
        assert "Please review the above failure and try a different approach." in ctx

    def test_empty_partial_output(self):
        ctx = PhaseSequencer._format_failure_context(2, "network error", "")
        assert "(none)" in ctx

    def test_partial_output_truncated_to_1000_chars(self):
        long_output = "y" * 2000
        ctx = PhaseSequencer._format_failure_context(1, "err", long_output)
        # The context should contain at most 1000 chars of the partial output
        # Find the partial output section
        assert "y" * 1000 in ctx
        assert "y" * 1001 not in ctx

    def test_attempt_number_reflected(self):
        ctx = PhaseSequencer._format_failure_context(3, "err", "out")
        assert "**Attempt:** 3" in ctx

    def test_markdown_structure(self):
        ctx = PhaseSequencer._format_failure_context(1, "some error", "some output")
        assert ctx.startswith("## Previous Attempt Failed")
        assert "**Partial Output:**" in ctx


# ---------------------------------------------------------------------------
# _build_phase_input with failure_context
# ---------------------------------------------------------------------------


class TestBuildPhaseInputFailureContext:
    def _make_sequencer_bare(self, prompt: str):
        phase = _make_phase(prompt=prompt)
        template = _make_template([phase])
        mock_runner = MagicMock()
        mock_runner.executors = []
        mock_runner.queue = MagicMock()
        seq = PhaseSequencer(template=template, runner=mock_runner)
        return seq, phase

    def test_first_attempt_empty_context(self):
        seq, phase = self._make_sequencer_bare("Task: {input}\n{failure_context}")
        result = seq._build_phase_input(phase, {"key": "val"}, failure_context="")
        # Empty failure_context → nothing meaningful injected
        assert "Task:" in result
        # failure_context should be empty string (no content)
        assert "Previous Attempt" not in result

    def test_retry_context_injected(self):
        seq, phase = self._make_sequencer_bare("Task\n{failure_context}")
        ctx = "## Previous Attempt Failed\n\n**Attempt:** 1\n**Error:** timeout"
        result = seq._build_phase_input(phase, {}, failure_context=ctx)
        # The context should appear in the prompt
        assert "## Previous Attempt Failed" in result
        assert "timeout" in result

    def test_template_without_failure_context_backward_compat(self):
        """Templates that don't include {failure_context} must work unchanged."""
        seq, phase = self._make_sequencer_bare("Simple prompt: {input[question]}")
        # Should not raise even though failure_context is non-empty
        result = seq._build_phase_input(
            phase,
            {"question": "What is AI?"},
            failure_context="## Previous Attempt Failed\n...",
        )
        assert "What is AI?" in result
        # failure_context not in template → not in output
        assert "Previous Attempt" not in result

    def test_curly_braces_in_error_escaped(self):
        """Curly braces in failure_context must not break str.format()."""
        seq, phase = self._make_sequencer_bare("Prompt\n{failure_context}")
        ctx = "Error: {foo} is not defined"
        # Should NOT raise KeyError — curly braces in failure_context are escaped
        result = seq._build_phase_input(phase, {}, failure_context=ctx)
        # The escaped value is injected; {{ → { in the final output
        # Note: Python str.format() substitutes the value verbatim when passed as kwarg,
        # but we escape before passing so {{ in the value remains {{ (visible as {{ in output)
        # What matters: no KeyError raised
        assert "Prompt" in result

    def test_curly_braces_no_key_error(self):
        """Ensure no KeyError even with deeply nested curly braces in failure_context."""
        seq, phase = self._make_sequencer_bare("{failure_context}")
        bad_ctx = "{missing_key} and {another[nested]}"
        # Must not raise
        result = seq._build_phase_input(phase, {}, failure_context=bad_ctx)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _execute_and_wait retry history tracking
# ---------------------------------------------------------------------------


class TestExecuteAndWaitRetryHistory:
    """Tests for retry_history and total_attempts attached to result."""

    def _run_single(self, phase, side_effects, initial_input=None):
        seq, mock_exec, mock_queue = _make_sequencer(phase, side_effects)
        task_id = "task-001"

        # Make get_task return a proper spec with mutable payload
        captured_prompts = []
        real_payload = {
            "prompt": "initial prompt",
            "phase_id": phase.id,
            "pipeline_id": "retry-feedback-pipeline",
        }

        class FakeSpec:
            id = task_id
            type = TaskType.CONTENT
            payload = real_payload
            max_retries = 3

        fake_spec = FakeSpec()
        mock_queue.get_task.return_value = fake_spec

        with patch("time.sleep"):
            result = seq._execute_and_wait(task_id, phase, initial_input=initial_input or {})
        return result, mock_exec

    def test_first_attempt_success_empty_retry_history(self):
        phase = _make_phase(retries=2)

        class FakeSpec:
            id = "t1"
            type = TaskType.CONTENT
            payload = {"prompt": "p", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()
        # side_effect=[] would override return_value; clear it first
        mock_exec.execute.side_effect = None
        mock_exec.execute.return_value = TaskResult(
            task_id="t1",
            task_type=TaskType.CONTENT,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": "ok"},
        )

        with patch("time.sleep"):
            result = seq._execute_and_wait("t1", phase, initial_input={})

        assert result["metadata"]["total_attempts"] == 1
        assert result["metadata"]["retry_history"] == []

    def test_retry_history_records_failed_attempts(self):
        phase = _make_phase(retries=2)

        class FakeSpec:
            id = "t2"
            type = TaskType.CONTENT
            payload = {"prompt": "p", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()

        call_count = [0]

        def side_effect(task_spec, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={"text": f"partial {call_count[0]}"},
                    errors=[TaskError(code="ERR", message=f"error {call_count[0]}", severity="error")],
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "final success"},
            )

        mock_exec.execute.side_effect = side_effect

        with patch("time.sleep"):
            result = seq._execute_and_wait("t2", phase, initial_input={})

        assert result["state"] == "success"
        assert result["metadata"]["total_attempts"] == 3
        history = result["metadata"]["retry_history"]
        assert len(history) == 2  # 2 failed attempts before success
        assert history[0]["attempt"] == 1
        assert "error 1" in history[0]["error"]
        assert history[1]["attempt"] == 2
        assert "error 2" in history[1]["error"]

    def test_all_retries_exhausted_retry_history_attached(self):
        phase = _make_phase(retries=2)

        class FakeSpec:
            id = "t3"
            type = TaskType.CONTENT
            payload = {"prompt": "p", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()

        mock_exec.execute.side_effect = None
        mock_exec.execute.return_value = TaskResult(
            task_id="t3",
            task_type=TaskType.CONTENT,
            state=TaskState.FAILED,
            confidence=0.0,
            result={"text": ""},
            errors=[TaskError(code="ERR", message="permanent failure", severity="error")],
        )

        with patch("time.sleep"):
            result = seq._execute_and_wait("t3", phase, initial_input={})

        assert result["state"] == "failed"
        assert result["metadata"]["total_attempts"] == 3
        history = result["metadata"]["retry_history"]
        assert len(history) == 3
        for i, entry in enumerate(history):
            assert entry["attempt"] == i + 1
            assert "permanent failure" in entry["error"]

    def test_exception_retries_recorded_in_history(self):
        """Exceptions (not graceful FAILED results) are also tracked."""
        phase = _make_phase(retries=1)

        class FakeSpec:
            id = "t4"
            type = TaskType.CONTENT
            payload = {"prompt": "p", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()
        mock_exec.execute.side_effect = RuntimeError("network timeout")

        with patch("time.sleep"):
            result = seq._execute_and_wait("t4", phase, initial_input={})

        history = result["metadata"]["retry_history"]
        assert len(history) == 2  # both attempts raised
        assert "network timeout" in history[0]["error"]
        assert history[0]["partial_output"] == ""

    def test_partial_output_stored_full_in_history(self):
        """Full partial output (>1000 chars) is stored in retry_history."""
        phase = _make_phase(retries=1)
        long_output = "a" * 2000

        class FakeSpec:
            id = "t5"
            type = TaskType.CONTENT
            payload = {"prompt": "p", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()

        call_n = [0]

        def side_effect(task_spec, **kwargs):
            call_n[0] += 1
            if call_n[0] == 1:
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={"text": long_output},
                    errors=[TaskError(code="E", message="fail", severity="error")],
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "ok"},
            )

        mock_exec.execute.side_effect = side_effect

        with patch("time.sleep"):
            result = seq._execute_and_wait("t5", phase, initial_input={})

        assert result["state"] == "success"
        history = result["metadata"]["retry_history"]
        assert len(history) == 1
        # Full output stored in history (not truncated)
        assert len(history[0]["partial_output"]) == 2000

    def test_prompt_updated_on_retry_with_failure_context(self):
        """Verify that the task_spec prompt is updated on the second attempt."""
        phase = _make_phase(retries=1, prompt="Context:\n{failure_context}\nDo work.")

        class FakeSpec:
            id = "t6"
            type = TaskType.CONTENT
            payload = {"prompt": "initial", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()

        prompts_seen = []

        def side_effect(task_spec, **kwargs):
            prompts_seen.append(task_spec.payload.get("prompt", ""))
            call_idx = len(prompts_seen)
            if call_idx == 1:
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={"text": ""},
                    errors=[TaskError(code="E", message="first failure", severity="error")],
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "ok"},
            )

        mock_exec.execute.side_effect = side_effect

        with patch("time.sleep"):
            seq._execute_and_wait("t6", phase, initial_input={})

        assert len(prompts_seen) == 2
        # Second prompt should include failure context
        assert "Previous Attempt Failed" in prompts_seen[1]
        assert "first failure" in prompts_seen[1]

    def test_no_failure_context_in_first_attempt_prompt(self):
        """First attempt prompt must NOT contain any failure context."""
        phase = _make_phase(retries=1, prompt="Work: {failure_context}")

        class FakeSpec:
            id = "t7"
            type = TaskType.CONTENT
            payload = {"prompt": "initial", "phase_id": "test_phase", "pipeline_id": "x"}
            max_retries = 3

        seq, mock_exec, mock_queue = _make_sequencer(phase, [])
        mock_queue.get_task.return_value = FakeSpec()

        prompts_seen = []

        def side_effect(task_spec, **kwargs):
            prompts_seen.append(task_spec.payload.get("prompt", ""))
            call_idx = len(prompts_seen)
            if call_idx == 1:
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={"text": ""},
                    errors=[TaskError(code="E", message="err", severity="error")],
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "ok"},
            )

        mock_exec.execute.side_effect = side_effect

        with patch("time.sleep"):
            seq._execute_and_wait("t7", phase, initial_input={})

        # The first prompt (initial submission, not rebuilt by _execute_and_wait) is "initial"
        assert "Previous Attempt" not in prompts_seen[0]


# ---------------------------------------------------------------------------
# Integration: full pipeline execute() with retry feedback
# ---------------------------------------------------------------------------


class TestPipelineRetryFeedbackIntegration:
    """Run the full sequencer.execute() to verify end-to-end retry feedback."""

    def test_pipeline_success_after_retry_has_retry_history(self):
        phase = _make_phase(
            prompt="Do task. {failure_context}",
            retries=1,
        )
        template = _make_template([phase])

        call_n = [0]

        def executor_side_effect(task_spec, **kwargs):
            call_n[0] += 1
            if call_n[0] == 1:
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={"text": ""},
                    errors=[TaskError(code="E", message="first try failed", severity="error")],
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.95,
                result={"text": "final answer"},
            )

        mock_executor = MagicMock()
        mock_executor.can_handle.return_value = True
        mock_executor.execute.side_effect = executor_side_effect

        task_store = {}

        def submit_task(spec):
            task_store[spec.id] = spec
            return spec.id

        def get_task(tid):
            return task_store.get(tid)

        mock_queue = MagicMock()
        mock_queue.submit_task.side_effect = submit_task
        mock_queue.get_task.side_effect = get_task

        mock_runner = MagicMock()
        mock_runner.executors = [mock_executor]
        mock_runner.queue = mock_queue

        seq = PhaseSequencer(template=template, runner=mock_runner)

        with patch("time.sleep"):
            pipeline_result = seq.execute({"question": "test"})

        phase_result = pipeline_result["phase_outputs"]["test_phase"]
        assert phase_result["state"] == "success"
        assert phase_result["metadata"]["total_attempts"] == 2
        history = phase_result["metadata"]["retry_history"]
        assert len(history) == 1
        assert "first try failed" in history[0]["error"]

    def test_template_without_failure_context_placeholder_backward_compat(self):
        """Old templates without {failure_context} must work fine."""
        phase = _make_phase(
            prompt="Old-style prompt: {input[task]}",
            retries=1,
        )
        template = _make_template([phase])

        call_n = [0]

        def executor_side_effect(task_spec, **kwargs):
            call_n[0] += 1
            if call_n[0] == 1:
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.FAILED,
                    confidence=0.0,
                    result={"text": ""},
                    errors=[TaskError(code="E", message="fail", severity="error")],
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "done"},
            )

        mock_executor = MagicMock()
        mock_executor.can_handle.return_value = True
        mock_executor.execute.side_effect = executor_side_effect

        task_store = {}

        def submit_task(spec):
            task_store[spec.id] = spec
            return spec.id

        def get_task(tid):
            return task_store.get(tid)

        mock_queue = MagicMock()
        mock_queue.submit_task.side_effect = submit_task
        mock_queue.get_task.side_effect = get_task

        mock_runner = MagicMock()
        mock_runner.executors = [mock_executor]
        mock_runner.queue = mock_queue

        seq = PhaseSequencer(template=template, runner=mock_runner)

        with patch("time.sleep"):
            # Must not raise
            pipeline_result = seq.execute({"task": "write a poem"})

        phase_result = pipeline_result["phase_outputs"]["test_phase"]
        assert phase_result["state"] == "success"
        # retry_history present even on backward-compat templates
        assert "retry_history" in phase_result["metadata"]
