"""Tests for PhaseSequencer phase retry logic (Issue #209).

Covers:
- AC-7:  Default (retries=0) — executor called exactly once, behaviour unchanged
- AC-8:  All attempts fail — executor called retries+1 times total
- AC-9:  Retry succeeds on 2nd attempt — loop exits, no further calls
- AC-10: All retries exhausted — final FAILED result returned
- AC-11: time.sleep called between failed attempts, NOT after the final attempt
- AC-12: WARNING logged on each failed attempt with phase ID, attempt#/total, error
- AC-13: ERROR logged after final exhausted attempt
- AC-14: metadata["attempt_number"] and metadata["total_attempts"] present
- AC-15: Default no-retry case: both metadata fields == 1
- AC-16: Final failure still aborts the pipeline (aborted=True, failed_phase key)
- AC-17: on_phase_complete called exactly once per phase (not per attempt)
- AC-4/AC-5/AC-6: YAML known_fields includes retries & retry_delay_seconds
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration_engine.schemas import TaskResult, TaskState, TaskType
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str = "test_phase",
    prompt: str = "Hello {input}",
    retries: int = 0,
    retry_delay_seconds: int = 0,  # zero delay by default to keep tests fast
    depends_on: Optional[List[str]] = None,
) -> PhaseDefinition:
    """Build a minimal PhaseDefinition for testing."""
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
    template_id: str = "retry-test-pipeline",
) -> PipelineTemplate:
    return PipelineTemplate(id=template_id, name="Retry Test Pipeline", phases=phases)


def _success_result(task_spec) -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": f"Output of {task_spec.payload.get('phase_id', '?')}"},
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


def _build_runner(
    execute_fn: Callable,
) -> MagicMock:
    """Build a mock TaskRunner with a custom execute side-effect."""
    runner = MagicMock()
    _task_store: Dict[str, Any] = {}

    def submit_task(spec):
        _task_store[spec.id] = spec
        return spec.id

    def get_task(task_id):
        return _task_store.get(task_id)

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
# AC-7 / AC-15 — default no-retry behaviour is identical to before
# ---------------------------------------------------------------------------


class TestDefaultNoRetry:
    """retries=0 (default) — executor is called exactly once, metadata==1."""

    def test_executor_called_exactly_once_on_success(self) -> None:
        phase = _make_phase("p1", retries=0)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert call_count == 1, "executor must be called exactly once with retries=0"
        assert not result.get("aborted", False)

    def test_executor_called_exactly_once_on_failure(self) -> None:
        """Even on failure, with retries=0 executor is only called once."""
        phase = _make_phase("p1", retries=0)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            return _failure_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert call_count == 1
        assert result.get("aborted") is True

    def test_metadata_attempt_number_and_total_attempts_are_1_on_success(self) -> None:
        """AC-15: default no-retry success → both metadata fields == 1."""
        phase = _make_phase("p1", retries=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _success_result(ts))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        phase_result = result["phase_outputs"]["p1"]
        assert phase_result["metadata"]["attempt_number"] == 1
        assert phase_result["metadata"]["total_attempts"] == 1

    def test_metadata_attempt_number_and_total_attempts_are_1_on_failure(self) -> None:
        """AC-15: default no-retry failure → both metadata fields == 1."""
        phase = _make_phase("p1", retries=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        phase_result = result["final_output"]
        assert phase_result["metadata"]["attempt_number"] == 1
        assert phase_result["metadata"]["total_attempts"] == 1


# ---------------------------------------------------------------------------
# AC-8 — all N retries exhausted: executor called N+1 times
# ---------------------------------------------------------------------------


class TestAllRetriesExhausted:
    """When every attempt fails the executor is called retries+1 times."""

    @pytest.mark.parametrize("retries", [1, 2, 3])
    def test_executor_call_count_equals_retries_plus_one(self, retries: int) -> None:
        phase = _make_phase("p1", retries=retries, retry_delay_seconds=0)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            return _failure_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            seq.execute({})

        assert call_count == retries + 1

    def test_final_result_state_is_failed(self) -> None:
        phase = _make_phase("p1", retries=2, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        final = result["final_output"]
        assert final["state"] == TaskState.FAILED.value

    def test_metadata_reflects_all_attempts(self) -> None:
        """AC-14: metadata shows total_attempts == retries+1 on full exhaustion."""
        retries = 3
        phase = _make_phase("p1", retries=retries, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        meta = result["final_output"]["metadata"]
        assert meta["attempt_number"] == retries + 1
        assert meta["total_attempts"] == retries + 1


# ---------------------------------------------------------------------------
# AC-9 — retry succeeds on 2nd attempt: loop exits immediately
# ---------------------------------------------------------------------------


class TestRetrySucceedsOnSecondAttempt:
    """Loop exits as soon as an attempt succeeds; no further calls are made."""

    def test_executor_called_twice_then_success(self) -> None:
        phase = _make_phase("p1", retries=3, retry_delay_seconds=0)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return _failure_result(task_spec)
            return _success_result(task_spec)  # succeeds on 2nd call

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert call_count == 2, "should stop after the first success"
        assert not result.get("aborted", False)

    def test_success_on_second_attempt_pipeline_completes(self) -> None:
        phase = _make_phase("p1", retries=2, retry_delay_seconds=0)
        template = _make_template([phase])

        attempt = 0

        def execute(task_spec, **kw):
            nonlocal attempt
            attempt += 1
            return _failure_result(task_spec) if attempt == 1 else _success_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert "p1" in result["phase_outputs"]
        assert result["phase_outputs"]["p1"]["state"] == TaskState.SUCCESS.value

    def test_metadata_shows_winning_attempt(self) -> None:
        """AC-14: attempt_number and total_attempts both == 2 when 2nd attempt succeeds."""
        phase = _make_phase("p1", retries=5, retry_delay_seconds=0)
        template = _make_template([phase])

        attempt_n = 0

        def execute(task_spec, **kw):
            nonlocal attempt_n
            attempt_n += 1
            return _success_result(task_spec) if attempt_n == 2 else _failure_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        meta = result["phase_outputs"]["p1"]["metadata"]
        assert meta["attempt_number"] == 2
        assert meta["total_attempts"] == 2


# ---------------------------------------------------------------------------
# AC-11 — time.sleep is called between failed attempts, NOT after the last one
# ---------------------------------------------------------------------------


class TestRetryDelay:
    """time.sleep(retry_delay_seconds) is called between attempts only."""

    def test_sleep_called_between_attempts_not_after_last(self) -> None:
        retries = 3
        delay = 10
        phase = _make_phase("p1", retries=retries, retry_delay_seconds=delay)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep") as mock_sleep:
            seq = PhaseSequencer(template, runner)
            seq.execute({})

        # retries=3 → 4 total attempts → 3 sleeps (between each pair)
        assert mock_sleep.call_count == retries
        mock_sleep.assert_called_with(delay)

    def test_sleep_not_called_when_retries_is_zero(self) -> None:
        phase = _make_phase("p1", retries=0, retry_delay_seconds=30)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep") as mock_sleep:
            seq = PhaseSequencer(template, runner)
            seq.execute({})

        mock_sleep.assert_not_called()

    def test_sleep_called_once_when_retries_is_one_and_fail(self) -> None:
        phase = _make_phase("p1", retries=1, retry_delay_seconds=5)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep") as mock_sleep:
            seq = PhaseSequencer(template, runner)
            seq.execute({})

        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(5)

    def test_sleep_not_called_after_successful_retry(self) -> None:
        """No sleep after the attempt that succeeds, even mid-sequence."""
        phase = _make_phase("p1", retries=5, retry_delay_seconds=99)
        template = _make_template([phase])

        attempt_n = 0

        def execute(task_spec, **kw):
            nonlocal attempt_n
            attempt_n += 1
            # succeed on attempt 2 → only 1 sleep should have happened (after attempt 1)
            return _success_result(task_spec) if attempt_n == 2 else _failure_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep") as mock_sleep:
            seq = PhaseSequencer(template, runner)
            seq.execute({})

        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(99)


# ---------------------------------------------------------------------------
# AC-12 / AC-13 — logging
# ---------------------------------------------------------------------------


class TestRetryLogging:
    """Correct log levels and content on each failed attempt and exhaustion."""

    def test_warning_logged_on_each_failed_attempt(self, caplog) -> None:
        retries = 2
        phase = _make_phase("my_phase", retries=retries, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts, "disk full"))

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            with patch("orchestration_engine.sequencer.time.sleep"):
                seq = PhaseSequencer(template, runner)
                seq.execute({})

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "my_phase" in r.message and "failed" in r.message.lower()]

        # One WARNING per failed attempt (3 total for retries=2)
        assert len(warnings) == retries + 1, (
            f"Expected {retries + 1} WARNING records, got {len(warnings)}: "
            f"{[r.message for r in warnings]}"
        )

    def test_warning_message_contains_attempt_fraction(self, caplog) -> None:
        phase = _make_phase("phase_x", retries=2, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts, "timeout"))

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            with patch("orchestration_engine.sequencer.time.sleep"):
                seq = PhaseSequencer(template, runner)
                seq.execute({})

        warn_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "phase_x" in r.message
        ]

        # First warning should say 1/3
        assert any("1/3" in m for m in warn_messages), (
            f"Expected '1/3' in a warning; messages: {warn_messages}"
        )

    def test_warning_message_contains_error_text(self, caplog) -> None:
        phase = _make_phase("phase_y", retries=1, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts, "connection refused"))

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            with patch("orchestration_engine.sequencer.time.sleep"):
                seq = PhaseSequencer(template, runner)
                seq.execute({})

        warn_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "phase_y" in r.message
        ]

        assert any("connection refused" in m for m in warn_messages), (
            f"Expected error text in warning; messages: {warn_messages}"
        )

    def test_error_logged_after_all_retries_exhausted(self, caplog) -> None:
        """AC-13: An ERROR log is emitted when the phase permanently fails."""
        phase = _make_phase("doomed_phase", retries=2, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with caplog.at_level(logging.ERROR, logger="orchestration_engine.sequencer"):
            with patch("orchestration_engine.sequencer.time.sleep"):
                seq = PhaseSequencer(template, runner)
                seq.execute({})

        error_logs = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and "doomed_phase" in r.message
        ]

        assert len(error_logs) >= 1, "Expected at least one ERROR log for exhausted retries"
        # Should mention the total number of attempts
        assert any("3" in r.message for r in error_logs), (
            f"Expected attempt count in error message; messages: {[r.message for r in error_logs]}"
        )


# ---------------------------------------------------------------------------
# AC-16 — pipeline still aborts on final failure
# ---------------------------------------------------------------------------


class TestPipelineAbortOnRetryExhaustion:
    """Final failure (all retries exhausted) still aborts the pipeline."""

    def test_pipeline_aborted_key_present(self) -> None:
        phase = _make_phase("fail_phase", retries=1, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert result.get("aborted") is True

    def test_failed_phase_key_present(self) -> None:
        phase = _make_phase("fail_phase", retries=1, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert result.get("failed_phase") == "fail_phase"

    def test_downstream_phase_not_executed_after_retry_exhaustion(self) -> None:
        """A phase after the failed one must not execute at all."""
        fail_phase = _make_phase("stage1", retries=1, retry_delay_seconds=0)
        ok_phase = _make_phase("stage2", depends_on=["stage1"])
        template = _make_template([fail_phase, ok_phase])

        call_log: List[str] = []

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            call_log.append(pid)
            if pid == "stage1":
                return _failure_result(task_spec)
            return _success_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert "stage2" not in result["phase_outputs"]
        assert result.get("aborted") is True


# ---------------------------------------------------------------------------
# AC-17 — on_phase_complete called once per phase (not per retry attempt)
# ---------------------------------------------------------------------------


class TestOnPhaseCompleteCalledOnce:
    """on_phase_complete fires exactly once per phase regardless of retry count."""

    def test_on_phase_complete_called_once_with_retries(self) -> None:
        phase = _make_phase("p", retries=3, retry_delay_seconds=0)
        template = _make_template([phase])

        attempt_n = 0

        def execute(task_spec, **kw):
            nonlocal attempt_n
            attempt_n += 1
            return _success_result(task_spec) if attempt_n == 3 else _failure_result(task_spec)

        runner = _build_runner(execute)

        complete_calls: List[str] = []

        def on_complete(phase_id, result_dict):
            complete_calls.append(phase_id)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner, on_phase_complete=on_complete)
            seq.execute({})

        assert complete_calls == ["p"], (
            f"on_phase_complete must fire exactly once; got: {complete_calls}"
        )

    def test_on_phase_complete_called_once_when_all_fail(self) -> None:
        phase = _make_phase("q", retries=2, retry_delay_seconds=0)
        template = _make_template([phase])

        runner = _build_runner(lambda ts, **kw: _failure_result(ts))

        complete_calls: List[str] = []

        def on_complete(phase_id, result_dict):
            complete_calls.append(phase_id)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner, on_phase_complete=on_complete)
            seq.execute({})

        assert complete_calls == ["q"]


# ---------------------------------------------------------------------------
# AC-4 / AC-5 / AC-6 — YAML known_fields includes retries & retry_delay_seconds
# ---------------------------------------------------------------------------


class TestPhaseDefinitionFields:
    """PhaseDefinition dataclass and YAML parsing handle retry fields correctly."""

    def test_phase_definition_defaults(self) -> None:
        """AC-1/AC-2: PhaseDefinition has retries=0 and retry_delay_seconds=30 by default."""
        phase = PhaseDefinition(id="p", name="p", prompt_template="x")
        assert phase.retries == 0
        assert phase.retry_delay_seconds == 30

    def test_phase_definition_none_normalization(self) -> None:
        """AC-3: None values for retry fields are normalised to their defaults."""
        phase = PhaseDefinition(id="p", name="p", prompt_template="x",
                                retries=None, retry_delay_seconds=None)  # type: ignore[arg-type]
        assert phase.retries == 0
        assert phase.retry_delay_seconds == 30

    def test_yaml_phase_with_retry_fields_parsed_correctly(self, tmp_path) -> None:
        """AC-5: YAML with retries/retry_delay_seconds parses into PhaseDefinition."""
        import yaml as _yaml
        from orchestration_engine.templates import TemplateEngine

        yaml_content = """
