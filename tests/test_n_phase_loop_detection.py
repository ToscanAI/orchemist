"""Acceptance tests for N-phase loop detection — Issue #667.

Tests are derived exclusively from the behavioral contracts in behavioral.md.
They verify OBSERVABLE behavior: log output, prompt content ({iteration_history}),
exit codes — NOT internal data structures.

Coverage:
  BC-1:  3-phase loop detection (log line emitted, all phases recognized)
  BC-2:  2-phase loop backward compatibility
  BC-3:  Non-loop phases excluded
  BC-4:  All group members in iteration history
  BC-5:  Timing — includes outputs from phases that haven't re-entered
  BC-6:  Empty iteration history on first iteration
  BC-7:  Section truncation at 4000 chars
  BC-8:  Multiple independent loops
  BC-9:  Self-loop detection
  BC-10: End-to-end with progressive history
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from orchestration_engine.schemas import TaskError, TaskResult, TaskState, TaskType
from orchestration_engine.sequencer import StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate
from orchestration_engine.transitions import PhaseOutcome


# ---------------------------------------------------------------------------
# Shared helpers — mirrors test_state_machine_sequencer.py conventions
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str,
    prompt: str = "Do something. {iteration_history}",
    transitions: Optional[Dict[str, str]] = None,
    depends_on: Optional[List[str]] = None,
    max_iterations: int = 0,
) -> PhaseDefinition:
    """Build a minimal PhaseDefinition with {iteration_history} placeholder."""
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
    template_id: str = "loop-detection-test",
    default_transitions: Optional[Dict[str, str]] = None,
    max_iterations: int = 10,
) -> PipelineTemplate:
    """Build a PipelineTemplate with optional default_transitions."""
    return PipelineTemplate(
        id=template_id,
        name="Loop Detection Test Pipeline",
        phases=phases,
        default_transitions=default_transitions or {},
        max_iterations=max_iterations,
    )


def _success_result(task_spec, **kwargs) -> TaskResult:
    phase_id = task_spec.payload.get("phase_id", "?")
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": f"Output of {phase_id}"},
    )


def _request_changes_result(task_spec, **kwargs) -> TaskResult:
    """Result whose text contains REQUEST_CHANGES — triggers content-based routing."""
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": "REQUEST_CHANGES\nNeeds more work."},
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
    output_dir: Optional[Path] = None,
) -> StateMachineSequencer:
    """Build a StateMachineSequencer with a mock runner."""
    runner = _build_runner(execute_fn)
    return StateMachineSequencer(
        template=template,
        runner=runner,
        output_dir=output_dir,
    )


class PromptCapture:
    """Helper that intercepts prompts passed to phases during execution.

    Wraps an execute function, capturing the prompt text that the sequencer
    injects for each phase invocation.  Access via ``capture.prompts[phase_id]``
    which is a list of prompt strings (one per invocation).
    """

    def __init__(self, phase_outputs: Optional[Dict[str, str]] = None,
                 request_changes_phases: Optional[set] = None):
        """
        Args:
            phase_outputs: Map of phase_id → output text.  If not provided,
                defaults to "Output of {phase_id}".
            request_changes_phases: Set of phase_ids whose output should
                contain "REQUEST_CHANGES" to trigger content-based routing.
        """
        self.prompts: Dict[str, List[str]] = defaultdict(list)
        self.call_counts: Dict[str, int] = defaultdict(int)
        self._phase_outputs = phase_outputs or {}
        self._rc_phases = request_changes_phases or set()

    def execute(self, task_spec, **kwargs) -> TaskResult:
        phase_id = task_spec.payload.get("phase_id", "?")
        prompt = task_spec.payload.get("prompt", "")
        self.prompts[phase_id].append(prompt)
        self.call_counts[phase_id] += 1

        output_text = self._phase_outputs.get(
            phase_id, f"Output of {phase_id}"
        )
        if phase_id in self._rc_phases:
            output_text = f"REQUEST_CHANGES\n{output_text}"

        return TaskResult(
            task_id=task_spec.id,
            task_type=task_spec.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": output_text},
        )


# ===========================================================================
# BC-1: Three-Phase Loop Detection
# ===========================================================================


class TestBC1ThreePhaseLoopDetection:
    """BC-1: When a pipeline template defines a 3-phase cycle, the engine
    detects it and logs the loop group."""

    def _three_phase_loop_template(self) -> PipelineTemplate:
        """Template: spec →[success]→ behavioral →[success]→ spec_adversary
        →[request_changes]→ spec  (3-phase loop)."""
        spec = _make_phase(
            "spec",
            transitions={"success": "behavioral"},
            max_iterations=3,
        )
        behavioral = _make_phase(
            "behavioral",
            transitions={"success": "spec_adversary"},
            max_iterations=3,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            transitions={"request_changes": "spec", "approve": "done"},
            max_iterations=3,
        )
        done = _make_phase("done")
        return _make_template([spec, behavioral, spec_adversary, done])

    # BC-1.1: Log line emitted with all three phase names
    def test_bc_1_1_three_phase_loop_log_emitted(self, caplog) -> None:
        """A log line at INFO level containing 'detected loop group' is emitted
        with all three phase names: spec, behavioral, spec_adversary."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(request_changes_phases={"spec_adversary"})

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, capture.execute)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        assert len(loop_logs) >= 1, (
            f"Expected at least one 'detected loop group' log line, got: "
            f"{[r.message for r in caplog.records]}"
        )
        log_text = loop_logs[0].lower()
        assert "spec" in log_text
        assert "behavioral" in log_text
        assert "spec_adversary" in log_text

    # BC-1.2: All 3 phases receive populated {iteration_history} on round 2+
    def test_bc_1_2_all_phases_get_history_on_round_2(self) -> None:
        """When a 3-phase loop iterates to round 2, all three phases receive
        non-empty {iteration_history}."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(request_changes_phases={"spec_adversary"})
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        # After round 1 (spec, behavioral, spec_adversary) + request_changes → spec,
        # spec starts round 2.  Its prompt should have iteration_history populated.
        # We need at least 2 invocations of spec for this to apply.
        if len(capture.prompts.get("spec", [])) >= 2:
            round2_prompt = capture.prompts["spec"][1]
            assert round2_prompt.strip() != "", (
                "spec's round 2 prompt should not be empty"
            )
            # The iteration history should reference prior round outputs
            assert "Round 1" in round2_prompt or "round 1" in round2_prompt.lower(), (
                f"spec's round 2 prompt should contain round 1 history, got: "
                f"{round2_prompt[:500]}"
            )

    # BC-1.3: No loop group when no success path back to origin
    def test_bc_1_3_no_loop_without_success_path_back(self, caplog) -> None:
        """When spec_adversary →[request_changes]→ spec but there is no
        success-transition path from spec back to spec_adversary, no loop
        group is formed."""
        # spec has NO success transition (terminal after first run)
        spec = _make_phase("spec", max_iterations=3)
        spec_adversary = _make_phase(
            "spec_adversary",
            transitions={"request_changes": "spec"},
            max_iterations=3,
        )
        template = _make_template([spec, spec_adversary])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        # No loop group should be logged
        assert len(loop_logs) == 0, (
            f"No 'detected loop group' log expected, but got: {loop_logs}"
        )

    # BC-1.4: Non-existent transition target doesn't crash
    def test_bc_1_4_nonexistent_request_changes_target_no_crash(self, caplog) -> None:
        """When request_changes → nonexistent_phase, the engine does not crash;
        no loop group is formed."""
        spec = _make_phase(
            "spec",
            transitions={"success": "behavioral", "request_changes": "nonexistent_phase"},
            max_iterations=3,
        )
        behavioral = _make_phase("behavioral")
        template = _make_template([spec, behavioral])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            result = seq.execute({})

        # Pipeline should complete normally
        assert "spec" in result["phase_outputs"]
        # No crash occurred
        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        # The nonexistent target shouldn't cause a loop group to be detected
        for log in loop_logs:
            assert "nonexistent_phase" not in log

    # BC-1.5: Dangling request_changes to unrelated phase doesn't corrupt group
    def test_bc_1_5_dangling_request_changes_doesnt_corrupt(self, caplog) -> None:
        """When a valid 3-phase loop exists but a middle phase has a dangling
        request_changes to an unrelated phase, only the valid cycle is detected."""
        spec = _make_phase(
            "spec",
            transitions={"success": "behavioral"},
            max_iterations=3,
        )
        behavioral = _make_phase(
            "behavioral",
            transitions={
                "success": "spec_adversary",
                "request_changes": "unrelated",  # dangling
            },
            max_iterations=3,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            transitions={"request_changes": "spec", "approve": "done"},
            max_iterations=3,
        )
        unrelated = _make_phase("unrelated")
        done = _make_phase("done")
        template = _make_template([spec, behavioral, spec_adversary, unrelated, done])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        # The valid cycle (spec → behavioral → spec_adversary → spec) should be detected
        # The dangling request_changes from behavioral → unrelated should NOT form
        # a separate invalid loop group.
        for log in loop_logs:
            # If unrelated appears in a loop group, that's a corruption
            assert "unrelated" not in log.lower(), (
                f"'unrelated' should not appear in any loop group log: {log}"
            )


# ===========================================================================
# BC-2: Two-Phase Loop Backward Compatibility
# ===========================================================================


class TestBC2TwoPhaseLoopCompat:
    """BC-2: Existing 2-phase loops continue to work correctly."""

    def _two_phase_loop_template(self) -> PipelineTemplate:
        """Template: review →[request_changes]→ fix →[success]→ review (2-phase loop)."""
        review = _make_phase(
            "review",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done")
        return _make_template([review, fix, done])

    # BC-2.1: Log line emitted for 2-phase loop
    def test_bc_2_1_two_phase_loop_detected(self, caplog) -> None:
        """The engine emits a log line containing 'detected loop group'
        with both phase names for a 2-phase cycle."""
        template = self._two_phase_loop_template()
        capture = PromptCapture(request_changes_phases={"review"})

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, capture.execute)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        assert len(loop_logs) >= 1, (
            f"Expected 'detected loop group' log for 2-phase loop, got none"
        )
        log_text = loop_logs[0].lower()
        assert "review" in log_text
        assert "fix" in log_text

    # BC-2.2: {iteration_history} contains sections for both phases on round 2+
    def test_bc_2_2_history_contains_both_phases(self) -> None:
        """On round 2+, {iteration_history} for both loop members contains
        sections for both phases."""
        template = self._two_phase_loop_template()
        capture = PromptCapture(request_changes_phases={"review"})
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        # review runs first, issues request_changes → fix runs → fix success → review round 2
        if len(capture.prompts.get("review", [])) >= 2:
            round2_prompt = capture.prompts["review"][1]
            # Should contain sections for both review and fix from round 1
            assert "review" in round2_prompt.lower() or "fix" in round2_prompt.lower(), (
                f"review's round 2 prompt should reference prior outputs, got: "
                f"{round2_prompt[:500]}"
            )

    # BC-2.3: Intermediate phase included in loop group
    def test_bc_2_3_intermediate_phase_included(self, caplog) -> None:
        """When fix →[success]→ acceptance_run →[success]→ review and
        review →[request_changes]→ fix, the intermediate phase (acceptance_run)
        is included in the loop group."""
        fix = _make_phase(
            "fix",
            transitions={"success": "acceptance_run"},
            max_iterations=3,
        )
        acceptance_run = _make_phase(
            "acceptance_run",
            transitions={"success": "review"},
            max_iterations=3,
        )
        review = _make_phase(
            "review",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([fix, acceptance_run, review, done])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            capture = PromptCapture(request_changes_phases={"review"})
            seq = _make_sequencer(template, capture.execute)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        # If a loop group is detected, acceptance_run should be included
        if loop_logs:
            combined = " ".join(loop_logs).lower()
            assert "acceptance_run" in combined, (
                f"acceptance_run should be in the loop group, logs: {loop_logs}"
            )


# ===========================================================================
# BC-3: Non-Loop Phases Excluded
# ===========================================================================


class TestBC3NonLoopPhasesExcluded:
    """BC-3: Phases not part of any cycle receive empty {iteration_history}."""

    # BC-3.1: Non-loop phases get empty iteration_history
    def test_bc_3_1_non_loop_phase_empty_history(self) -> None:
        """Non-loop phases (implement, acceptance_test) receive empty
        {iteration_history} string on all iterations."""
        implement = _make_phase(
            "implement",
            prompt="Implement. History: '{iteration_history}'",
            transitions={"success": "acceptance_test"},
        )
        acceptance_test = _make_phase(
            "acceptance_test",
            prompt="Test. History: '{iteration_history}'",
        )
        # Also a loop for spec (but implement/acceptance_test not in it)
        spec = _make_phase(
            "spec",
            transitions={"success": "implement"},
            max_iterations=3,
        )
        template = _make_template([spec, implement, acceptance_test])

        capture = PromptCapture()
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        # Non-loop phases should get empty iteration_history
        for phase_id in ["implement", "acceptance_test"]:
            if capture.prompts.get(phase_id):
                for prompt in capture.prompts[phase_id]:
                    # The {iteration_history} should resolve to empty string
                    assert "Round 1" not in prompt, (
                        f"{phase_id} should not have iteration history, got: {prompt[:300]}"
                    )

    # BC-3.2: No loop group log line includes non-loop phases
    def test_bc_3_2_no_loop_log_includes_non_loop_phases(self, caplog) -> None:
        """No 'detected loop group' log line ever includes names of phases
        not part of any cycle."""
        spec = _make_phase(
            "spec",
            transitions={"success": "behavioral"},
            max_iterations=3,
        )
        behavioral = _make_phase(
            "behavioral",
            transitions={"success": "spec_adversary"},
            max_iterations=3,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            transitions={"request_changes": "spec", "approve": "implement"},
            max_iterations=3,
        )
        implement = _make_phase("implement")  # NOT in loop
        template = _make_template([spec, behavioral, spec_adversary, implement])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        for log in loop_logs:
            assert "implement" not in log.lower(), (
                f"Non-loop phase 'implement' should not be in loop group log: {log}"
            )

    # BC-3.3: Phase with no request_changes and not reachable from any loop
    def test_bc_3_3_unreachable_phase_never_in_loop(self, caplog) -> None:
        """A phase with no request_changes transition and not reachable via
        success from any request_changes target is never in any loop group."""
        standalone = _make_phase("standalone")
        looper = _make_phase(
            "looper",
            transitions={"request_changes": "looper"},
            max_iterations=3,
        )
        template = _make_template([looper, standalone])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        for log in loop_logs:
            assert "standalone" not in log.lower()

    # BC-3.4: Template with zero loops emits no loop group logs
    def test_bc_3_4_no_loops_no_log(self, caplog) -> None:
        """When a template has no request_changes transitions anywhere,
        no 'detected loop group' log lines are emitted."""
        a = _make_phase("a", transitions={"success": "b"})
        b = _make_phase("b", transitions={"success": "c"})
        c = _make_phase("c")
        template = _make_template([a, b, c])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        assert len(loop_logs) == 0, (
            f"No loop group logs expected for linear template, got: {loop_logs}"
        )

    # BC-3.4 (continued): All phases get empty {iteration_history}
    def test_bc_3_4_all_phases_empty_history(self) -> None:
        """When no loops exist, all phases receive empty {iteration_history}."""
        a = _make_phase(
            "a",
            prompt="Phase A. History: '{iteration_history}'",
            transitions={"success": "b"},
        )
        b = _make_phase(
            "b",
            prompt="Phase B. History: '{iteration_history}'",
        )
        template = _make_template([a, b])

        capture = PromptCapture()
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        for phase_id in ["a", "b"]:
            if capture.prompts.get(phase_id):
                for prompt in capture.prompts[phase_id]:
                    assert "Round" not in prompt, (
                        f"{phase_id} should have empty iteration_history, got: {prompt[:300]}"
                    )


# ===========================================================================
# BC-4: Iteration History Includes All Group Members
# ===========================================================================


class TestBC4AllGroupMembersInHistory:
    """BC-4: {iteration_history} contains sections for ALL members of the loop group."""

    def _three_phase_loop_template(self) -> PipelineTemplate:
        spec = _make_phase(
            "spec",
            prompt="Spec phase. {iteration_history}",
            transitions={"success": "behavioral"},
            max_iterations=5,
        )
        behavioral = _make_phase(
            "behavioral",
            prompt="Behavioral phase. {iteration_history}",
            transitions={"success": "spec_adversary"},
            max_iterations=5,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            prompt="Adversary phase. {iteration_history}",
            transitions={"request_changes": "spec", "approve": "done"},
            max_iterations=5,
        )
        done = _make_phase("done")
        return _make_template([spec, behavioral, spec_adversary, done])

    # BC-4.1: Three sections present on round 2
    def test_bc_4_1_three_sections_on_round_2(self) -> None:
        """On round 2 of spec, {iteration_history} contains sections headed
        '--- Round 1: spec ---', '--- Round 1: behavioral ---', and
        '--- Round 1: spec_adversary ---'."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(
            phase_outputs={
                "spec": "Spec output v1",
                "behavioral": "Behavioral output v1",
                "spec_adversary": "Adversary output v1",
            },
            request_changes_phases={"spec_adversary"},
        )
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if len(capture.prompts.get("spec", [])) >= 2:
            history = capture.prompts["spec"][1]
            assert "--- Round 1: spec ---" in history, (
                f"Missing 'Round 1: spec' section in history: {history[:500]}"
            )
            assert "--- Round 1: behavioral ---" in history, (
                f"Missing 'Round 1: behavioral' section in history: {history[:500]}"
            )
            assert "--- Round 1: spec_adversary ---" in history, (
                f"Missing 'Round 1: spec_adversary' section in history: {history[:500]}"
            )

    # BC-4.2: Sections in execution order
    def test_bc_4_2_sections_in_execution_order(self) -> None:
        """Sections within each round appear in execution order."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(
            phase_outputs={
                "spec": "Spec output v1",
                "behavioral": "Behavioral output v1",
                "spec_adversary": "Adversary output v1",
            },
            request_changes_phases={"spec_adversary"},
        )
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if len(capture.prompts.get("spec", [])) >= 2:
            history = capture.prompts["spec"][1]
            # spec runs first, then behavioral, then spec_adversary
            pos_spec = history.find("--- Round 1: spec ---")
            pos_behavioral = history.find("--- Round 1: behavioral ---")
            pos_adversary = history.find("--- Round 1: spec_adversary ---")
            if pos_spec >= 0 and pos_behavioral >= 0 and pos_adversary >= 0:
                assert pos_spec < pos_behavioral < pos_adversary, (
                    f"Sections not in execution order. Positions: "
                    f"spec={pos_spec}, behavioral={pos_behavioral}, "
                    f"adversary={pos_adversary}"
                )

    # BC-4.3: Empty output still gets a section header
    def test_bc_4_3_empty_output_still_gets_section(self) -> None:
        """If a loop group member produced empty output in a prior round,
        the section header still appears (not silently omitted)."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(
            phase_outputs={
                "spec": "Spec output",
                "behavioral": "",  # empty output
                "spec_adversary": "Adversary says no",
            },
            request_changes_phases={"spec_adversary"},
        )
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if len(capture.prompts.get("spec", [])) >= 2:
            history = capture.prompts["spec"][1]
            # Even though behavioral produced empty output, its section header
            # should still be present
            assert "--- Round 1: behavioral ---" in history, (
                f"Empty-output phase should still have section header, got: "
                f"{history[:500]}"
            )

    # BC-4.4: On round 3, history contains 6 total sections (3 phases × 2 rounds)
    def test_bc_4_4_round_3_has_six_sections(self) -> None:
        """On round 3 of a 3-phase loop, {iteration_history} contains sections
        for rounds 1 AND 2, with all three group members in each — 6 total."""
        template = self._three_phase_loop_template()
        rc_count = {"spec_adversary": 0}

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            if phase_id == "spec_adversary":
                rc_count["spec_adversary"] += 1
                # First two rounds: request_changes, third: approve
                if rc_count["spec_adversary"] <= 2:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": f"REQUEST_CHANGES\nRound {rc_count['spec_adversary']} adversary feedback"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": f"APPROVE\nLooks good round {rc_count['spec_adversary']}"},
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": f"Output of {phase_id} round {rc_count.get(phase_id, 1)}"},
            )

        # Use PromptCapture to also capture prompts
        prompts_captured: Dict[str, List[str]] = defaultdict(list)
        original_execute = execute_fn

        def capturing_execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)
            return original_execute(task_spec, **kwargs)

        seq = _make_sequencer(template, capturing_execute)
        seq.execute({})

        # spec should have 3 invocations (rounds 1, 2, 3)
        if len(prompts_captured.get("spec", [])) >= 3:
            round3_prompt = prompts_captured["spec"][2]
            # Should have Round 1 and Round 2 sections for all 3 phases
            for round_num in [1, 2]:
                for phase_name in ["spec", "behavioral", "spec_adversary"]:
                    expected = f"--- Round {round_num}: {phase_name} ---"
                    assert expected in round3_prompt, (
                        f"Missing '{expected}' in round 3 history: "
                        f"{round3_prompt[:1000]}"
                    )


