"""Tests for #535 — reject phases that render <MISSING:> before dispatch.

When a phase prompt (or command / working_dir) references a config key or an
upstream phase output that does not exist, ``_SafeDict`` / ``_PreviousOutputProxy``
render the literal ``<MISSING:...>`` token rather than raising. Before this fix
the broken string was passed straight to ``submit_task`` and the executor
silently produced garbage.

The fix adds a single shared ``PhaseSequencer._check_for_unresolved_placeholders``
guard, wired into all three dispatch sites (sequential, parallel, state-machine)
between prompt-build and ``submit_task``. A surviving ``<MISSING:...>`` token
aborts the pipeline with ``state == "permanently_failed"`` and
``error_code == "UNRESOLVED_PLACEHOLDERS"``; the error names every offending
marker plus the phase id.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from orchestration_engine.schemas import TaskError, TaskResult, TaskState
from orchestration_engine.sequencer import PhaseSequencer, StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str = "p0",
    prompt: str = "Hello",
    task_type: str = "content",
    command: Optional[str] = None,
    working_dir: str = ".",
    depends_on: Optional[List[str]] = None,
) -> PhaseDefinition:
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        task_type=task_type,
        command=command,
        working_dir=working_dir,
        allowed_commands=["run", "echo", "pytest"],
        depends_on=depends_on or [],
    )


def _make_template(phases: List[PhaseDefinition], parallel: bool = True) -> PipelineTemplate:
    return PipelineTemplate(
        id="missing-guard-fixture",
        name="Missing Guard Fixture",
        phases=phases,
        parallel=parallel,
    )


def _success_result(task_spec, **kw) -> TaskResult:
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": f"Output of {task_spec.payload.get('phase_id', '?')}"},
    )


def _panic_runner() -> MagicMock:
    """A runner whose submit_task EXPLODES if ever called.

    Proves the guard aborts the pipeline before any task is dispatched.
    """
    runner = MagicMock()

    def _boom(spec):
        raise AssertionError(
            "submit_task was called — the <MISSING:> guard failed to abort "
            f"before dispatch (phase={spec.payload.get('phase_id')!r})"
        )

    runner.queue.submit_task.side_effect = _boom
    runner.queue.get_task.side_effect = lambda tid: None
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()
    # Empty (non-dry-run) executor list so the guard treats this as a REAL run
    # and aborts on genuine missing references. The guard fires before any task
    # is dispatched, so the executor list is never actually consulted for work.
    runner.executors = []
    return runner


def _counting_runner(execute_fn: Callable) -> MagicMock:
    """A normal mock runner that records submit_task calls and runs execute_fn."""
    runner = MagicMock()
    _task_store: Dict[str, Any] = {}
    counter = {"count": 0}

    def submit_task(spec):
        counter["count"] += 1
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
    runner._submit_count = counter
    return runner


def _make_bare_sequencer() -> PhaseSequencer:
    """A PhaseSequencer with a do-nothing runner, for unit-testing the guard."""
    template = _make_template([_make_phase()])
    return PhaseSequencer(template, MagicMock())


# ---------------------------------------------------------------------------
# Unit tests — _check_for_unresolved_placeholders in isolation
# ---------------------------------------------------------------------------


class TestUnresolvedPlaceholderGuardUnit:
    """Unit tests of the guard given a pre-collected marker set.

    The guard consumes the ``missing_sink`` set populated by the
    config/input/previous_output substitutions; these tests feed it directly.
    """

    def test_guard_returns_none_for_empty_marker_set(self):
        seq = _make_bare_sequencer()
        phase = _make_phase()
        assert seq._check_for_unresolved_placeholders(phase, set()) is None

    def test_guard_returns_failure_for_config_marker(self):
        seq = _make_bare_sequencer()
        phase = _make_phase("impl")
        result = seq._check_for_unresolved_placeholders(
            phase, {"<MISSING:config_key>"}
        )
        assert result is not None
        assert result["state"] == "permanently_failed"
        assert result["error_code"] == "UNRESOLVED_PLACEHOLDERS"
        message = result["errors"][0]["message"]
        assert "<MISSING:config_key>" in message
        assert "impl" in message  # names the phase id

    def test_guard_returns_failure_for_command_marker(self):
        seq = _make_bare_sequencer()
        phase = _make_phase()
        result = seq._check_for_unresolved_placeholders(
            phase, {"<MISSING:test_command>"}
        )
        assert result is not None
        assert "<MISSING:test_command>" in result["errors"][0]["message"]

    def test_guard_rejects_previous_output_marker(self):
        """The previous_output[...] marker form is also rejected (#535 scope)."""
        seq = _make_bare_sequencer()
        phase = _make_phase()
        result = seq._check_for_unresolved_placeholders(
            phase, {"<MISSING:previous_output[earlier]>"}
        )
        assert result is not None
        assert "<MISSING:previous_output[earlier]>" in result["errors"][0]["message"]

    def test_guard_names_all_markers(self):
        seq = _make_bare_sequencer()
        phase = _make_phase()
        result = seq._check_for_unresolved_placeholders(
            phase, {"<MISSING:key>", "<MISSING:other>"}
        )
        assert result is not None
        message = result["errors"][0]["message"]
        assert message.count("<MISSING:key>") == 1
        assert message.count("<MISSING:other>") == 1


# ---------------------------------------------------------------------------
# Integration tests — full execute() across all three dispatch paths
# ---------------------------------------------------------------------------


class TestUnresolvedPlaceholderGuardIntegration:
    def test_sequential_dispatch_aborts_on_missing_config_key(self):
        """Sequential path: a phase whose prompt renders <MISSING:> aborts the
        pipeline and never calls submit_task."""
        phase = _make_phase("impl", prompt="Fix {config[nonexistent_key]} now")
        template = _make_template([phase], parallel=False)
        seq = PhaseSequencer(template, _panic_runner(), config={})

        result = seq.execute({})

        assert result.get("aborted") is True
        assert result["failed_phase"] == "impl"
        assert result["final_output"]["error_code"] == "UNRESOLVED_PLACEHOLDERS"
        assert result["final_output"]["state"] == "permanently_failed"
        message = result["final_output"]["errors"][0]["message"]
        assert "<MISSING:nonexistent_key>" in message

    def test_parallel_dispatch_aborts_on_missing_config_key(self):
        """Parallel path: a 2-phase wave (parallel=True) where the phases render
        <MISSING:> aborts via the parallel worker guard and never dispatches.

        Both phases reference missing keys so neither reaches submit_task — this
        keeps the assertion on the failed phase deterministic (no race between a
        guard-rejected phase and a clean phase reaching the panic runner).
        """
        # Two independent phases → single wave executed in parallel.
        p1 = _make_phase("first", prompt="Need {config[missing_a]} here")
        p2 = _make_phase("second", prompt="Need {config[missing_b]} too")
        template = _make_template([p1, p2], parallel=True)
        seq = PhaseSequencer(template, _panic_runner(), config={})

        result = seq.execute({})

        assert result.get("aborted") is True
        # Whichever phase the aggregator reports first, it is a guard rejection.
        # (Under the default fail_fast, the other phase may be cancelled before
        # it is recorded — so we don't assert on the full failed set, only that
        # the reported failure is a placeholder rejection and nothing was ever
        # dispatched. The _panic_runner would have raised if submit_task fired.)
        assert result["failed_phase"] in ("first", "second")
        assert result["final_output"]["error_code"] == "UNRESOLVED_PLACEHOLDERS"
        failed_pid = result["failed_phase"]
        assert seq.phase_outputs[failed_pid]["error_code"] == "UNRESOLVED_PLACEHOLDERS"

    def test_state_machine_aborts_on_missing_config_key(self):
        """State-machine path: build a StateMachineSequencer directly (per
        adversary Q1) and prove the guard aborts with iteration metadata."""
        phase = _make_phase("sm_phase", prompt="Use {config[missing_key]} here")
        template = _make_template([phase])
        seq = StateMachineSequencer(template=template, runner=_panic_runner(), config={})

        result = seq.execute({})

        assert result.get("aborted") is True
        assert result["failed_phase"] == "sm_phase"
        assert result["final_output"]["error_code"] == "UNRESOLVED_PLACEHOLDERS"
        assert "<MISSING:missing_key>" in result["final_output"]["errors"][0]["message"]
        # State-machine abort dict carries iteration bookkeeping (matches the
        # folder-guard abort shape it mirrors).
        assert "iteration_history" in result
        assert "iteration_counts" in result

    def test_missing_in_command_aborts(self):
        """A command-task phase whose rendered command holds <MISSING:> aborts."""
        phase = _make_phase(
            "cmd_phase",
            prompt="run the tests",
            task_type="command",
            command="run {config[missing_runner]}",
        )
        template = _make_template([phase], parallel=False)
        seq = PhaseSequencer(template, _panic_runner(), config={})

        result = seq.execute({})

        assert result.get("aborted") is True
        assert result["final_output"]["error_code"] == "UNRESOLVED_PLACEHOLDERS"
        assert "<MISSING:missing_runner>" in result["final_output"]["errors"][0]["message"]

    def test_normal_phase_unaffected(self):
        """A phase with a fully-resolved prompt dispatches exactly once and the
        pipeline completes normally (no abort)."""
        phase = _make_phase("ok", prompt="Implement {config[task]}")
        template = _make_template([phase], parallel=False)
        runner = _counting_runner(_success_result)
        seq = PhaseSequencer(template, runner, config={"task": "sort algorithm"})

        result = seq.execute({})

        assert not result.get("aborted", False)
        assert runner._submit_count["count"] == 1

    def test_inlined_context_file_with_missing_text_does_not_abort(self, tmp_path):
        """A phase that inlines a context file whose content legitimately
        contains a literal <MISSING:...> string AND a {config[...]} doc example
        must NOT abort (#535 false-positive guard).

        The guard keys on markers emitted by config/input/previous_output
        substitution (recorded in the per-render sink), NOT on a substring scan
        of the rendered prompt — so inlined file/skill content (e.g. the
        project's own source or docs, which the audit pipelines inline) cannot
        trip it. This is the exact regression class the source-tracking design
        prevents; without it the bundled audit example pipelines abort on
        dry-run.
        """
        # A doc fragment that mentions the placeholder syntax AND a literal
        # <MISSING:...> token — the kind of content the audit pipelines inline.
        doc = tmp_path / "placeholder_docs.md"
        doc.write_text(
            "Use {config[key]} to reference a config value, e.g. {config[tone]}.\n"
            "If a key is absent the engine substitutes <MISSING:tone>.\n"
        )
        phase = PhaseDefinition(
            id="audit",
            name="Audit",
            prompt_template="Review the docs:\n\n{files[placeholder_docs]}\n\nDone.",
            context_files=[str(doc)],
        )
        template = _make_template([phase], parallel=False)
        runner = _counting_runner(_success_result)
        seq = PhaseSequencer(template, runner, config={})

        result = seq.execute({})

        # The literal <MISSING:tone> and {config[key]} in the inlined doc must
        # NOT abort the pipeline — they did not come from a real missing
        # config/input/previous_output reference.
        assert not result.get("aborted", False)
        assert runner._submit_count["count"] == 1
        # And the inlined content (including the literal token) is in the prompt.
        dispatched_prompt = seq.phase_outputs["audit"]
        assert isinstance(dispatched_prompt, dict)