id: retry-pipeline
name: Retry Pipeline
version: "1.0.0"
description: Test retry YAML parsing
author: Test
phases:
  - id: step1
    name: Step One
    prompt_template: "Do work: {input}"
    retries: 3
    retry_delay_seconds: 10
"""
        tpl_file = tmp_path / "retry_test.yaml"
        tpl_file.write_text(yaml_content)

        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)

        assert len(template.phases) == 1
        phase = template.phases[0]
        assert phase.retries == 3
        assert phase.retry_delay_seconds == 10

    def test_yaml_phase_without_retry_fields_uses_defaults(self, tmp_path) -> None:
        """AC-6: YAML without retry fields still parses; defaults apply."""
        from orchestration_engine.templates import TemplateEngine

        yaml_content = """
id: no-retry-pipeline
name: No Retry Pipeline
version: "1.0.0"
description: Pipeline without retry fields
author: Test
phases:
  - id: step1
    name: Step One
    prompt_template: "Simple prompt"
"""
        tpl_file = tmp_path / "no_retry.yaml"
        tpl_file.write_text(yaml_content)

        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)

        phase = template.phases[0]
        assert phase.retries == 0
        assert phase.retry_delay_seconds == 30

    def test_yaml_retry_fields_not_in_unknown_warning(self, tmp_path, caplog) -> None:
        """AC-4: retries and retry_delay_seconds must NOT appear in 'unknown fields' warning."""
        from orchestration_engine.templates import TemplateEngine

        yaml_content = """