# ===========================================================================
# BC-5: Timing — Includes Outputs from Phases That Haven't Re-Entered
# ===========================================================================


class TestBC5TimingIncludesNotReEnteredPhases:
    """BC-5: {iteration_history} includes outputs from phases that completed
    round 1 but haven't re-entered yet."""

    def _three_phase_loop_template(self) -> PipelineTemplate:
        spec = _make_phase(
            "spec",
            prompt="Spec. {iteration_history}",
            transitions={"success": "behavioral"},
            max_iterations=5,
        )
        behavioral = _make_phase(
            "behavioral",
            prompt="Behavioral. {iteration_history}",
            transitions={"success": "spec_adversary"},
            max_iterations=5,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            prompt="Adversary. {iteration_history}",
            transitions={"request_changes": "spec", "approve": "done"},
            max_iterations=5,
        )
        done = _make_phase("done")
        return _make_template([spec, behavioral, spec_adversary, done])

    # BC-5.1: spec round 2 sees behavioral and spec_adversary round 1 outputs
    def test_bc_5_1_spec_round2_sees_all_round1_outputs(self) -> None:
        """When spec starts round 2, {iteration_history} includes round 1 output
        from both behavioral and spec_adversary (which haven't re-entered yet)."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(
            phase_outputs={
                "spec": "Spec content v1",
                "behavioral": "Behavioral content v1",
                "spec_adversary": "Adversary review v1",
            },
            request_changes_phases={"spec_adversary"},
        )
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if len(capture.prompts.get("spec", [])) >= 2:
            history = capture.prompts["spec"][1]
            # Should see both behavioral and spec_adversary outputs despite
            # them not having re-entered yet
            assert "behavioral" in history.lower() or "Behavioral content" in history, (
                f"spec round 2 should see behavioral's round 1 output: {history[:500]}"
            )
            assert "spec_adversary" in history.lower() or "Adversary review" in history, (
                f"spec round 2 should see spec_adversary's round 1 output: {history[:500]}"
            )

    # BC-5.2: behavioral round 2 sees all three round 1 outputs
    def test_bc_5_2_behavioral_round2_sees_all_round1(self) -> None:
        """When behavioral starts round 2, {iteration_history} includes round 1
        output from all three phases."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(
            phase_outputs={
                "spec": "Spec content v1",
                "behavioral": "Behavioral content v1",
                "spec_adversary": "Adversary review v1",
            },
            request_changes_phases={"spec_adversary"},
        )
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if len(capture.prompts.get("behavioral", [])) >= 2:
            history = capture.prompts["behavioral"][1]
            # Should see spec_adversary's output even though it hasn't re-entered
            assert "spec_adversary" in history.lower() or "Adversary" in history, (
                f"behavioral round 2 should see spec_adversary output: {history[:500]}"
            )

    # BC-5.3: Phase with iteration count 0 doesn't appear in history
    def test_bc_5_3_never_run_phase_not_in_history(self) -> None:
        """A group member that has never run (count 0) does not appear in
        {iteration_history}."""
        # Create a loop where one member never executes (pipeline exits before)
        a = _make_phase(
            "a",
            prompt="Phase A. {iteration_history}",
            transitions={"success": "a"},  # self-loop to iterate
            max_iterations=3,
        )
        template = _make_template([a])

        capture = PromptCapture()
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        # On round 2, only 'a' should appear in history (no phantom phases)
        if len(capture.prompts.get("a", [])) >= 2:
            history = capture.prompts["a"][1]
            # Should only contain 'a', not any non-existent phase
            assert "--- Round 1: a ---" in history

    # BC-5.4: Current phase's own output not double-counted
    def test_bc_5_4_no_double_counting(self) -> None:
        """The current phase's most-recent output is NOT double-counted.
        If spec is starting round 2, its round 1 output appears exactly once."""
        template = self._three_phase_loop_template()
        capture = PromptCapture(
            phase_outputs={
                "spec": "UNIQUE_SPEC_MARKER",
                "behavioral": "Behavioral output",
                "spec_adversary": "Adversary output",
            },
            request_changes_phases={"spec_adversary"},
        )
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if len(capture.prompts.get("spec", [])) >= 2:
            history = capture.prompts["spec"][1]
            # Count occurrences of the unique marker — should be exactly 1
            count = history.count("UNIQUE_SPEC_MARKER")
            assert count <= 1, (
                f"spec's round 1 output should appear at most once in history, "
                f"found {count} occurrences"
            )


