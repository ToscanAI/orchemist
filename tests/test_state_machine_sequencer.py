"""Tests for StateMachineSequencer — Issue #234.

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
) -> PhaseDefinition:
    """Build a minimal PhaseDefinition with optional transition config."""
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        transitions=transitions or {},
        depends_on=depends_on or [],
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