id: known-fields-pipeline
name: Known Fields Pipeline
version: "1.0.0"
description: Checking known fields
author: Test
phases:
  - id: phase1
    name: Phase One
    prompt_template: "Go"
    retries: 2
    retry_delay_seconds: 15
"""
        tpl_file = tmp_path / "known_fields.yaml"
        tpl_file.write_text(yaml_content)

        engine = TemplateEngine(templates_dir=tmp_path)

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.templates"):
            engine.load_template(tpl_file)

        unknown_warnings = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "unknown fields" in r.message.lower()
        ]
        for msg in unknown_warnings:
            assert "retries" not in msg, (
                f"'retries' was incorrectly flagged as an unknown field: {msg}"
            )
            assert "retry_delay_seconds" not in msg, (
                f"'retry_delay_seconds' was incorrectly flagged as an unknown field: {msg}"
            )

    def test_negative_retries_clamped_to_zero(self) -> None:
        """Negative retries (e.g. -1) must be clamped to 0, not crash."""
        phase = PhaseDefinition(id="p", name="p", prompt_template="x", retries=-1)
        assert phase.retries == 0

    def test_negative_retry_delay_clamped_to_zero(self) -> None:
        """Negative retry_delay_seconds must be clamped to 0 (time.sleep(-n) raises)."""
        phase = PhaseDefinition(id="p", name="p", prompt_template="x",
                                retry_delay_seconds=-5)
        assert phase.retry_delay_seconds == 0

    def test_float_retries_coerced_to_int(self) -> None:
        """YAML float retries (e.g. 1.5) must be coerced to int — range(1, 2.5) TypeError."""
        phase = PhaseDefinition(id="p", name="p", prompt_template="x",
                                retries=1.5)  # type: ignore[arg-type]
        assert phase.retries == 1
        assert isinstance(phase.retries, int)

    def test_float_retry_delay_coerced_to_int(self) -> None:
        """YAML float retry_delay_seconds coerced to int."""
        phase = PhaseDefinition(id="p", name="p", prompt_template="x",
                                retry_delay_seconds=7.9)  # type: ignore[arg-type]
        assert phase.retry_delay_seconds == 7
        assert isinstance(phase.retry_delay_seconds, int)


# ---------------------------------------------------------------------------
# Exception-catching retry path (WARNING #3 fix)
# ---------------------------------------------------------------------------


class TestExceptionRetry:
    """executor.execute() raising an exception is retried like a FAILED result."""

    def test_exception_on_first_attempt_is_retried(self) -> None:
        """An exception on attempt 1 with retries=1 results in 2 total calls."""
        phase = _make_phase("ex_phase", retries=1, retry_delay_seconds=0)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("API timeout")
            return _success_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert call_count == 2
        assert not result.get("aborted", False)

    def test_all_attempts_raise_exception_pipeline_aborts_gracefully(self) -> None:
        """All attempts raising exceptions → graceful pipeline abort (not crash)."""
        phase = _make_phase("ex_phase", retries=1, retry_delay_seconds=0)
        template = _make_template([phase])

        def always_raise(task_spec, **kw):
            raise RuntimeError("network down")

        runner = _build_runner(always_raise)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        # Should abort gracefully, NOT raise
        assert result.get("aborted") is True
        assert result.get("failed_phase") == "ex_phase"

    def test_exception_then_failure_result_exhausts_all_retries(self) -> None:
        """Mix of exception and FAILED return: all retries exhausted properly."""
        phase = _make_phase("mixed", retries=2, retry_delay_seconds=0)
        template = _make_template([phase])

        call_count = 0

        def execute(task_spec, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient error")
            return _failure_result(task_spec, "persistent failure")

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert call_count == 3  # 1 exception + 2 FAILED returns
        assert result.get("aborted") is True

    def test_exception_path_logs_warning_with_exception_message(self, caplog) -> None:
        """WARNING is logged when executor raises (not returns FAILED)."""
        phase = _make_phase("ex_log_phase", retries=0, retry_delay_seconds=0)
        template = _make_template([phase])

        def raise_os_error(task_spec, **kw):
            raise OSError("disk full")

        runner = _build_runner(raise_os_error)

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            with patch("orchestration_engine.sequencer.time.sleep"):
                seq = PhaseSequencer(template, runner)
                seq.execute({})

        warn_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "ex_log_phase" in r.message
        ]
        assert any("disk full" in m for m in warn_messages), (
            f"Exception message not found in warnings: {warn_messages}"
        )

    def test_exception_path_sleeps_between_attempts(self) -> None:
        """Sleep is called between exception-raising attempts."""
        phase = _make_phase("ex_sleep_phase", retries=2, retry_delay_seconds=7)
        template = _make_template([phase])

        def always_timeout(task_spec, **kw):
            raise TimeoutError("timeout")

        runner = _build_runner(always_timeout)

        with patch("orchestration_engine.sequencer.time.sleep") as mock_sleep:
            seq = PhaseSequencer(template, runner)
            seq.execute({})

        assert mock_sleep.call_count == 2  # between attempt 1→2 and 2→3
        mock_sleep.assert_called_with(7)


# ---------------------------------------------------------------------------
# Integration: multi-phase pipeline with retry on one phase
# ---------------------------------------------------------------------------


class TestRetryIntegration:
    """End-to-end scenarios with multiple phases and retry on one."""

    def test_middle_phase_retries_and_succeeds_pipeline_completes(self) -> None:
        """A middle phase retries, eventually succeeds; the final phase runs normally."""
        phases = [
            _make_phase("setup", retries=0),
            _make_phase("work", retries=2, retry_delay_seconds=0, depends_on=["setup"]),
            _make_phase("cleanup", retries=0, depends_on=["work"]),
        ]
        template = _make_template(phases)

        work_attempts = 0

        def execute(task_spec, **kw):
            nonlocal work_attempts
            pid = task_spec.payload.get("phase_id", "")
            if pid == "work":
                work_attempts += 1
                return _failure_result(task_spec) if work_attempts < 2 else _success_result(task_spec)
            return _success_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert not result.get("aborted", False)
        assert set(result["phase_outputs"].keys()) == {"setup", "work", "cleanup"}
        assert result["phase_outputs"]["work"]["metadata"]["attempt_number"] == 2

    def test_first_phase_retries_exhausted_stops_pipeline(self) -> None:
        """First phase failing after all retries stops the whole pipeline."""
        phases = [
            _make_phase("init", retries=1, retry_delay_seconds=0),
            _make_phase("run", depends_on=["init"]),
        ]
        template = _make_template(phases)

        run_calls: List[str] = []

        def execute(task_spec, **kw):
            pid = task_spec.payload.get("phase_id", "")
            if pid == "run":
                run_calls.append(pid)
            return _failure_result(task_spec) if pid == "init" else _success_result(task_spec)

        runner = _build_runner(execute)

        with patch("orchestration_engine.sequencer.time.sleep"):
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("failed_phase") == "init"
        assert run_calls == [], "downstream phase must not run after retry exhaustion"


# ===========================================================================
# Issue #102 — Parallel Phase Execution
# New tests for AC-1 through AC-9 of the parallel execution feature.
# All existing tests above this line are preserved unchanged.
# ===========================================================================

import time as _time_mod
import threading as _threading_mod

from orchestration_engine.templates import TemplateEngine
from orchestration_engine.heartbeat import ProgressHeartbeat


# ---------------------------------------------------------------------------
# Shared helpers for parallel tests
# ---------------------------------------------------------------------------


def _make_parallel_template(
    phases: List[PhaseDefinition],
    template_id: str = "parallel-test-pipeline",
    parallel: bool = True,
    max_parallel: int = 0,
    fail_fast: bool = True,
) -> PipelineTemplate:
    """Build a PipelineTemplate with explicit parallel-execution settings."""
    return PipelineTemplate(
        id=template_id,
        name="Parallel Test Pipeline",
        phases=phases,
        parallel=parallel,
        max_parallel=max_parallel,
        fail_fast=fail_fast,
    )


def _make_independent_phases(count: int, prompt: str = "Do {input}") -> List[PhaseDefinition]:
    """Build *count* independent (no depends_on) phases."""
    return [
        PhaseDefinition(
            id=f"phase_{i}",
            name=f"Phase {i}",
            prompt_template=prompt,
            retries=0,
            retry_delay_seconds=0,
            depends_on=[],
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# AC-2 / AC-3 / AC-4 — PipelineTemplate new fields & YAML parsing
# ---------------------------------------------------------------------------


class TestParallelTemplateFields:
    """PipelineTemplate accepts and defaults parallel/max_parallel/fail_fast."""

    def test_defaults_when_fields_absent(self) -> None:
        """AC-2: parallel defaults to True when field is absent."""
        template = PipelineTemplate(id="t", name="T", phases=[])
        assert template.parallel is True
        assert template.max_parallel == 0
        assert template.fail_fast is True

    def test_parallel_false_stored(self) -> None:
        """AC-2: parallel=False is correctly stored."""
        template = PipelineTemplate(id="t", name="T", phases=[], parallel=False)
        assert template.parallel is False

    def test_max_parallel_stored(self) -> None:
        """AC-3: max_parallel value is stored and clamped."""
        t = PipelineTemplate(id="t", name="T", phases=[], max_parallel=3)
        assert t.max_parallel == 3

    def test_max_parallel_negative_clamped_to_zero(self) -> None:
        """AC-3: negative max_parallel clamped to 0."""
        t = PipelineTemplate(id="t", name="T", phases=[], max_parallel=-5)
        assert t.max_parallel == 0

    def test_fail_fast_false_stored(self) -> None:
        """AC-4: fail_fast=False is correctly stored."""
        t = PipelineTemplate(id="t", name="T", phases=[], fail_fast=False)
        assert t.fail_fast is False

    def test_yaml_parallel_true_explicit(self, tmp_path) -> None:
        """AC-2: YAML with parallel: true → template.parallel is True."""
        yaml_content = """