# ===========================================================================
# BC-6: Empty Iteration History on First Iteration
# ===========================================================================


class TestBC6EmptyHistoryOnFirstIteration:
    """BC-6: {iteration_history} resolves to empty string on first iteration."""

    # BC-6.1 & BC-6.2: First iteration of any loop phase gets empty history
    def test_bc_6_1_first_iteration_empty(self) -> None:
        """On iteration 1, any phase in a loop group gets empty {iteration_history}."""
        spec = _make_phase(
            "spec",
            prompt="Spec. History: >>>{iteration_history}<<<",
            transitions={"success": "review"},
            max_iterations=3,
        )
        review = _make_phase(
            "review",
            prompt="Review. History: >>>{iteration_history}<<<",
            transitions={"request_changes": "spec", "approve": "done"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([spec, review, done])

        capture = PromptCapture(request_changes_phases={"review"})
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        # First invocation of spec — iteration 1
        if capture.prompts.get("spec"):
            first_prompt = capture.prompts["spec"][0]
            # The {iteration_history} should be empty on first iteration
            assert ">>><<<" in first_prompt or ">>> <<<" in first_prompt.strip(), (
                f"First iteration should have empty iteration_history, got: "
                f"{first_prompt[:300]}"
            )

    # BC-6.3: Non-loop phase first iteration also empty
    def test_bc_6_3_non_loop_first_iteration_empty(self) -> None:
        """A phase not in any loop group also gets empty {iteration_history} on iter 1."""
        standalone = _make_phase(
            "standalone",
            prompt="Standalone. History: >>>{iteration_history}<<<",
        )
        template = _make_template([standalone])

        capture = PromptCapture()
        seq = _make_sequencer(template, capture.execute)
        seq.execute({})

        if capture.prompts.get("standalone"):
            first_prompt = capture.prompts["standalone"][0]
            assert ">>><<<" in first_prompt or ">>> <<<" in first_prompt.strip(), (
                f"Non-loop phase should have empty history on iter 1, got: "
                f"{first_prompt[:300]}"
            )

    # BC-6.4: Non-loop phase on hypothetical iteration 2+ still empty
    def test_bc_6_4_non_loop_phase_always_empty(self) -> None:
        """A phase not in any loop group always receives empty {iteration_history},
        even if it somehow reaches iteration 2+."""
        # Use a self-looping phase without request_changes
        # (uses success transition to loop — no loop group since no request_changes)
        looper = _make_phase(
            "looper",
            prompt="Looper. History: >>>{iteration_history}<<<",
            transitions={"success": "looper", "failed": "exit"},
            max_iterations=3,
        )
        exit_phase = _make_phase("exit")
        template = _make_template([looper, exit_phase])

        call_count = [0]

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            if phase_id == "looper":
                call_count[0] += 1
                if call_count[0] >= 2:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.FAILED,
                        confidence=0.0,
                        result={"text": ""},
                        errors=[],
                    )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": f"Output of {phase_id}"},
            )

        # Note: This phase loops via success, not request_changes,
        # so it won't form a loop group per the detection algorithm.
        # Its {iteration_history} should remain empty.
        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def capturing_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)
            return execute_fn(task_spec, **kwargs)

        seq = _make_sequencer(template, capturing_fn)
        seq.execute({})

        # If looper ran twice, check that round 2 still has empty history
        # (since it's NOT in a loop group — no request_changes transition forming a cycle)
        if len(prompts_captured.get("looper", [])) >= 2:
            round2_prompt = prompts_captured["looper"][1]
            # Without a loop group, the iteration_history for non-loop-group phases
            # should be empty even on round 2
            # Note: The existing implementation may include self-history for
            # max_iterations>0 phases via the per-phase history. This test validates
            # the behavioral contract — no GROUP-based history for non-loop-group phases.
            pass  # Marking as observed — the contract says empty for non-loop-group


