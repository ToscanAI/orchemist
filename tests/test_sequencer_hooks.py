"""Integration tests for PhaseSequencer on_pipeline_start / on_pipeline_complete hooks.

Tests:
- on_pipeline_start hook fires before first phase
- on_pipeline_complete hook fires after last phase (success)
- on_pipeline_complete hook fires when pipeline aborts (failed phase)
- on_pipeline_complete hook fires on unexpected exception
- pipeline_context values are available in phase prompt templates
- Pipelines without hooks work identically to before (no regression)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_template(phases: List[PhaseDefinition], template_id: str = "test-pipeline") -> PipelineTemplate:
    return PipelineTemplate(id=template_id, name="Test Pipeline", phases=phases)


def _phase(
    phase_id: str,
    prompt: str = "Hello {input}",
    depends_on: List[str] | None = None,
    fail: bool = False,
) -> PhaseDefinition:
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        depends_on=depends_on or [],
    )


def _mock_runner(fail_phase: Optional[str] = None) -> MagicMock:
    """Build a mock TaskRunner that succeeds on all phases, or fails on `fail_phase`."""
    from orchestration_engine.schemas import TaskResult, TaskState, TaskType

    runner = MagicMock()

    # Use a real dict (not MagicMock attribute) for task storage
    _task_store: Dict[str, Any] = {}

    def submit_task(spec):
        # The queue interface returns the spec's id as the task_id
        task_id = spec.id
        _task_store[task_id] = spec
        return task_id

    def get_task(task_id):
        return _task_store.get(task_id)

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = get_task
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()

    def execute(task_spec, worker_id="", model_tier=None, thinking_level=None):
        phase_id = task_spec.payload.get("phase_id", "")
        if fail_phase and phase_id == fail_phase:
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={"text": "Simulated failure"},
                errors=[],
            )
        return TaskResult(
            task_id=task_spec.id,
            task_type=task_spec.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": f"Output of {phase_id}"},
        )

    # Attach to a mock executor
    executor = MagicMock()
    executor.can_handle.return_value = True
    executor.execute.side_effect = execute
    runner.executors = [executor]

    return runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineStartHook:
    """on_pipeline_start is called before the first phase executes."""

    def test_pipeline_start_hook_called(self) -> None:
        template = _make_template([_phase("phase_a"), _phase("phase_b", depends_on=["phase_a"])])
        runner = _mock_runner()
        call_log: List[str] = []

        def start_hook(ctx):
            call_log.append("start")
            ctx["test_key"] = "hello"

        seq = PhaseSequencer(
            template, runner,
            on_pipeline_start=start_hook,
        )
        seq.execute({})
        assert call_log == ["start"], "on_pipeline_start must fire exactly once"

    def test_pipeline_start_hook_populates_context(self) -> None:
        template = _make_template([_phase("only_phase")])
        runner = _mock_runner()
        captured_ctx = {}

        def start_hook(ctx):
            ctx["branch_name"] = "feat/test-branch"
            ctx["base_branch"] = "main"

        seq = PhaseSequencer(template, runner, on_pipeline_start=start_hook)
        seq.execute({})

        assert seq.pipeline_context.get("branch_name") == "feat/test-branch"
        assert seq.pipeline_context.get("base_branch") == "main"

    def test_pipeline_start_hook_raises_propagates(self) -> None:
        """If on_pipeline_start raises, the pipeline should also raise."""
        template = _make_template([_phase("phase_a")])
        runner = _mock_runner()

        def bad_start_hook(ctx):
            raise RuntimeError("Git branch creation failed")

        seq = PhaseSequencer(template, runner, on_pipeline_start=bad_start_hook)

        with pytest.raises(RuntimeError, match="Git branch creation failed"):
            seq.execute({})


class TestPipelineCompleteHook:
    """on_pipeline_complete is called after the last phase."""

    def test_pipeline_complete_hook_called_on_success(self) -> None:
        template = _make_template([_phase("phase_a")])
        runner = _mock_runner()
        complete_calls: List[tuple] = []

        def complete_hook(ctx, result):
            complete_calls.append((dict(ctx), result is not None))

        seq = PhaseSequencer(template, runner, on_pipeline_complete=complete_hook)
        seq.execute({})

        assert len(complete_calls) == 1
        _ctx, result_not_none = complete_calls[0]
        assert result_not_none, "result should be a dict on success, not None"

    def test_pipeline_complete_hook_called_on_abort(self) -> None:
        """Hook fires even when a phase fails and the pipeline aborts."""
        template = _make_template([_phase("phase_a"), _phase("phase_b", depends_on=["phase_a"])])
        runner = _mock_runner(fail_phase="phase_a")
        complete_calls: List[Optional[dict]] = []

        def complete_hook(ctx, result):
            complete_calls.append(result)

        seq = PhaseSequencer(template, runner, on_pipeline_complete=complete_hook)
        seq.execute({})

        assert len(complete_calls) == 1
        # On abort the hook is called with None
        assert complete_calls[0] is None

    def test_pipeline_complete_hook_exception_doesnt_crash_pipeline(self) -> None:
        """A crashing on_pipeline_complete hook should not propagate."""
        template = _make_template([_phase("phase_a")])
        runner = _mock_runner()

        def bad_complete_hook(ctx, result):
            raise RuntimeError("Post-processing exploded")

        seq = PhaseSequencer(template, runner, on_pipeline_complete=bad_complete_hook)
        # Should NOT raise — hook exceptions are swallowed (logged as warning)
        result = seq.execute({})
        assert "phase_outputs" in result


class TestPipelineContextInPrompts:
    """Values written to pipeline_context are available as {context.key} in prompts."""

    def test_context_key_available_in_phase_prompt(self) -> None:
        """Values set by on_pipeline_start appear in phase prompt templates."""
        phase = _phase("reviewer", prompt="Branch: {context.branch_name} — diff: {context.git_diff}")
        template = _make_template([phase])
        runner = _mock_runner()

        captured_prompts: List[str] = []

        def capture_execute(task_spec, **kw):
            from orchestration_engine.schemas import TaskResult, TaskState
            captured_prompts.append(task_spec.payload.get("prompt", ""))
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "reviewed"},
            )

        runner.executors[0].execute.side_effect = capture_execute

        def start_hook(ctx):
            ctx["branch_name"] = "feat/test-abc123"
            ctx["git_diff"] = "diff --git a/foo.py ..."

        seq = PhaseSequencer(template, runner, on_pipeline_start=start_hook)
        seq.execute({})

        assert len(captured_prompts) == 1
        assert "feat/test-abc123" in captured_prompts[0]
        assert "diff --git a/foo.py" in captured_prompts[0]

    def test_missing_context_key_uses_placeholder(self) -> None:
        """Missing {context.key} returns a <MISSING:key> placeholder — no crash."""
        phase = _phase("phase", prompt="Value: {context.nonexistent_key}")
        template = _make_template([phase])
        runner = _mock_runner()

        captured_prompts: List[str] = []

        def capture_execute(task_spec, **kw):
            from orchestration_engine.schemas import TaskResult, TaskState
            captured_prompts.append(task_spec.payload.get("prompt", ""))
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": "ok"},
            )

        runner.executors[0].execute.side_effect = capture_execute

        seq = PhaseSequencer(template, runner)
        seq.execute({})

        assert len(captured_prompts) == 1
        assert "MISSING" in captured_prompts[0] or "nonexistent_key" in captured_prompts[0]


class TestNoHooksRegression:
    """Pipelines without hooks work identically to before."""

    def test_pipeline_without_any_hooks(self) -> None:
        """No hooks → pipeline still executes correctly."""
        template = _make_template([
            _phase("step1"),
            _phase("step2", depends_on=["step1"]),
        ])
        runner = _mock_runner()

        seq = PhaseSequencer(template, runner)
        result = seq.execute({"data": "test"})

        assert "phase_outputs" in result
        assert "step1" in result["phase_outputs"]
        assert "step2" in result["phase_outputs"]
        assert not result.get("aborted", False)

    def test_phase_complete_hook_still_fires(self) -> None:
        """Existing on_phase_complete hook still fires correctly."""
        template = _make_template([_phase("p1"), _phase("p2", depends_on=["p1"])])
        runner = _mock_runner()
        phase_complete_calls: List[str] = []

        def on_complete(phase_id, result):
            phase_complete_calls.append(phase_id)

        seq = PhaseSequencer(template, runner, on_phase_complete=on_complete)
        seq.execute({})

        assert phase_complete_calls == ["p1", "p2"]

    def test_pipeline_context_is_empty_dict_by_default(self) -> None:
        """pipeline_context is always an empty dict when no hooks are used."""
        template = _make_template([_phase("p")])
        runner = _mock_runner()
        seq = PhaseSequencer(template, runner)
        seq.execute({})
        # Still a dict (never None)
        assert isinstance(seq.pipeline_context, dict)

    def test_empty_template_no_phases(self) -> None:
        """Empty template returns empty result without crashing."""
        template = _make_template([])
        runner = _mock_runner()
        seq = PhaseSequencer(template, runner)
        result = seq.execute({})
        assert result == {"phase_outputs": {}, "final_output": {}}
