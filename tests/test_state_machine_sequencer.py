"""Tests for StateMachineSequencer — Issue #234 + Issue #235.

Covers:
- AC-1:  Single phase with no transitions executes once and returns result
- AC-2:  Two-phase chain via 'success' transition runs both phases in order
- AC-3:  Failure transition routes to error handler phase instead of success path
- AC-4:  Timeout transition routes to dedicated timeout handler
- AC-5:  Missing transition (terminal state) halts execution cleanly
- AC-6:  default_transitions on template applies to all phases that don't override
- AC-7:  Phase-level transitions override default_transitions on a per-key basis
- AC-8:  on_phase_start/on_phase_complete callbacks called once per executed phase
- AC-9:  on_pipeline_start hook is called once before any phase executes
- AC-10: on_pipeline_complete hook is called once after execution completes
- AC-11: phase_outputs contains only executed phases
- AC-12: final_output is the result of the last executed phase
- AC-13: Cycle detection logs a warning and stops execution (no infinite loop)
- AC-14: Unknown transition target raises KeyError with descriptive message
- AC-15: StateMachineSequencer is exported from orchestration_engine package
- AC-16: Templates without transitions use PhaseSequencer (not StateMachineSequencer)
         — CLI auto-detection logic test (indirect via has_transitions check)
- AC-17: Three-phase chain: A→[success]→B→[success]→C (no C transition), all run
- AC-18: skipped outcome routes via 'skipped' transition key

Loop / iteration support (Issue #235):
- AC-19: Phase with max_iterations > 0 can be revisited up to that many times
- AC-20: Exceeding max_iterations aborts with abort_reason=MAX_ITERATIONS_EXCEEDED
- AC-21: iteration_history accumulates prior results on each loop re-entry
- AC-22: iteration_counts reflects total executions per phase
- AC-23: Callbacks fire once per iteration (not once per unique phase)
- AC-24: result["metadata"]["iteration"] is annotated on each phase result
- AC-25: execute() result includes iteration_history and iteration_counts keys
- AC-26: Non-loop phase (max_iterations==0) still triggers legacy cycle guard
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from orchestration_engine.schemas import TaskError, TaskResult, TaskState, TaskType
from orchestration_engine.sequencer import StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate
from orchestration_engine.transitions import PhaseOutcome


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_sequencer.py)
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str,
    prompt: str = "Do something",
    transitions: Optional[Dict[str, str]] = None,
    depends_on: Optional[List[str]] = None,
    max_iterations: int = 0,
) -> PhaseDefinition:
    """Build a minimal PhaseDefinition with optional transition config.

    Args:
        phase_id:       Unique phase identifier.
        prompt:         Prompt template string.
        transitions:    Outcome → phase_id mapping.
        depends_on:     List of phase IDs this phase depends on.
        max_iterations: Maximum loop iterations (0 = legacy cycle guard, >0 = loop phase).
    """
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        transitions=transitions or {},
        depends_on=depends_on or [],
        max_iterations=max_iterations,
    )


def _make_template(
    phases: List[PhaseDefinition],
    template_id: str = "state-machine-test",
    default_transitions: Optional[Dict[str, str]] = None,
) -> PipelineTemplate:
    """Build a PipelineTemplate with optional default_transitions."""
    return PipelineTemplate(
        id=template_id,
        name="State Machine Test Pipeline",
        phases=phases,
        default_transitions=default_transitions or {},
    )


def _success_result(task_spec, **kwargs) -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": f"Output of {task_spec.payload.get('phase_id', '?')}"},
    )


def _failure_result(task_spec, message: str = "Simulated failure", **kwargs) -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.FAILED,
        confidence=0.0,
        result={"text": ""},
        errors=[TaskError(code="EXEC_ERR", message=message, severity="error")],
    )


def _timeout_result(task_spec, **kwargs) -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.FAILED,
        confidence=0.0,
        result={"text": ""},
        errors=[TaskError(code="timeout", message="timed out", severity="error")],
    )


def _cancelled_result(task_spec, **kwargs) -> TaskResult:
    """Result with state=cancelled → outcome SKIPPED."""
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.CANCELLED,
        confidence=0.0,
        result={"text": ""},
    )


def _build_runner(execute_fn: Callable) -> MagicMock:
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


def _make_sequencer(
    template: PipelineTemplate,
    execute_fn: Callable,
    on_phase_start: Optional[Callable] = None,
    on_phase_complete: Optional[Callable] = None,
    on_pipeline_start: Optional[Callable] = None,
    on_pipeline_complete: Optional[Callable] = None,
) -> StateMachineSequencer:
    """Convenience: build a StateMachineSequencer with a mock runner."""
    runner = _build_runner(execute_fn)
    return StateMachineSequencer(
        template=template,
        runner=runner,
        on_phase_start=on_phase_start,
        on_phase_complete=on_phase_complete,
        on_pipeline_start=on_pipeline_start,
        on_pipeline_complete=on_pipeline_complete,
    )


# ---------------------------------------------------------------------------
# AC-1 — Single phase, no transitions
# ---------------------------------------------------------------------------


class TestSinglePhaseNoTransitions:
    """A single phase with no transitions is a terminal phase on any outcome."""

    def test_single_success_phase_executes_and_returns(self) -> None:
        phase = _make_phase("only")
        template = _make_template([phase])
        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert result["phase_outputs"]["only"]["state"] == "success"
        assert result["final_output"]["state"] == "success"

    def test_single_failed_phase_no_transition_is_terminal(self) -> None:
        phase = _make_phase("only")
        template = _make_template([phase])
        seq = _make_sequencer(template, _failure_result)
        result = seq.execute({})

        # No 'failed' transition → terminal state, NOT an abort
        assert "only" in result["phase_outputs"]
        assert result["phase_outputs"]["only"]["state"] == "failed"
        assert result["final_output"]["state"] == "failed"
        # StateMachineSequencer does NOT set aborted=True just because a phase failed
        # and there is no failure transition — it just terminates the chain.
        assert not result.get("aborted")

    def test_empty_template_returns_empty_result(self) -> None:
        template = _make_template([])
        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert result == {"phase_outputs": {}, "final_output": {}}


# ---------------------------------------------------------------------------
# AC-2 — Two-phase success chain
# ---------------------------------------------------------------------------


class TestTwoPhaseSuccessChain:
    """A→[success]→B: both phases execute; B has no transition (terminal)."""

    def _two_phase_template(self) -> PipelineTemplate:
        phase_a = _make_phase("fetch", transitions={"success": "process"})
        phase_b = _make_phase("process")
        return _make_template([phase_a, phase_b])

    def test_both_phases_execute_in_order(self) -> None:
        template = self._two_phase_template()
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert executed == ["fetch", "process"]

    def test_phase_outputs_contains_both_phases(self) -> None:
        template = self._two_phase_template()
        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert set(result["phase_outputs"].keys()) == {"fetch", "process"}

    def test_final_output_is_last_phase_result(self) -> None:
        template = self._two_phase_template()
        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        # final_output should be the result of 'process' (the last executed)
        assert result["final_output"] == result["phase_outputs"]["process"]

    def test_no_aborted_flag_on_success_chain(self) -> None:
        template = self._two_phase_template()
        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert not result.get("aborted")


# ---------------------------------------------------------------------------
# AC-3 — Failure transition routes to error handler
# ---------------------------------------------------------------------------


class TestFailureTransitionRouting:
    """On failure, the 'failed' transition key routes to an error handler phase."""

    def _build_template(self) -> PipelineTemplate:
        """fetch → success: process | failed: error_handler; process → terminal."""
        phase_fetch = _make_phase(
            "fetch",
            transitions={"success": "process", "failed": "error_handler"},
        )
        phase_process = _make_phase("process")
        phase_error = _make_phase("error_handler")
        return _make_template([phase_fetch, phase_process, phase_error])

    def test_failure_routes_to_error_handler(self) -> None:
        template = self._build_template()
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            if pid == "fetch":
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # fetch fails → error_handler runs, process never runs
        assert executed == ["fetch", "error_handler"]
        assert "error_handler" in result["phase_outputs"]
        assert "process" not in result["phase_outputs"]

    def test_success_route_skips_error_handler(self) -> None:
        template = self._build_template()
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert executed == ["fetch", "process"]
        assert "error_handler" not in result["phase_outputs"]


# ---------------------------------------------------------------------------
# AC-4 — Timeout transition
# ---------------------------------------------------------------------------


class TestTimeoutTransitionRouting:
    """A 'timeout' transition key routes to a dedicated timeout handler."""

    def test_timeout_outcome_routes_correctly(self) -> None:
        phase_task = _make_phase(
            "long_task",
            transitions={"success": "publish", "timeout": "timeout_handler"},
        )
        phase_publish = _make_phase("publish")
        phase_timeout_handler = _make_phase("timeout_handler")
        template = _make_template([phase_task, phase_publish, phase_timeout_handler])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            if pid == "long_task":
                return _timeout_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert executed == ["long_task", "timeout_handler"]
        assert "publish" not in result["phase_outputs"]


# ---------------------------------------------------------------------------
# AC-5 — Terminal state when no transition matches
# ---------------------------------------------------------------------------


class TestTerminalStateNoTransition:
    """Execution halts cleanly when there is no transition for the current outcome."""

    def test_success_with_no_success_transition_is_terminal(self) -> None:
        # Phase has only a 'failed' transition; success has none → terminal
        phase = _make_phase("work", transitions={"failed": "error"})
        phase_error = _make_phase("error")
        template = _make_template([phase, phase_error])
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # Only 'work' runs; no transition for 'success', so chain terminates
        assert executed == ["work"]
        assert "error" not in result["phase_outputs"]


# ---------------------------------------------------------------------------
# AC-6 — default_transitions on template
# ---------------------------------------------------------------------------


class TestDefaultTransitions:
    """template.default_transitions applies to phases with no per-phase override."""

    def test_default_transitions_applied_to_all_phases(self) -> None:
        """A pipeline-level default transition routes all phases uniformly."""
        phase_a = _make_phase("a")  # no per-phase transitions
        phase_b = _make_phase("b")  # no per-phase transitions
        phase_c = _make_phase("c")  # terminal
        template = _make_template(
            [phase_a, phase_b, phase_c],
            default_transitions={"success": "b"},  # only a→b is meaningful here
        )
        # We need a custom template to test a→b→c via defaults
        # Let's build it properly: a→b (default success), b→c (default success), c terminal
        phase_a2 = _make_phase("a")
        phase_b2 = _make_phase("b")
        phase_c2 = _make_phase("c")
        # default success transition goes to next phase alphabetically — not possible
        # Instead, test that default_transitions makes 'a' go to 'b'
        template2 = _make_template(
            [phase_a2, phase_b2, phase_c2],
            default_transitions={"success": "b"},
        )
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            return _success_result(task_spec)

        seq = _make_sequencer(template2, execute_fn)
        result = seq.execute({})

        # a →[success, default]→ b →[success, default]→ b (cycle detected!) → stops
        # Actually: a runs, success transition → b, b runs, success transition → b (cycle!)
        # The cycle guard should fire here and stop at b
        assert "a" in result["phase_outputs"]
        assert "b" in result["phase_outputs"]
        # Cycle guard fires on second visit to 'b', stopping execution
        assert executed.count("b") == 1  # visited only once due to cycle guard

    def test_default_transitions_routing_chain(self) -> None:
        """default_transitions 'success' routes a→b; b has no transitions → terminal."""
        phase_a = _make_phase("start")
        phase_b = _make_phase("end")
        template = _make_template(
            [phase_a, phase_b],
            default_transitions={"success": "end"},
        )
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            executed.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # 'start' → success → 'end' (default) → success → 'end' (cycle guard fires)
        # So: start runs once, end runs once, cycle guard stops
        assert executed == ["start", "end"]


# ---------------------------------------------------------------------------
# AC-7 — Phase-level transitions override default_transitions per key
# ---------------------------------------------------------------------------


class TestPhaseOverridesDefaultTransitions:
    """Phase.transitions overrides default_transitions on a per-key basis."""

    def test_phase_override_replaces_default_for_that_key(self) -> None:
        """Phase 'a' overrides default 'failed' transition with its own target.

        default_transitions has failed→default_fail.
        Phase 'a' overrides that with failed→custom_fail.
        Result: 'a' failure routes to 'custom_fail', NOT 'default_fail'.
        'custom_fail' succeeds → no 'success' in defaults → terminal.
        """
        phase_a = _make_phase("a", transitions={"failed": "custom_fail"})
        phase_default_fail = _make_phase("default_fail")
        phase_custom_fail = _make_phase("custom_fail")
        template = _make_template(
            [phase_a, phase_default_fail, phase_custom_fail],
            default_transitions={"failed": "default_fail"},
        )
        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            if pid == "a":
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # 'a' fails → phase overrides default → routes to 'custom_fail'
        # 'custom_fail' succeeds → no 'success' in default_transitions → terminal
        assert "a" in executed
        assert "custom_fail" in executed
        assert "default_fail" not in executed

    def test_phase_inherits_unoverridden_keys_from_default(self) -> None:
        """Phase only overrides 'success'; 'failed' still uses default."""
        phase_a = _make_phase(
            "a",
            transitions={"success": "b"},  # only overrides success
        )
        phase_b = _make_phase("b")
        phase_fallback = _make_phase("fallback")
        template = _make_template(
            [phase_a, phase_b, phase_fallback],
            default_transitions={"failed": "fallback"},
        )

        seq = _make_sequencer(template, _failure_result)
        result = seq.execute({})

        # 'a' fails → effective transitions: {failed: fallback, success: b}
        # → routes to fallback
        assert "fallback" in result["phase_outputs"]
        assert "b" not in result["phase_outputs"]


# ---------------------------------------------------------------------------
# AC-8 — Callbacks called once per executed phase
# ---------------------------------------------------------------------------


class TestCallbacksCalledOncePerPhase:
    """on_phase_start and on_phase_complete are called exactly once per phase."""

    def test_on_phase_start_called_for_each_executed_phase(self) -> None:
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b")
        template = _make_template([phase_a, phase_b])

        start_calls: List[str] = []
        seq = _make_sequencer(
            template,
            _success_result,
            on_phase_start=lambda pid, phase, wave: start_calls.append(pid),
        )
        seq.execute({})

        assert start_calls == ["a", "b"]

    def test_on_phase_complete_called_for_each_executed_phase(self) -> None:
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b")
        template = _make_template([phase_a, phase_b])

        complete_calls: List[str] = []
        seq = _make_sequencer(
            template,
            _success_result,
            on_phase_complete=lambda pid, result: complete_calls.append(pid),
        )
        seq.execute({})

        assert complete_calls == ["a", "b"]

    def test_unexecuted_phases_do_not_trigger_callbacks(self) -> None:
        """When failure routes away from 'process', 'process' gets no callback."""
        phase_fetch = _make_phase(
            "fetch",
            transitions={"success": "process", "failed": "error"},
        )
        phase_process = _make_phase("process")
        phase_error = _make_phase("error")
        template = _make_template([phase_fetch, phase_process, phase_error])

        complete_calls: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "fetch":
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(
            template,
            execute_fn,
            on_phase_complete=lambda pid, result: complete_calls.append(pid),
        )
        seq.execute({})

        assert "process" not in complete_calls
        assert complete_calls == ["fetch", "error"]


# ---------------------------------------------------------------------------
# AC-9/AC-10 — Pipeline hooks
# ---------------------------------------------------------------------------


class TestPipelineHooks:
    """on_pipeline_start and on_pipeline_complete are each called exactly once."""

    def test_on_pipeline_start_called_once(self) -> None:
        phase = _make_phase("only")
        template = _make_template([phase])
        start_calls = []
        seq = _make_sequencer(
            template,
            _success_result,
            on_pipeline_start=lambda ctx: start_calls.append(1),
        )
        seq.execute({})
        assert len(start_calls) == 1

    def test_on_pipeline_complete_called_once_on_success(self) -> None:
        phase = _make_phase("only")
        template = _make_template([phase])
        complete_calls = []
        seq = _make_sequencer(
            template,
            _success_result,
            on_pipeline_complete=lambda ctx, res: complete_calls.append(res),
        )
        seq.execute({})
        # Called once with the final result dict (not None)
        assert len(complete_calls) == 1
        assert complete_calls[0] is not None
        assert "phase_outputs" in complete_calls[0]

    def test_on_pipeline_complete_called_on_exception(self) -> None:
        """on_pipeline_complete receives None when an unhandled exception occurs.

        Note: executor exceptions are caught internally by _execute_and_wait and
        returned as FAILED results — they do NOT propagate to execute().
        To test the exception path in execute(), we make runner.queue.submit_task
        raise, which IS inside the try block and DOES propagate.
        """
        phase = _make_phase("only")
        template = _make_template([phase])
        complete_calls = []

        runner = MagicMock()
        runner.queue.submit_task.side_effect = RuntimeError("queue exploded")
        runner.queue.get_task = MagicMock(return_value=None)
        runner.executors = []

        seq = StateMachineSequencer(
            template=template,
            runner=runner,
            on_pipeline_complete=lambda ctx, res: complete_calls.append(res),
        )
        with pytest.raises(RuntimeError, match="queue exploded"):
            seq.execute({})

        assert len(complete_calls) == 1
        assert complete_calls[0] is None


# ---------------------------------------------------------------------------
# AC-11/AC-12 — phase_outputs and final_output correctness
# ---------------------------------------------------------------------------


class TestOutputAccumulation:
    """phase_outputs contains only executed phases; final_output is the last one."""

    def test_phase_outputs_only_has_executed_phases(self) -> None:
        """Phases not reached by transitions are absent from phase_outputs."""
        phase_a = _make_phase("a", transitions={"success": "c"})
        phase_b = _make_phase("b")  # never reached
        phase_c = _make_phase("c")
        template = _make_template([phase_a, phase_b, phase_c])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert set(result["phase_outputs"].keys()) == {"a", "c"}
        assert "b" not in result["phase_outputs"]

    def test_final_output_is_last_executed_phase(self) -> None:
        phase_a = _make_phase("a", transitions={"success": "c"})
        phase_b = _make_phase("b")
        phase_c = _make_phase("c")
        template = _make_template([phase_a, phase_b, phase_c])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert result["final_output"] == result["phase_outputs"]["c"]


# ---------------------------------------------------------------------------
# AC-13 — Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """Cycles are detected and execution stops with a warning (no infinite loop)."""

    def test_cycle_logs_warning_and_stops(self, caplog) -> None:
        # A → B → A (cycle)
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b", transitions={"success": "a"})  # back to a
        template = _make_template([phase_a, phase_b])

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            result = seq.execute({})

        # Both phases executed once; cycle guard fired on second visit to 'a'
        assert "a" in result["phase_outputs"]
        assert "b" in result["phase_outputs"]
        assert any("cycle" in r.message.lower() for r in caplog.records)

    def test_self_loop_is_caught(self, caplog) -> None:
        """A phase that transitions to itself is immediately caught."""
        phase_a = _make_phase("a", transitions={"success": "a"})
        template = _make_template([phase_a])

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            result = seq.execute({})

        assert "a" in result["phase_outputs"]
        assert any("cycle" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# AC-14 — Unknown transition target raises KeyError
# ---------------------------------------------------------------------------


class TestUnknownTransitionTarget:
    """Referencing a nonexistent phase ID via transitions raises a KeyError."""

    def test_missing_phase_id_in_transition_raises_key_error(self) -> None:
        phase = _make_phase("start", transitions={"success": "nonexistent_phase"})
        template = _make_template([phase])

        seq = _make_sequencer(template, _success_result)
        with pytest.raises(KeyError, match="nonexistent_phase"):
            seq.execute({})


# ---------------------------------------------------------------------------
# AC-15 — Package export
# ---------------------------------------------------------------------------


class TestPackageExport:
    """StateMachineSequencer is accessible from the top-level package."""

    def test_importable_from_package(self) -> None:
        from orchestration_engine import StateMachineSequencer as SMS  # noqa: N813
        assert SMS is StateMachineSequencer

    def test_in_package_all(self) -> None:
        import orchestration_engine
        assert "StateMachineSequencer" in orchestration_engine.__all__


# ---------------------------------------------------------------------------
# AC-17 — Three-phase linear chain
# ---------------------------------------------------------------------------


class TestThreePhaseLinearChain:
    """A→[success]→B→[success]→C (C has no transitions): all three run in order."""

    def test_three_phases_run_in_order(self) -> None:
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b", transitions={"success": "c"})
        phase_c = _make_phase("c")  # terminal
        template = _make_template([phase_a, phase_b, phase_c])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            executed.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert executed == ["a", "b", "c"]
        assert set(result["phase_outputs"].keys()) == {"a", "b", "c"}
        assert result["final_output"] == result["phase_outputs"]["c"]


# ---------------------------------------------------------------------------
# AC-18 — Skipped outcome routes via 'skipped' key
# ---------------------------------------------------------------------------


class TestSkippedOutcomeRouting:
    """A 'skipped' transition key is used when the phase outcome is SKIPPED."""

    def test_skipped_outcome_routes_to_dedicated_handler(self) -> None:
        phase_a = _make_phase(
            "a",
            transitions={"success": "b", "skipped": "skip_handler"},
        )
        phase_b = _make_phase("b")
        phase_skip = _make_phase("skip_handler")
        template = _make_template([phase_a, phase_b, phase_skip])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            executed.append(pid)
            if pid == "a":
                return _cancelled_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert executed == ["a", "skip_handler"]
        assert "b" not in result["phase_outputs"]


# ---------------------------------------------------------------------------
# _resolve_next_phase unit tests
# ---------------------------------------------------------------------------


class TestResolveNextPhase:
    """Unit tests for StateMachineSequencer._resolve_next_phase."""

    def _make_seq(self, default_transitions: dict = None) -> StateMachineSequencer:
        phase = _make_phase("dummy")
        template = _make_template([phase], default_transitions=default_transitions or {})
        runner = MagicMock()
        runner.executors = []
        runner.queue = MagicMock()
        return StateMachineSequencer(template=template, runner=runner)

    def test_returns_next_phase_for_matching_outcome(self) -> None:
        seq = self._make_seq()
        phase = _make_phase("a", transitions={"success": "b"})
        assert seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS) == "b"

    def test_returns_none_for_no_matching_outcome(self) -> None:
        seq = self._make_seq()
        phase = _make_phase("a", transitions={"success": "b"})
        assert seq._resolve_next_phase(phase, PhaseOutcome.FAILED) is None

    def test_default_transition_used_when_no_phase_override(self) -> None:
        seq = self._make_seq(default_transitions={"failed": "fallback"})
        phase = _make_phase("a")  # no phase-level transitions
        assert seq._resolve_next_phase(phase, PhaseOutcome.FAILED) == "fallback"

    def test_phase_override_takes_precedence_over_default(self) -> None:
        seq = self._make_seq(default_transitions={"success": "default_next"})
        phase = _make_phase("a", transitions={"success": "phase_next"})
        assert seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS) == "phase_next"

    def test_phase_inherits_default_for_unoverridden_keys(self) -> None:
        seq = self._make_seq(default_transitions={"failed": "default_fail"})
        phase = _make_phase("a", transitions={"success": "b"})
        # 'failed' not overridden → uses default
        assert seq._resolve_next_phase(phase, PhaseOutcome.FAILED) == "default_fail"
        # 'success' overridden → uses phase-level
        assert seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS) == "b"


# ---------------------------------------------------------------------------
# CLI auto-detection helper (AC-16 — indirect test)
# ---------------------------------------------------------------------------


class TestCliAutoDetection:
    """The has_transitions check correctly identifies templates that need SMS."""

    def test_template_with_transitions_triggers_sms(self) -> None:
        """has_transitions is True when any phase has transitions."""
        phase = _make_phase("a", transitions={"success": "b"})
        template = _make_template([phase])
        has_transitions = any(p.transitions for p in template.phases) or bool(
            template.default_transitions
        )
        assert has_transitions is True

    def test_template_with_default_transitions_triggers_sms(self) -> None:
        """has_transitions is True when default_transitions is set."""
        phase = _make_phase("a")
        template = _make_template([phase], default_transitions={"success": "b"})
        has_transitions = any(p.transitions for p in template.phases) or bool(
            template.default_transitions
        )
        assert has_transitions is True

    def test_template_without_transitions_uses_phase_sequencer(self) -> None:
        """has_transitions is False when no transitions are configured anywhere."""
        phase = _make_phase("a")
        template = _make_template([phase])
        has_transitions = any(p.transitions for p in template.phases) or bool(
            template.default_transitions
        )
        assert has_transitions is False


# ---------------------------------------------------------------------------
# AC-19 — TestLoopExecution: phase with max_iterations > 0 can loop
# ---------------------------------------------------------------------------


class TestLoopExecution:
    """Phases with max_iterations > 0 may be revisited up to that many times."""

    def test_self_loop_with_max_iterations_runs_n_times(self) -> None:
        """A phase that loops back to itself runs exactly max_iterations times."""
        # 'work' loops to itself on success, 'done' is reached on failure
        phase_work = _make_phase(
            "work",
            max_iterations=3,
            transitions={"success": "work", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_work, phase_done])

        execution_count = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "work":
                execution_count[0] += 1
                # Succeed on first 2 runs (loop back), fail on the 3rd (go to done)
                if execution_count[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert execution_count[0] == 3
        assert "work" in result["phase_outputs"]
        assert "done" in result["phase_outputs"]
        assert not result.get("aborted")

    def test_two_phase_loop_a_to_b_to_a(self) -> None:
        """A→B→A loop: both phases run multiple times before chain terminates."""
        # 'a' loops to 'b' on success; 'b' loops back to 'a' on success.
        # After 2 visits to 'a', 'a' returns failure → no 'failed' transition → terminal.
        phase_a = _make_phase(
            "a",
            max_iterations=2,
            transitions={"success": "b"},
        )
        phase_b = _make_phase(
            "b",
            max_iterations=2,
            transitions={"success": "a"},
        )
        template = _make_template([phase_a, phase_b])

        a_count = [0]
        b_count = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "a":
                a_count[0] += 1
                if a_count[0] < 2:
                    return _success_result(task_spec)
                # 2nd visit: fail — no 'failed' transition → terminal
                return _failure_result(task_spec)
            b_count[0] += 1
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert a_count[0] == 2
        assert b_count[0] == 1
        assert result["iteration_counts"]["a"] == 2
        assert result["iteration_counts"]["b"] == 1


# ---------------------------------------------------------------------------
# AC-20 — TestIterationLimits: max_iterations enforcement
# ---------------------------------------------------------------------------


class TestIterationLimits:
    """Exceeding max_iterations returns MAX_ITERATIONS_EXCEEDED abort dict."""

    def test_exceeding_max_iterations_returns_abort(self) -> None:
        """When a loop phase would execute more times than max_iterations, abort."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=2,
            transitions={"success": "loop"},  # always loops back
        )
        template = _make_template([phase_loop])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("abort_reason") == "MAX_ITERATIONS_EXCEEDED"
        assert result.get("exceeded_phase") == "loop"

    def test_abort_includes_iteration_counts_and_history(self) -> None:
        """The MAX_ITERATIONS_EXCEEDED abort result exposes tracking data."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=2,
            transitions={"success": "loop"},
        )
        template = _make_template([phase_loop])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert "iteration_counts" in result
        assert "iteration_history" in result
        # 'loop' executed max_iterations times before the third attempt aborted
        assert result["iteration_counts"]["loop"] == 3  # incremented before abort check

    def test_max_iterations_1_allows_exactly_one_visit(self) -> None:
        """max_iterations=1 on a loop-transitioning phase runs it once then aborts."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=1,
            transitions={"success": "loop"},
        )
        template = _make_template([phase_loop])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("abort_reason") == "MAX_ITERATIONS_EXCEEDED"
        # Exactly 1 successful execution happened
        assert result["iteration_counts"]["loop"] == 2  # 1 executed + 1 attempted abort

    def test_max_iterations_respected_per_phase_independently(self) -> None:
        """Different phases may have different max_iterations limits."""
        phase_a = _make_phase(
            "a",
            max_iterations=3,
            transitions={"success": "b"},
        )
        phase_b = _make_phase(
            "b",
            max_iterations=1,
            transitions={"success": "a"},
        )
        template = _make_template([phase_a, phase_b])

        a_count = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "a":
                a_count[0] += 1
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # b has max_iterations=1; second visit to b triggers abort
        assert result.get("aborted") is True
        assert result.get("exceeded_phase") == "b"

    def test_pipeline_completes_normally_within_max_iterations(self) -> None:
        """A loop that exits before the limit completes without abort."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=5,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        call_count = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                call_count[0] += 1
                if call_count[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)  # exits the loop
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert not result.get("aborted")
        assert result.get("abort_reason") is None
        assert result["iteration_counts"]["loop"] == 3


# ---------------------------------------------------------------------------
# AC-21/AC-22 — TestIterationHistory: history accumulation and counts
# ---------------------------------------------------------------------------


class TestIterationHistory:
    """iteration_history accumulates prior results; iteration_counts tracks executions."""

    def test_single_phase_no_loop_has_empty_history(self) -> None:
        """A phase that runs only once has an empty iteration_history."""
        phase = _make_phase("only")
        template = _make_template([phase])
        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert "iteration_history" in result
        # 'only' ran once → no prior results to store
        assert result["iteration_history"].get("only", []) == []

    def test_loop_phase_history_accumulates_prior_results(self) -> None:
        """Each re-execution of a loop phase appends the old result to history."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=5,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # loop ran 3 times → 2 prior results in history
        assert len(result["iteration_history"]["loop"]) == 2
        # History entries are the results of iterations 1 and 2
        assert result["iteration_history"]["loop"][0]["metadata"]["iteration"] == 1
        assert result["iteration_history"]["loop"][1]["metadata"]["iteration"] == 2

    def test_iteration_counts_matches_actual_executions(self) -> None:
        """iteration_counts reflects the true number of times each phase ran."""
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase(
            "b",
            max_iterations=3,
            transitions={"success": "b", "failed": "c"},
        )
        phase_c = _make_phase("c")
        template = _make_template([phase_a, phase_b, phase_c])

        b_count = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "b":
                b_count[0] += 1
                if b_count[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert result["iteration_counts"]["a"] == 1
        assert result["iteration_counts"]["b"] == 3
        assert result["iteration_counts"]["c"] == 1

    def test_phase_outputs_holds_latest_result(self) -> None:
        """phase_outputs always contains the LATEST result for a loop phase."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=3,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # The latest result (3rd run) should be in phase_outputs
        latest = result["phase_outputs"]["loop"]
        assert latest["metadata"]["iteration"] == 3
        # History holds the first 2 runs
        assert len(result["iteration_history"]["loop"]) == 2

    def test_iteration_metadata_annotation_present(self) -> None:
        """Each result has metadata['iteration'] set to the run number."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=3,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # Check history entries
        for i, hist_entry in enumerate(result["iteration_history"]["loop"], start=1):
            assert hist_entry["metadata"]["iteration"] == i

        # Check final (latest) result
        assert result["phase_outputs"]["loop"]["metadata"]["iteration"] == 3


# ---------------------------------------------------------------------------
# AC-23 — TestIterationCallbacks: callbacks fire once per execution iteration
# ---------------------------------------------------------------------------


class TestIterationCallbacks:
    """on_phase_start and on_phase_complete fire once per execution, not per unique phase."""

    def test_callbacks_fire_for_each_loop_iteration(self) -> None:
        """A loop phase that runs 3 times triggers 3 start and 3 complete callbacks."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=5,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        start_calls: List[str] = []
        complete_calls: List[str] = []
        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(
            template,
            execute_fn,
            on_phase_start=lambda pid, phase, idx: start_calls.append(pid),
            on_phase_complete=lambda pid, result: complete_calls.append(pid),
        )
        seq.execute({})

        # loop ran 3 times, done ran once
        assert start_calls.count("loop") == 3
        assert complete_calls.count("loop") == 3
        assert start_calls.count("done") == 1
        assert complete_calls.count("done") == 1

    def test_step_index_increments_across_iterations(self) -> None:
        """The step_index (wave_index arg) passed to on_phase_start is always increasing."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=3,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        step_indices: List[int] = []
        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(
            template,
            execute_fn,
            on_phase_start=lambda pid, phase, idx: step_indices.append(idx),
        )
        seq.execute({})

        # Each iteration increments the step index: 0, 1, 2 for loop; 3 for done
        assert step_indices == [0, 1, 2, 3]

    def test_instance_iteration_counts_accessible_after_execute(self) -> None:
        """After execute(), self.iteration_counts and self.iteration_history are set."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=3,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        seq.execute({})

        # Instance vars are available for observability after execute()
        assert seq.iteration_counts["loop"] == 3
        assert seq.iteration_counts["done"] == 1
        assert len(seq.iteration_history["loop"]) == 2


# ---------------------------------------------------------------------------
# AC-25 — Result dict always includes iteration_history and iteration_counts
# ---------------------------------------------------------------------------


class TestResultIterationFields:
    """execute() result always includes iteration_history and iteration_counts."""

    def test_simple_chain_result_includes_iteration_fields(self) -> None:
        """Even a simple linear chain (no loops) includes the iteration fields."""
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b")
        template = _make_template([phase_a, phase_b])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert "iteration_history" in result
        assert "iteration_counts" in result
        assert result["iteration_counts"]["a"] == 1
        assert result["iteration_counts"]["b"] == 1
        assert result["iteration_history"].get("a", []) == []
        assert result["iteration_history"].get("b", []) == []

    def test_single_phase_result_includes_iteration_fields(self) -> None:
        """A single terminal phase still provides the iteration fields."""
        phase = _make_phase("only")
        template = _make_template([phase])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert "iteration_history" in result
        assert "iteration_counts" in result
        assert result["iteration_counts"]["only"] == 1

    def test_cycle_guard_result_includes_iteration_fields(self) -> None:
        """When the legacy cycle guard fires, the result still includes iteration fields."""
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b", transitions={"success": "a"})  # cycle
        template = _make_template([phase_a, phase_b])

        seq = _make_sequencer(template, _success_result)
        result = seq.execute({})

        assert "iteration_history" in result
        assert "iteration_counts" in result


# ---------------------------------------------------------------------------
# AC-26 — Non-loop phase (max_iterations==0) still uses legacy cycle guard
# ---------------------------------------------------------------------------


class TestNonLoopCycleGuard:
    """max_iterations==0 phases still trigger the legacy cycle guard on revisit."""

    def test_non_loop_phase_not_revisited(self, caplog) -> None:
        """A phase with max_iterations==0 that would be revisited triggers the guard."""
        phase_a = _make_phase("a", transitions={"success": "b"})
        phase_b = _make_phase("b", transitions={"success": "a"})  # cycle, no max_iterations
        template = _make_template([phase_a, phase_b])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            executed.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        with caplog.at_level(logging.WARNING, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, execute_fn)
            result = seq.execute({})

        assert executed.count("a") == 1
        assert executed.count("b") == 1
        assert any("cycle" in r.message.lower() for r in caplog.records)
        # Not an abort — just a clean stop
        assert not result.get("aborted")

    def test_loop_phase_not_constrained_by_cycle_guard(self) -> None:
        """A phase with max_iterations>0 bypasses the legacy cycle guard."""
        phase_loop = _make_phase(
            "loop",
            max_iterations=3,
            transitions={"success": "loop", "failed": "done"},
        )
        phase_done = _make_phase("done")
        template = _make_template([phase_loop, phase_done])

        run_index = [0]

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid == "loop":
                run_index[0] += 1
                if run_index[0] < 3:
                    return _success_result(task_spec)
                return _failure_result(task_spec)
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # Loop ran 3 times without triggering the cycle guard
        assert run_index[0] == 3
        assert not result.get("aborted")


class TestExhaustedRouting:
    """Issue #615: Tests for PhaseOutcome.EXHAUSTED routing in StateMachineSequencer."""

    def test_exhausted_routes_to_next_phase(self) -> None:
        """When a loop phase exceeds max_iterations and has an 'exhausted' transition,
        the sequencer routes to the named phase instead of aborting."""
        # Phase A: max_iterations=1, always succeeds (loops back to itself via success)
        # On 2nd visit, MAX_ITERATIONS_EXCEEDED fires → routes to phase B via 'exhausted'
        phase_a = _make_phase(
            "phase_a",
            max_iterations=1,
            transitions={"success": "phase_a", "exhausted": "phase_b"},
        )
        phase_b = _make_phase("phase_b")
        template = _make_template([phase_a, phase_b])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            executed.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # phase_a ran once (2nd visit triggers exhausted → routes to phase_b)
        assert executed.count("phase_a") == 1
        assert executed.count("phase_b") == 1
        # Final status is failed (aborted=True, abort_reason=EXHAUSTED_ROUTE)
        assert result.get("aborted") is True
        assert result.get("abort_reason") == "EXHAUSTED_ROUTE"

    def test_exhausted_fallback_to_failed_transition(self) -> None:
        """When a loop phase exceeds max_iterations and has no 'exhausted' transition
        but has a 'failed' transition, EXHAUSTED falls back to 'failed' route (backward compat)."""
        phase_a = _make_phase(
            "phase_a",
            max_iterations=1,
            transitions={"success": "phase_a", "failed": "error_phase"},
        )
        phase_error = _make_phase("error_phase")
        template = _make_template([phase_a, phase_error])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            executed.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # Without 'exhausted' key, _resolve_next_phase falls back to 'failed' → error_phase.
        # error_phase runs and pipeline completes via exhausted route.
        assert executed.count("phase_a") == 1
        assert executed.count("error_phase") == 1
        # Stamped as EXHAUSTED_ROUTE because we routed via exhausted fallback
        assert result.get("aborted") is True
        assert result.get("abort_reason") == "EXHAUSTED_ROUTE"

    def test_exhausted_no_transition_aborts(self) -> None:
        """When a loop phase exceeds max_iterations and has no 'exhausted' or 'failed'
        transition, the sequencer returns aborted=True, abort_reason=MAX_ITERATIONS_EXCEEDED."""
        phase_a = _make_phase(
            "phase_a",
            max_iterations=1,
            transitions={"success": "phase_a"},
        )
        template = _make_template([phase_a])

        executed: List[str] = []

        def execute_fn(task_spec, **kwargs):
            executed.append(task_spec.payload["phase_id"])
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        assert result.get("aborted") is True
        assert result.get("abort_reason") == "MAX_ITERATIONS_EXCEEDED"

    def test_exhausted_route_sets_failed_status_after_postmortem(self) -> None:
        """When routing via exhausted and the destination phase completes successfully,
        the pipeline final result still has aborted=True (run is recorded as failed)."""
        phase_loop = _make_phase(
            "loop_phase",
            max_iterations=1,
            transitions={"success": "loop_phase", "exhausted": "postmortem"},
        )
        phase_postmortem = _make_phase("postmortem", transitions={})
        template = _make_template([phase_loop, phase_postmortem])

        def execute_fn(task_spec, **kwargs):
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # Postmortem completed successfully, but run is stamped as failed
        assert result.get("aborted") is True
        assert result.get("abort_reason") == "EXHAUSTED_ROUTE"
        assert "postmortem" in result.get("phase_outputs", {})

    def test_exhausted_routes_via_exhausted_not_verdict_when_both_present(
        self,
    ) -> None:
        """Regression: EXHAUSTED outcome must not be hijacked by content-based routing.

        When a phase has both verdict keys (request_changes, approve) AND an
        exhausted transition — the exact production layout for spec_adversary —
        and the phase output contains "REQUEST_CHANGES" text, the sequencer
        must still route via `exhausted`, not via `request_changes`.
        """
        # Phase that loops back to itself on request_changes (like spec_adversary)
        # and routes to postmortem when exhausted.
        adversary_phase = _make_phase(
            "spec_adversary",
            max_iterations=1,
            transitions={
                "approve": "implement",
                "request_changes": "spec",
                "exhausted": "postmortem",
            },
        )
        spec_phase = _make_phase("spec", transitions={"success": "spec_adversary"})
        postmortem_phase = _make_phase("postmortem", transitions={})
        implement_phase = _make_phase("implement", transitions={})
        template = _make_template(
            [adversary_phase, spec_phase, postmortem_phase, implement_phase]
        )

        call_count: dict[str, int] = {}

        def execute_fn(task_spec, **kwargs) -> TaskResult:
            phase_id = task_spec.payload.get("phase_id", task_spec.id)
            call_count[phase_id] = call_count.get(phase_id, 0) + 1
            # spec_adversary outputs REQUEST_CHANGES text — mimics production
            if phase_id == "spec_adversary":
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "REQUEST_CHANGES\nNeeds more work."},
                )
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({"phase_id": "spec_adversary"})

        phase_outputs = result.get("phase_outputs", {})
        iteration_counts = result.get("iteration_counts", {})

        # spec_adversary must have been called twice (1st: request_changes → spec;
        # 2nd: exhausted → postmortem) — confirming content routing on 1st pass is fine
        # but EXHAUSTED on the 2nd pass must NOT be hijacked by the verdict.
        assert iteration_counts.get("spec_adversary") == 2, (
            f"Expected spec_adversary to run twice, got: {iteration_counts}"
        )

        # postmortem must have been reached via the exhausted transition
        assert "postmortem" in phase_outputs, (
            "Expected routing to postmortem via exhausted, "
            "but postmortem was not executed. "
            f"Phase outputs: {list(phase_outputs.keys())}"
        )

        # implement must NOT have been reached (that's the approve branch)
        assert "implement" not in phase_outputs, (
            "Sequencer incorrectly routed via approve instead of exhausted"
        )

        # spec must NOT have been called a second time (exhausted must not re-route
        # via request_changes on the 2nd spec_adversary iteration)
        assert iteration_counts.get("spec", 0) <= 1, (
            "Sequencer incorrectly routed via request_changes on exhausted iteration; "
            f"iteration_counts: {iteration_counts}"
        )

        # Run is still stamped as failed
        assert result.get("aborted") is True