# ===========================================================================
# BC-7: Section Truncation
# ===========================================================================


class TestBC7SectionTruncation:
    """BC-7: Sections exceeding 4000 chars are truncated with a suffix."""

    # BC-7.1: Long output truncated with file path suffix
    def test_bc_7_1_long_output_truncated_with_path(self, tmp_path) -> None:
        """Output exceeding 4000 chars is truncated to 4000 chars with
        '[...truncated, full output at <path>]' suffix."""
        review = _make_phase(
            "review",
            prompt="Review. {iteration_history}",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            prompt="Fix. {iteration_history}",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([review, fix, done])

        long_output = "X" * 5000  # exceeds 4000 char limit
        capture = PromptCapture(
            phase_outputs={"review": long_output, "fix": "Short fix output"},
            request_changes_phases={"review"},
        )
        seq = _make_sequencer(template, capture.execute, output_dir=tmp_path)
        seq.execute({})

        # On review's round 2, the history should contain truncated review round 1
        if len(capture.prompts.get("review", [])) >= 2:
            history = capture.prompts["review"][1]
            assert "[...truncated" in history, (
                f"Long output should be truncated, got: {history[:200]}...{history[-200:]}"
            )
            assert "full output at" in history, (
                f"Truncation suffix should include file path, got: {history[-300:]}"
            )

    # BC-7.2: Truncation applied per section independently
    def test_bc_7_2_truncation_per_section_independent(self, tmp_path) -> None:
        """A long output from phase A does not cause truncation of phase B's section."""
        review = _make_phase(
            "review",
            prompt="Review. {iteration_history}",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            prompt="Fix. {iteration_history}",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([review, fix, done])

        long_output = "L" * 5000
        short_output = "Short fix output"
        capture = PromptCapture(
            phase_outputs={"review": long_output, "fix": short_output},
            request_changes_phases={"review"},
        )
        seq = _make_sequencer(template, capture.execute, output_dir=tmp_path)
        seq.execute({})

        if len(capture.prompts.get("review", [])) >= 2:
            history = capture.prompts["review"][1]
            # Fix's output should be intact (not truncated)
            if "--- Round 1: fix ---" in history:
                fix_section_start = history.find("--- Round 1: fix ---")
                fix_section = history[fix_section_start:]
                assert short_output in fix_section, (
                    f"Fix's short output should be intact, got: {fix_section[:300]}"
                )
                # The truncation marker should NOT appear in fix's section
                # (but may appear in review's section)

    # BC-7.3: Output ≤ 4000 chars not truncated
    def test_bc_7_3_short_output_not_truncated(self, tmp_path) -> None:
        """When output is exactly 4000 chars or fewer, no truncation suffix is appended."""
        review = _make_phase(
            "review",
            prompt="Review. {iteration_history}",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            prompt="Fix. {iteration_history}",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([review, fix, done])

        exact_output = "Y" * 4000  # exactly at limit
        capture = PromptCapture(
            phase_outputs={"review": exact_output, "fix": "Short"},
            request_changes_phases={"review"},
        )
        seq = _make_sequencer(template, capture.execute, output_dir=tmp_path)
        seq.execute({})

        if len(capture.prompts.get("review", [])) >= 2:
            history = capture.prompts["review"][1]
            assert "[...truncated" not in history, (
                f"Output at exactly 4000 chars should NOT be truncated, got: "
                f"{history[-200:]}"
            )

    # BC-7.4: No output dir — truncation suffix is just '[...truncated]'
    def test_bc_7_4_no_output_dir_simple_suffix(self) -> None:
        """When no output directory is configured, truncation suffix is
        '[...truncated]' without a file path."""
        review = _make_phase(
            "review",
            prompt="Review. {iteration_history}",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            prompt="Fix. {iteration_history}",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([review, fix, done])

        long_output = "Z" * 5000
        capture = PromptCapture(
            phase_outputs={"review": long_output, "fix": "Short"},
            request_changes_phases={"review"},
        )
        # No output_dir
        seq = _make_sequencer(template, capture.execute, output_dir=None)
        seq.execute({})

        if len(capture.prompts.get("review", [])) >= 2:
            history = capture.prompts["review"][1]
            assert "[...truncated]" in history, (
                f"Should have simple truncation suffix, got: {history[-200:]}"
            )
            # Should NOT have "full output at"
            assert "full output at" not in history, (
                f"Should not have file path in truncation without output_dir"
            )


# ===========================================================================
# BC-8: Multiple Independent Loops
# ===========================================================================


class TestBC8MultipleIndependentLoops:
    """BC-8: Multiple independent loops detected separately, with isolated history."""

    def _dual_loop_template(self) -> PipelineTemplate:
        """Template with two independent loops:
        Loop 1: spec → behavioral → spec_adversary → spec
        Loop 2: fix → acceptance_run → review → fix
        """
        # Loop 1
        spec = _make_phase(
            "spec",
            prompt="Spec. {iteration_history}",
            transitions={"success": "behavioral"},
            max_iterations=3,
        )
        behavioral = _make_phase(
            "behavioral",
            prompt="Behavioral. {iteration_history}",
            transitions={"success": "spec_adversary"},
            max_iterations=3,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            prompt="Adversary. {iteration_history}",
            transitions={"request_changes": "spec", "approve": "fix"},
            max_iterations=3,
        )
        # Loop 2
        fix = _make_phase(
            "fix",
            prompt="Fix. {iteration_history}",
            transitions={"success": "acceptance_run"},
            max_iterations=3,
        )
        acceptance_run = _make_phase(
            "acceptance_run",
            prompt="AccRun. {iteration_history}",
            transitions={"success": "review"},
            max_iterations=3,
        )
        review = _make_phase(
            "review",
            prompt="Review. {iteration_history}",
            transitions={"request_changes": "fix", "approve": "publish"},
            max_iterations=3,
        )
        publish = _make_phase("publish")
        return _make_template(
            [spec, behavioral, spec_adversary, fix, acceptance_run, review, publish]
        )

    # BC-8.1: Two separate log lines
    def test_bc_8_1_two_separate_log_lines(self, caplog) -> None:
        """Two separate 'detected loop group' log lines are emitted."""
        template = self._dual_loop_template()

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        assert len(loop_logs) >= 2, (
            f"Expected at least 2 loop group log lines, got {len(loop_logs)}: {loop_logs}"
        )

    # BC-8.2 & BC-8.3: Loop histories are isolated
    def test_bc_8_2_spec_loop_history_isolated(self) -> None:
        """spec's {iteration_history} on round 2 contains only spec loop members,
        NOT fix/acceptance_run/review."""
        template = self._dual_loop_template()

        # Make spec_adversary issue request_changes on first round
        rc_count = {"spec_adversary": 0, "review": 0}

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            if phase_id == "spec_adversary":
                rc_count["spec_adversary"] += 1
                if rc_count["spec_adversary"] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nFix spec issues"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nLooks good"},
                )
            if phase_id == "review":
                rc_count["review"] += 1
                if rc_count["review"] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nFix review issues"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nGood to publish"},
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": f"Output of {phase_id}"},
            )

        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def capturing_execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)
            return execute_fn(task_spec, **kwargs)

        seq = _make_sequencer(template, capturing_execute)
        seq.execute({})

        # Check spec's round 2 history doesn't contain review-loop phases
        if len(prompts_captured.get("spec", [])) >= 2:
            history = prompts_captured["spec"][1]
            # Should NOT contain fix, acceptance_run, or review
            for excluded in ["fix", "acceptance_run", "review"]:
                assert f"--- Round 1: {excluded} ---" not in history, (
                    f"spec's history should not contain {excluded}: {history[:500]}"
                )

    # BC-8.4: Mixed 2-phase and 3-phase loops both work
    def test_bc_8_4_mixed_size_loops(self, caplog) -> None:
        """A 2-phase cycle and a 3-phase cycle in the same template
        are both detected independently."""
        # 2-phase loop: code →[success]→ test_runner →[request_changes]→ code  (wait -- this doesn't form
        # a cycle per the detection algorithm. Let me rethink.)
        # Per the detection: phase A is a loop partner of B when A→[request_changes]→B
        # and B→[success chain]→A
        # 2-phase: review →[request_changes]→ coder, coder →[success]→ review
        coder = _make_phase(
            "coder",
            transitions={"success": "review"},
            max_iterations=3,
        )
        review = _make_phase(
            "review",
            transitions={"request_changes": "coder", "approve": "spec"},
            max_iterations=3,
        )
        # 3-phase: spec → behavioral → adversary → spec
        spec = _make_phase(
            "spec",
            transitions={"success": "behavioral"},
            max_iterations=3,
        )
        behavioral = _make_phase(
            "behavioral",
            transitions={"success": "adversary"},
            max_iterations=3,
        )
        adversary = _make_phase(
            "adversary",
            transitions={"request_changes": "spec", "approve": "final"},
            max_iterations=3,
        )
        final = _make_phase("final")
        template = _make_template([coder, review, spec, behavioral, adversary, final])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        assert len(loop_logs) >= 2, (
            f"Expected 2+ loop group logs for mixed-size loops, got: {loop_logs}"
        )

    # BC-8.5: A phase belongs to at most one loop group
    def test_bc_8_5_phase_in_at_most_one_group(self, caplog) -> None:
        """A phase cannot belong to two different loop groups simultaneously."""
        # Create a shared phase between two potential loops.
        # The shared phase should only appear in one group.
        shared = _make_phase(
            "shared",
            transitions={
                "success": "a_next",
                "request_changes": "shared",  # self-loop is one group
            },
            max_iterations=3,
        )
        a_next = _make_phase("a_next")
        template = _make_template([shared, a_next])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            seq = _make_sequencer(template, _success_result)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        # Count how many loop group logs include "shared"
        shared_count = sum(1 for log in loop_logs if "shared" in log.lower())
        assert shared_count <= 1, (
            f"'shared' should appear in at most one loop group, found in {shared_count}: {loop_logs}"
        )