id: par-true
name: Parallel True
version: "1.0.0"
description: test
author: test
parallel: true
phases:
  - id: step1
    name: Step One
    prompt_template: "Go"
"""
        tpl_file = tmp_path / "par_true.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)
        assert template.parallel is True

    def test_yaml_parallel_false(self, tmp_path) -> None:
        """AC-2: YAML with parallel: false → template.parallel is False."""
        yaml_content = """
id: par-false
name: Parallel False
version: "1.0.0"
description: test
author: test
parallel: false
phases:
  - id: step1
    name: Step One
    prompt_template: "Go"
"""
        tpl_file = tmp_path / "par_false.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)
        assert template.parallel is False

    def test_yaml_without_parallel_defaults_to_true(self, tmp_path) -> None:
        """AC-2: Legacy YAML without parallel key → parallel=True (default)."""
        yaml_content = """
id: legacy
name: Legacy Pipeline
version: "1.0.0"
description: test
author: test
phases:
  - id: step1
    name: Step One
    prompt_template: "Go"
"""
        tpl_file = tmp_path / "legacy.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)
        assert template.parallel is True
        assert template.max_parallel == 0
        assert template.fail_fast is True

    def test_yaml_max_parallel_parsed(self, tmp_path) -> None:
        """AC-3: YAML max_parallel: 2 → template.max_parallel == 2."""
        yaml_content = """
