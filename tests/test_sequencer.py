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