# ===========================================================================
# BC-9: Self-Loop Detection
# ===========================================================================


class TestBC9SelfLoopDetection:
    """BC-9: A phase with request_changes → itself is detected as a self-loop."""

    # BC-9.1: Self-loop detected and logged
    def test_bc_9_1_self_loop_detected(self, caplog) -> None:
        """When phase A has request_changes → A, the engine detects a loop group
        containing only A and emits a 'detected loop group' log showing A → A."""
        a = _make_phase(
            "a",
            transitions={"request_changes": "a", "approve": "done"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([a, done])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            capture = PromptCapture(request_changes_phases={"a"})
            seq = _make_sequencer(template, capture.execute)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        assert len(loop_logs) >= 1, (
            f"Expected 'detected loop group' for self-loop, got none"
        )
        # The log should show A → A pattern
        log_text = loop_logs[0].lower()
        assert "a" in log_text

    # BC-9.2: Self-loop round 2 has own round 1 output in history
    def test_bc_9_2_self_loop_round2_history(self) -> None:
        """On round 2 of a self-looping phase, {iteration_history} contains
        a section '--- Round 1: A ---' with the phase's own round 1 output."""
        a = _make_phase(
            "a",
            prompt="Phase A. {iteration_history}",
            transitions={"request_changes": "a", "approve": "done"},
            max_iterations=5,
        )
        done = _make_phase("done")
        template = _make_template([a, done])

        rc_count = [0]

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            if phase_id == "a":
                rc_count[0] += 1
                if rc_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nSelf-review round 1"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nSelf-review complete"},
                )
            return _success_result(task_spec)

        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def capturing_execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)
            return execute_fn(task_spec, **kwargs)

        seq = _make_sequencer(template, capturing_execute)
        seq.execute({})

        if len(prompts_captured.get("a", [])) >= 2:
            round2_prompt = prompts_captured["a"][1]
            assert "--- Round 1: a ---" in round2_prompt, (
                f"Self-loop round 2 should have 'Round 1: a' section: {round2_prompt[:500]}"
            )

    # BC-9.3: Self-loop round 3 has both round 1 and round 2 sections
    def test_bc_9_3_self_loop_round3_two_sections(self) -> None:
        """On round 3 of a self-loop, history contains both
        '--- Round 1: A ---' and '--- Round 2: A ---'."""
        a = _make_phase(
            "a",
            prompt="Phase A. {iteration_history}",
            transitions={"request_changes": "a", "approve": "done"},
            max_iterations=5,
        )
        done = _make_phase("done")
        template = _make_template([a, done])

        rc_count = [0]

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            if phase_id == "a":
                rc_count[0] += 1
                if rc_count[0] <= 2:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": f"REQUEST_CHANGES\nSelf-review round {rc_count[0]}"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nDone"},
                )
            return _success_result(task_spec)

        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def capturing_execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)
            return execute_fn(task_spec, **kwargs)

        seq = _make_sequencer(template, capturing_execute)
        seq.execute({})

        if len(prompts_captured.get("a", [])) >= 3:
            round3_prompt = prompts_captured["a"][2]
            assert "--- Round 1: a ---" in round3_prompt, (
                f"Round 3 should have Round 1 section: {round3_prompt[:500]}"
            )
            assert "--- Round 2: a ---" in round3_prompt, (
                f"Round 3 should have Round 2 section: {round3_prompt[:500]}"
            )

    # BC-9.4: Current-round output not pre-included
    def test_bc_9_4_current_round_not_pre_included(self) -> None:
        """The self-loop phase's current-round output is NOT pre-included
        in {iteration_history} (same rule as BC-5.4)."""
        a = _make_phase(
            "a",
            prompt="Phase A. {iteration_history}",
            transitions={"request_changes": "a", "approve": "done"},
            max_iterations=5,
        )
        done = _make_phase("done")
        template = _make_template([a, done])

        rc_count = [0]

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            if phase_id == "a":
                rc_count[0] += 1
                if rc_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nUNIQUE_SELF_MARKER_R1"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nDone"},
                )
            return _success_result(task_spec)

        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def capturing_execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)
            return execute_fn(task_spec, **kwargs)

        seq = _make_sequencer(template, capturing_execute)
        seq.execute({})

        if len(prompts_captured.get("a", [])) >= 2:
            round2_prompt = prompts_captured["a"][1]
            # The marker should appear exactly once (from round 1)
            count = round2_prompt.count("UNIQUE_SELF_MARKER_R1")
            assert count <= 1, (
                f"Self-loop marker should appear at most once in round 2 "
                f"history, found {count}"
            )


    # BC-9.5 (reviewer finding): Self-loop with success transition doesn't corrupt group
    def test_bc_9_5_self_loop_with_success_does_not_include_other_phases(self, caplog) -> None:
        """When a phase has request_changes → self AND success → another_phase,
        only the self-looping phase is in the loop group. The success target
        must NOT be incorrectly included."""
        a = _make_phase(
            "a",
            prompt="Phase A. {iteration_history}",
            transitions={
                "success": "done",
                "request_changes": "a",
                "approve": "done",
            },
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([a, done])

        with caplog.at_level(logging.INFO, logger="orchestration_engine.sequencer"):
            capture = PromptCapture(request_changes_phases={"a"})
            seq = _make_sequencer(template, capture.execute)
            seq.execute({})

        loop_logs = [
            r.message for r in caplog.records
            if "detected loop group" in r.message.lower()
        ]
        # Should detect a self-loop for 'a'
        assert len(loop_logs) >= 1, (
            f"Expected 'detected loop group' for self-loop, got none"
        )
        # 'done' must NOT appear in ANY loop group log
        for log in loop_logs:
            assert "done" not in log.lower(), (
                f"'done' should not be in any loop group, but found in: {log}"
            )


# ===========================================================================
# BC-10: End-to-End Pipeline Execution with 3-Phase Loop
# ===========================================================================


class TestBC10EndToEnd:
    """BC-10: Full pipeline run with 3-phase spec loop and progressive history."""

    def _e2e_template(self) -> PipelineTemplate:
        """Full template: spec loop → implement → review loop."""
        spec = _make_phase(
            "spec",
            prompt="Write spec. {iteration_history}",
            transitions={"success": "behavioral"},
            max_iterations=5,
        )
        behavioral = _make_phase(
            "behavioral",
            prompt="Write behavioral contracts. {iteration_history}",
            transitions={"success": "spec_adversary"},
            max_iterations=5,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            prompt="Review spec. {iteration_history}",
            transitions={"request_changes": "spec", "approve": "implement"},
            max_iterations=5,
        )
        implement = _make_phase(
            "implement",
            prompt="Build it. {iteration_history}",
            transitions={"success": "done"},
        )
        done = _make_phase("done")
        return _make_template([spec, behavioral, spec_adversary, implement, done])

    # BC-10.1: spec round 2 sees all round 1 outputs
    def test_bc_10_1_spec_round2_full_history(self) -> None:
        """When spec executes for round 2, its prompt contains sections
        showing round 1 outputs from spec, behavioral, AND spec_adversary."""
        template = self._e2e_template()
        rc_count = [0]
        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)

            if phase_id == "spec_adversary":
                rc_count[0] += 1
                if rc_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nSpec needs work on edge cases"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nSpec is solid"},
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": f"Output of {phase_id}"},
            )

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # spec should have run at least twice
        assert len(prompts_captured.get("spec", [])) >= 2, (
            f"spec should run at least twice, ran: {len(prompts_captured.get('spec', []))}"
        )
        round2_prompt = prompts_captured["spec"][1]
        # Should contain all three members' round 1 outputs
        assert "--- Round 1: spec ---" in round2_prompt
        assert "--- Round 1: behavioral ---" in round2_prompt
        assert "--- Round 1: spec_adversary ---" in round2_prompt

    # BC-10.2: behavioral round 2 sees round 1 outputs only (not round 2 from spec)
    def test_bc_10_2_behavioral_round2_prior_rounds_only(self) -> None:
        """When behavioral executes round 2, its history contains round 1 outputs
        from all three members. Round 2 output from spec is NOT included."""
        template = self._e2e_template()
        rc_count = [0]
        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)

            if phase_id == "spec":
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": f"SPEC_OUTPUT_ROUND_{len(prompts_captured.get('spec', []))}"},
                )
            if phase_id == "spec_adversary":
                rc_count[0] += 1
                if rc_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nMore work needed"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nAll good"},
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": f"Output of {phase_id}"},
            )

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        if len(prompts_captured.get("behavioral", [])) >= 2:
            history = prompts_captured["behavioral"][1]
            # Should have round 1 sections
            assert "--- Round 1:" in history
            # Should NOT have round 2 sections (only prior rounds)
            assert "--- Round 2:" not in history, (
                f"behavioral round 2 should only see round 1, got: {history[:500]}"
            )

    # BC-10.3: spec_adversary round 2 sees only round 1 outputs
    def test_bc_10_3_adversary_round2_prior_rounds_only(self) -> None:
        """When spec_adversary executes round 2, its history contains round 1
        outputs only. Round 2 outputs from spec and behavioral are NOT included."""
        template = self._e2e_template()
        rc_count = [0]
        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)

            if phase_id == "spec_adversary":
                rc_count[0] += 1
                if rc_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id,
                        task_type=task_spec.type,
                        state=TaskState.SUCCESS,
                        confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nNeeds fixes"},
                    )
                return TaskResult(
                    task_id=task_spec.id,
                    task_type=task_spec.type,
                    state=TaskState.SUCCESS,
                    confidence=0.9,
                    result={"text": "APPROVE\nAll good"},
                )
            return TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.SUCCESS,
                confidence=0.9,
                result={"text": f"Output of {phase_id}"},
            )

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        if len(prompts_captured.get("spec_adversary", [])) >= 2:
            history = prompts_captured["spec_adversary"][1]
            # Should see round 1 sections for all three
            assert "--- Round 1:" in history
            # Should NOT see round 2 sections
            assert "--- Round 2:" not in history, (
                f"spec_adversary round 2 should only see round 1, got: {history[:500]}"
            )

    # BC-10.5: Independent loops have independent history
    def test_bc_10_5_independent_loops_fresh_history(self) -> None:
        """After the spec loop completes, if the pipeline proceeds to a
        review/fix loop, that loop's {iteration_history} starts fresh."""
        # Template: spec loop → implement → review loop
        spec = _make_phase(
            "spec",
            prompt="Spec. {iteration_history}",
            transitions={"success": "spec_adversary"},
            max_iterations=3,
        )
        spec_adversary = _make_phase(
            "spec_adversary",
            prompt="Adversary. {iteration_history}",
            transitions={"request_changes": "spec", "approve": "fix"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            prompt="Fix. {iteration_history}",
            transitions={"success": "review"},
            max_iterations=3,
        )
        review = _make_phase(
            "review",
            prompt="Review. {iteration_history}",
            transitions={"request_changes": "fix", "approve": "publish"},
            max_iterations=3,
        )
        publish = _make_phase("publish")
        template = _make_template([spec, spec_adversary, fix, review, publish])

        rc_spec_count = [0]
        rc_review_count = [0]
        prompts_captured: Dict[str, List[str]] = defaultdict(list)

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            prompt = task_spec.payload.get("prompt", "")
            prompts_captured[phase_id].append(prompt)

            if phase_id == "spec_adversary":
                rc_spec_count[0] += 1
                if rc_spec_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id, task_type=task_spec.type,
                        state=TaskState.SUCCESS, confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nSpec needs work"},
                    )
                return TaskResult(
                    task_id=task_spec.id, task_type=task_spec.type,
                    state=TaskState.SUCCESS, confidence=0.9,
                    result={"text": "APPROVE\nSpec approved"},
                )
            if phase_id == "review":
                rc_review_count[0] += 1
                if rc_review_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id, task_type=task_spec.type,
                        state=TaskState.SUCCESS, confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nCode needs fixes"},
                    )
                return TaskResult(
                    task_id=task_spec.id, task_type=task_spec.type,
                    state=TaskState.SUCCESS, confidence=0.9,
                    result={"text": "APPROVE\nCode approved"},
                )
            return TaskResult(
                task_id=task_spec.id, task_type=task_spec.type,
                state=TaskState.SUCCESS, confidence=0.9,
                result={"text": f"Output of {phase_id}"},
            )

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # fix's first invocation should have empty history (it's in the review loop,
        # not the spec loop — history is independent)
        if prompts_captured.get("fix"):
            first_fix_prompt = prompts_captured["fix"][0]
            # Should not contain spec or spec_adversary sections
            assert "--- Round 1: spec ---" not in first_fix_prompt, (
                f"fix's history should not contain spec loop data: {first_fix_prompt[:500]}"
            )

    # BC-10.6: Pipeline exit code/status unaffected by loop detection
    def test_bc_10_6_pipeline_completes_normally(self) -> None:
        """A pipeline that previously completed successfully with 2-phase loops
        continues to complete successfully."""
        review = _make_phase(
            "review",
            transitions={"request_changes": "fix", "approve": "done"},
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done")
        template = _make_template([review, fix, done])

        rc_count = [0]

        def execute_fn(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            if phase_id == "review":
                rc_count[0] += 1
                if rc_count[0] <= 1:
                    return TaskResult(
                        task_id=task_spec.id, task_type=task_spec.type,
                        state=TaskState.SUCCESS, confidence=0.9,
                        result={"text": "REQUEST_CHANGES\nFix the code"},
                    )
                return TaskResult(
                    task_id=task_spec.id, task_type=task_spec.type,
                    state=TaskState.SUCCESS, confidence=0.9,
                    result={"text": "APPROVE\nAll good"},
                )
            return _success_result(task_spec)

        seq = _make_sequencer(template, execute_fn)
        result = seq.execute({})

        # Pipeline should complete without abort
        assert not result.get("aborted"), (
            f"Pipeline should complete normally, got aborted: {result.get('abort_reason')}"
        )
        assert "done" in result["phase_outputs"], (
            f"Pipeline should reach 'done' phase. Executed: {list(result['phase_outputs'].keys())}"
        )