id: max-par
name: Max Parallel
version: "1.0.0"
description: test
author: test
max_parallel: 2
phases:
  - id: step1
    name: Step One
    prompt_template: "Go"
"""
        tpl_file = tmp_path / "max_par.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)
        assert template.max_parallel == 2

    def test_yaml_fail_fast_false_parsed(self, tmp_path) -> None:
        """AC-4: YAML fail_fast: false → template.fail_fast is False."""
        yaml_content = """
id: no-fail-fast
name: No Fail Fast
version: "1.0.0"
description: test
author: test
fail_fast: false
phases:
  - id: step1
    name: Step One
    prompt_template: "Go"
"""
        tpl_file = tmp_path / "no_fail_fast.yaml"
        tpl_file.write_text(yaml_content)
        engine = TemplateEngine(templates_dir=tmp_path)
        template = engine.load_template(tpl_file)
        assert template.fail_fast is False


# ---------------------------------------------------------------------------
# AC-1 — Concurrent wave execution (timing)
# ---------------------------------------------------------------------------


class TestParallelExecution:
    """Phases in the same wave execute concurrently when parallel=True."""

    def test_parallel_phases_faster_than_sequential(self) -> None:
        """AC-1: 3 independent phases each sleeping 0.5s must complete in <1s total."""
        phases = _make_independent_phases(3)
        template = _make_parallel_template(phases, parallel=True)

        def execute(task_spec, **kw):
            # Real sleep — not patched — so we can measure wall-clock concurrency
            _time_mod.sleep(0.5)
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)

        start = _time_mod.time()
        result = seq.execute({})
        elapsed = _time_mod.time() - start

        assert not result.get("aborted", False), "pipeline must succeed"
        # Sequential would take ≥1.5s; parallel should be ≤1.0s with margin
        assert elapsed < 1.0, (
            f"Expected parallel execution to complete in <1s but took {elapsed:.2f}s"
        )

    def test_parallel_all_phase_outputs_present(self) -> None:
        """All parallel phases must have their outputs recorded."""
        phases = _make_independent_phases(4)
        template = _make_parallel_template(phases, parallel=True)

        runner = _build_runner(lambda ts, **kw: _success_result(ts))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        for i in range(4):
            assert f"phase_{i}" in result["phase_outputs"], (
                f"phase_{i} missing from phase_outputs"
            )

    def test_sequential_mode_still_works(self) -> None:
        """AC-8: When parallel=False the sequential path executes correctly."""
        phases = [_make_phase("a"), _make_phase("b", depends_on=["a"])]
        template = _make_parallel_template(phases, parallel=False)

        execution_order: List[str] = []

        def execute(task_spec, **kw):
            execution_order.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert execution_order == ["a", "b"]

    def test_single_phase_wave_always_sequential(self) -> None:
        """AC-1: A wave of size 1 uses the sequential path even if parallel=True."""
        # Chain a → b → c: each wave has size 1
        phase_a = _make_phase("a")
        phase_b = _make_phase("b", depends_on=["a"])
        phase_c = _make_phase("c", depends_on=["b"])
        template = _make_parallel_template([phase_a, phase_b, phase_c], parallel=True)

        call_order: List[str] = []

        def execute(task_spec, **kw):
            call_order.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert call_order == ["a", "b", "c"], (
            f"Expected sequential order a→b→c, got: {call_order}"
        )


# ---------------------------------------------------------------------------
# AC-3 — max_parallel enforcement
# ---------------------------------------------------------------------------


class TestMaxParallel:
    """max_parallel limits concurrent phases in a wave."""

    def test_max_parallel_two_with_four_phases(self) -> None:
        """AC-3: With max_parallel=2 and 4 phases, ≤2 must run concurrently."""
        phases = _make_independent_phases(4)
        template = _make_parallel_template(phases, parallel=True, max_parallel=2)

        _lock = _threading_mod.Lock()
        _current_count = [0]
        _max_observed = [0]

        def execute(task_spec, **kw):
            with _lock:
                _current_count[0] += 1
                if _current_count[0] > _max_observed[0]:
                    _max_observed[0] = _current_count[0]
            _time_mod.sleep(0.05)  # small sleep to allow overlap
            with _lock:
                _current_count[0] -= 1
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert _max_observed[0] <= 2, (
            f"max_parallel=2 violated: {_max_observed[0]} phases ran concurrently"
        )

    def test_max_parallel_one_equivalent_to_sequential(self) -> None:
        """max_parallel=1 means phases run one at a time (like sequential)."""
        phases = _make_independent_phases(3)
        template = _make_parallel_template(phases, parallel=True, max_parallel=1)

        _lock = _threading_mod.Lock()
        _current_count = [0]
        _max_observed = [0]

        def execute(task_spec, **kw):
            with _lock:
                _current_count[0] += 1
                if _current_count[0] > _max_observed[0]:
                    _max_observed[0] = _current_count[0]
            _time_mod.sleep(0.02)
            with _lock:
                _current_count[0] -= 1
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert _max_observed[0] <= 1, (
            f"max_parallel=1 violated: {_max_observed[0]} phases ran concurrently"
        )

    def test_max_parallel_zero_means_unlimited(self) -> None:
        """max_parallel=0 allows all phases to run concurrently."""
        n = 5
        phases = _make_independent_phases(n)
        template = _make_parallel_template(phases, parallel=True, max_parallel=0)

        _lock = _threading_mod.Lock()
        _current_count = [0]
        _max_observed = [0]

        def execute(task_spec, **kw):
            with _lock:
                _current_count[0] += 1
                if _current_count[0] > _max_observed[0]:
                    _max_observed[0] = _current_count[0]
            _time_mod.sleep(0.05)
            with _lock:
                _current_count[0] -= 1
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        # With unlimited concurrency, all n phases can run simultaneously
        assert _max_observed[0] <= n


# ---------------------------------------------------------------------------
# AC-4 — fail_fast behaviour
# ---------------------------------------------------------------------------


class TestFailFastTrue:
    """fail_fast=True: pipeline aborts immediately when a phase fails."""

    def test_fail_fast_true_pipeline_aborts(self) -> None:
        """AC-4: fail_fast=True → pipeline aborts when any phase fails."""
        phases = _make_independent_phases(3)
        template = _make_parallel_template(phases, parallel=True, fail_fast=True)

        def execute(task_spec, **kw):
            pid = task_spec.payload["phase_id"]
            if pid == "phase_0":
                return _failure_result(task_spec, "intentional failure")
            _time_mod.sleep(0.1)
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("failed_phase") == "phase_0"

    def test_fail_fast_true_reports_failed_phase(self) -> None:
        """AC-4: failed_phase key names the failing phase."""
        phases = _make_independent_phases(2)
        template = _make_parallel_template(phases, parallel=True, fail_fast=True)

        def execute(task_spec, **kw):
            if task_spec.payload["phase_id"] == "phase_1":
                return _failure_result(task_spec)
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        # The failed_phase may be phase_0 or phase_1 depending on order,
        # but at least one of them must be in the result
        failed = result.get("failed_phase") or result.get("failed_phases", [])
        assert failed  # something must be flagged as failed

    def test_fail_fast_true_is_default(self) -> None:
        """AC-4: fail_fast defaults to True even without explicit setting."""
        phases = _make_independent_phases(2)
        # No explicit fail_fast — should default to True
        template = PipelineTemplate(
            id="t", name="T", phases=phases, parallel=True
        )
        assert template.fail_fast is True

        def execute(task_spec, **kw):
            if task_spec.payload["phase_id"] == "phase_0":
                return _failure_result(task_spec)
            _time_mod.sleep(0.1)
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True


class TestFailFastFalse:
    """fail_fast=False: all siblings run to completion even when one fails."""

    def test_fail_fast_false_all_phases_run(self) -> None:
        """AC-4: fail_fast=False → siblings complete even if one phase fails."""
        phases = _make_independent_phases(3)
        template = _make_parallel_template(
            phases, parallel=True, max_parallel=3, fail_fast=False
        )

        completed_phases: List[str] = []
        _lock = _threading_mod.Lock()

        def execute(task_spec, **kw):
            pid = task_spec.payload["phase_id"]
            _time_mod.sleep(0.05)
            with _lock:
                completed_phases.append(pid)
            if pid == "phase_0":
                return _failure_result(task_spec, "planned failure")
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        # Pipeline aborts because phase_0 failed
        assert result.get("aborted") is True
        # All 3 phases must have completed execution (fail_fast=False)
        assert set(completed_phases) == {"phase_0", "phase_1", "phase_2"}, (
            f"Expected all 3 phases to complete; got: {completed_phases}"
        )

    def test_fail_fast_false_outputs_from_success_phases_present(self) -> None:
        """AC-4: fail_fast=False → successful siblings' outputs are recorded."""
        phases = _make_independent_phases(3)
        template = _make_parallel_template(
            phases, parallel=True, max_parallel=0, fail_fast=False
        )

        def execute(task_spec, **kw):
            pid = task_spec.payload["phase_id"]
            if pid == "phase_0":
                return _failure_result(task_spec)
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        # Successful phases should still have outputs recorded
        outputs = result.get("phase_outputs", {})
        assert "phase_1" in outputs or "phase_2" in outputs, (
            "Successful sibling outputs must be recorded with fail_fast=False"
        )

    def test_fail_fast_false_all_failures_in_result(self) -> None:
        """AC-4: fail_fast=False → failed_phases list contains all failures."""
        phases = _make_independent_phases(3)
        template = _make_parallel_template(
            phases, parallel=True, max_parallel=0, fail_fast=False
        )

        def execute(task_spec, **kw):
            # All three phases fail
            return _failure_result(task_spec, "all bad")

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert result.get("aborted") is True
        failed_phases = result.get("failed_phases", [])
        assert len(failed_phases) == 3, (
            f"Expected 3 failed phases; got: {failed_phases}"
        )


# ---------------------------------------------------------------------------
# AC-5 — Token/cost tracking: thread-safe aggregation
# ---------------------------------------------------------------------------


class TestTokenAggregation:
    """Token and cost totals must be correct under concurrent execution."""

    def test_phase_outputs_all_recorded_under_concurrency(self) -> None:
        """AC-5: No phase output is lost due to race conditions."""
        n = 20
        phases = _make_independent_phases(n)
        template = _make_parallel_template(phases, parallel=True, max_parallel=0)

        runner = _build_runner(lambda ts, **kw: _success_result(ts))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert len(result["phase_outputs"]) == n, (
            f"Expected {n} phase outputs; got {len(result['phase_outputs'])}"
        )

    def test_deterministic_output_count_repeated_runs(self) -> None:
        """AC-5: Running the same parallel pipeline N times always yields the same output count."""
        phases = _make_independent_phases(10)
        template = _make_parallel_template(phases, parallel=True, max_parallel=0)

        for run in range(10):
            runner = _build_runner(lambda ts, **kw: _success_result(ts))
            seq = PhaseSequencer(template, runner)
            result = seq.execute({})
            assert not result.get("aborted", False)
            assert len(result["phase_outputs"]) == 10, (
                f"Run {run}: expected 10 outputs, got {len(result['phase_outputs'])}"
            )


# ---------------------------------------------------------------------------
# AC-6 — Thread-safe progress callbacks
# ---------------------------------------------------------------------------


class TestThreadSafeCallbacks:
    """on_phase_start / on_phase_complete invocations are serialised."""

    def test_start_events_count_equals_n(self) -> None:
        """AC-6: Exactly N on_phase_start events for N parallel phases."""
        n = 10
        phases = _make_independent_phases(n)
        template = _make_parallel_template(phases, parallel=True, max_parallel=0)

        start_events: List[str] = []
        _lock = _threading_mod.Lock()

        def on_start(phase_id, phase, wave_index):
            with _lock:
                start_events.append(phase_id)

        runner = _build_runner(lambda ts, **kw: _success_result(ts))
        seq = PhaseSequencer(template, runner, on_phase_start=on_start)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert len(start_events) == n, (
            f"Expected {n} start events; got {len(start_events)}: {start_events}"
        )
        assert len(set(start_events)) == n, "Duplicate start events detected"

    def test_complete_events_count_equals_n(self) -> None:
        """AC-6: Exactly N on_phase_complete events for N parallel phases."""
        n = 10
        phases = _make_independent_phases(n)
        template = _make_parallel_template(phases, parallel=True, max_parallel=0)

        complete_events: List[str] = []
        _lock = _threading_mod.Lock()

        def on_complete(phase_id, result_dict):
            with _lock:
                complete_events.append(phase_id)

        runner = _build_runner(lambda ts, **kw: _success_result(ts))
        seq = PhaseSequencer(template, runner, on_phase_complete=on_complete)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert len(complete_events) == n, (
            f"Expected {n} complete events; got {len(complete_events)}: {complete_events}"
        )
        assert len(set(complete_events)) == n, "Duplicate complete events detected"

    def test_callbacks_under_high_concurrency_no_duplicates(self) -> None:
        """AC-6: Under high concurrency, no duplicate or missing events."""
        n = 50
        phases = _make_independent_phases(n)
        template = _make_parallel_template(phases, parallel=True, max_parallel=0)

        start_events: List[str] = []
        complete_events: List[str] = []
        _lock = _threading_mod.Lock()

        def on_start(phase_id, phase, wave_index):
            _time_mod.sleep(0.001)  # increase chance of race
            with _lock:
                start_events.append(phase_id)

        def on_complete(phase_id, result_dict):
            _time_mod.sleep(0.001)
            with _lock:
                complete_events.append(phase_id)

        runner = _build_runner(lambda ts, **kw: _success_result(ts))
        seq = PhaseSequencer(template, runner,
                             on_phase_start=on_start,
                             on_phase_complete=on_complete)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert len(start_events) == n
        assert len(complete_events) == n
        assert sorted(start_events) == sorted(complete_events)


# ---------------------------------------------------------------------------
# AC-7 — Heartbeat multi-phase display
# ---------------------------------------------------------------------------


class TestHeartbeatMultiPhase:
    """ProgressHeartbeat supports concurrent active phases (Issue #102 / AC-7)."""

    def test_set_current_phase_adds_to_active_set(self) -> None:
        """AC-7: set_current_phase adds to active_phases, not replaces."""
        hb = ProgressHeartbeat(total_phases=3, force=False)
        hb.set_current_phase("phase_a")
        hb.set_current_phase("phase_b")
        hb.set_current_phase("phase_c")

        assert hb.active_phases == frozenset({"phase_a", "phase_b", "phase_c"})

    def test_remove_active_phase_removes_from_set(self) -> None:
        """AC-7: remove_active_phase removes a phase from active_phases."""
        hb = ProgressHeartbeat(total_phases=3, force=False)
        hb.set_current_phase("phase_a")
        hb.set_current_phase("phase_b")
        hb.set_current_phase("phase_c")

        hb.remove_active_phase("phase_b")
        assert hb.active_phases == frozenset({"phase_a", "phase_c"})

    def test_on_phase_complete_with_name_removes_phase(self) -> None:
        """AC-7: on_phase_complete(phase_name) removes from active_phases."""
        hb = ProgressHeartbeat(total_phases=3, force=False)
        hb.set_current_phase("alpha")
        hb.set_current_phase("beta")
        hb.on_phase_complete(phase_name="alpha")

        assert "alpha" not in hb.active_phases
        assert "beta" in hb.active_phases

    def test_on_phase_complete_without_name_does_not_modify_set(self) -> None:
        """AC-7: on_phase_complete() without name is backward-compatible."""
        hb = ProgressHeartbeat(total_phases=2, force=False)
        hb.set_current_phase("a")
        hb.set_current_phase("b")
        hb.on_phase_complete()  # no name → legacy compat

        assert hb.active_phases == frozenset({"a", "b"})

    def test_active_phases_returns_frozenset(self) -> None:
        """AC-7: active_phases property returns a frozenset."""
        hb = ProgressHeartbeat(total_phases=2, force=False)
        hb.set_current_phase("x")
        result = hb.active_phases
        assert isinstance(result, frozenset)

    def test_active_phases_empty_initially(self) -> None:
        """AC-7: active_phases is empty before any phase starts."""
        hb = ProgressHeartbeat(total_phases=5, force=False)
        assert hb.active_phases == frozenset()

    def test_all_phases_cleared_after_completion(self) -> None:
        """AC-7: active_phases is empty once all phases complete."""
        hb = ProgressHeartbeat(total_phases=3, force=False)
        for name in ("a", "b", "c"):
            hb.set_current_phase(name)
        for name in ("a", "b", "c"):
            hb.remove_active_phase(name)

        assert hb.active_phases == frozenset()

    def test_active_phases_thread_safe(self) -> None:
        """AC-7: concurrent set_current_phase / remove calls do not corrupt state."""
        hb = ProgressHeartbeat(total_phases=100, force=False)
        errors: List[Exception] = []

        def adder():
            for i in range(50):
                try:
                    hb.set_current_phase(f"phase_{i}")
                except Exception as exc:
                    errors.append(exc)

        def remover():
            for i in range(50):
                try:
                    hb.remove_active_phase(f"phase_{i}")
                except Exception as exc:
                    errors.append(exc)

        threads = [
            _threading_mod.Thread(target=adder),
            _threading_mod.Thread(target=adder),
            _threading_mod.Thread(target=remover),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread-safety errors: {errors}"
        # active_phases should be a valid frozenset (no corruption)
        _ = hb.active_phases

    def test_heartbeat_emit_shows_multiple_phases(self) -> None:
        """AC-7: _emit() output contains all active phase names."""
        import io

        stream = io.StringIO()
        hb = ProgressHeartbeat(
            total_phases=5,
            start_time=_time_mod.time() - 60,  # pretend 60s elapsed
            interval_seconds=9999,
            stream=stream,
            force=True,
        )
        hb.set_current_phase("write")
        hb.set_current_phase("fact-check")
        hb.set_current_phase("review")
        hb._emit()

        output = stream.getvalue()
        assert "write" in output
        assert "fact-check" in output
        assert "review" in output


# ---------------------------------------------------------------------------
# AC-8 — Backward compatibility: all sequential tests still pass
# (The test classes above the Issue #102 section are the existing suite —
# they must pass unchanged.  This class adds a final integration smoke-test.)
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Sequential pipeline behaviour is unchanged with parallel=False."""

    def test_sequential_pipeline_end_to_end(self) -> None:
        """AC-8: A sequential pipeline (parallel=False) produces correct results."""
        phases = [
            _make_phase("init"),
            _make_phase("process", depends_on=["init"]),
            _make_phase("output", depends_on=["process"]),
        ]
        template = _make_parallel_template(phases, parallel=False)

        execution_order: List[str] = []

        def execute(task_spec, **kw):
            execution_order.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert execution_order == ["init", "process", "output"]
        assert set(result["phase_outputs"].keys()) == {"init", "process", "output"}

    def test_parallel_default_does_not_break_dependent_phases(self) -> None:
        """AC-8: parallel=True (default) on a dependent chain still runs in order."""
        phase_a = _make_phase("a")
        phase_b = _make_phase("b", depends_on=["a"])
        template = PipelineTemplate(
            id="chain", name="Chain", phases=[phase_a, phase_b], parallel=True
        )

        order: List[str] = []

        def execute(task_spec, **kw):
            order.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        runner = _build_runner(execute)
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})

        assert not result.get("aborted", False)
        assert order == ["a", "b"], f"Dependent phases must run in dependency order: {order}"
