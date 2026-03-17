"""Tests for content-based routing (Issue #301).

Covers:
- extract_verdict(): unit tests for all verdict keywords
- extract_verdict(): edge cases (blank lines, casing, no-match, empty input)
- _resolve_next_phase(): verdict keys present → content routing used
- _resolve_next_phase(): no verdict keys in transitions → content routing skipped
- _resolve_next_phase(): verdict in output but no matching key → falls back to outcome
- _resolve_next_phase(): result is None → falls back to outcome
- End-to-end: review→fix loop routes correctly on REQUEST_CHANGES verdict
- End-to-end: review→test routes correctly on APPROVE verdict
- End-to-end: loop respects max_iterations guard on fix phase
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from orchestration_engine.schemas import TaskError, TaskResult, TaskState
from orchestration_engine.sequencer import StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate
from orchestration_engine.transitions import PhaseOutcome, extract_verdict


# ---------------------------------------------------------------------------
# Helpers (mirror pattern from test_state_machine_sequencer.py)
# ---------------------------------------------------------------------------


def _make_phase(
    phase_id: str,
    prompt: str = "Do something",
    transitions: Optional[Dict[str, str]] = None,
    max_iterations: int = 0,
) -> PhaseDefinition:
    """Build a minimal PhaseDefinition."""
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        transitions=transitions or {},
        max_iterations=max_iterations,
    )


def _make_template(phases: List[PhaseDefinition]) -> PipelineTemplate:
    """Build a PipelineTemplate from a list of phases."""
    return PipelineTemplate(
        id="content-routing-test",
        name="Content Routing Test Pipeline",
        phases=phases,
    )


def _result_with_text(task_spec, text: str = "", state: str = "success", **kwargs) -> TaskResult:
    """Return a TaskResult whose result.text is *text*."""
    task_state = TaskState.SUCCESS if state == "success" else TaskState.FAILED
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=task_state,
        confidence=0.9,
        result={"text": text},
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


def _make_sequencer(template: PipelineTemplate, execute_fn: Callable) -> StateMachineSequencer:
    """Build a StateMachineSequencer backed by a mock runner."""
    runner = _build_runner(execute_fn)
    return StateMachineSequencer(template=template, runner=runner)


# ---------------------------------------------------------------------------
# 1. Unit tests for extract_verdict()
# ---------------------------------------------------------------------------


class TestExtractVerdict:
    """Unit tests for transitions.extract_verdict."""

    # -- Happy path: each keyword ------------------------------------------

    def test_approve_lowercase(self):
        assert extract_verdict("approve\nsome explanation") == "approve"

    def test_approve_uppercase(self):
        assert extract_verdict("APPROVE\nsome explanation") == "approve"

    def test_approve_mixed_case(self):
        assert extract_verdict("Approve: looks good") == "approve"

    def test_request_changes(self):
        assert extract_verdict("REQUEST_CHANGES\nfix the auth bug") == "request_changes"

    def test_request_changes_lowercase(self):
        assert extract_verdict("request_changes: fix line 42") == "request_changes"

    def test_abort(self):
        assert extract_verdict("ABORT\nfatal issue found") == "abort"

    def test_abort_lowercase(self):
        assert extract_verdict("abort: cannot proceed") == "abort"

    # -- First non-blank line only ----------------------------------------

    def test_highest_priority_verdict_wins(self):
        """REQUEST_CHANGES beats APPROVE regardless of line order (Issue #600)."""
        text = "APPROVE\nREQUEST_CHANGES: nitpick\nSome body text"
        assert extract_verdict(text) == "request_changes"

    def test_keyword_after_first_line_found_by_full_scan(self):
        """A keyword on a later line IS found by the full scan.

        This supports streaming output (partial_output) where preamble
        text appears before the verdict.
        """
        text = "This is an introduction.\nAPPROVE"
        assert extract_verdict(text) == "approve"

    def test_keyword_mid_sentence_not_matched(self):
        """A keyword that doesn't START a line is not matched."""
        text = "The reviewer said APPROVE but we need more context."
        assert extract_verdict(text) is None

    def test_leading_blank_lines_skipped(self):
        """Blank lines before the first content line are ignored."""
        text = "\n\n  \nAPPROVE\nexplanation"
        assert extract_verdict(text) == "approve"

    def test_first_non_blank_no_keyword_scans_further(self):
        """First non-blank line has no keyword → full scan finds it on later line."""
        text = "This is an intro.\nREQUEST_CHANGES: something"
        assert extract_verdict(text) == "request_changes"

    # -- Edge cases -------------------------------------------------------

    def test_empty_string_returns_none(self):
        assert extract_verdict("") is None

    def test_none_equivalent_empty_returns_none(self):
        assert extract_verdict("") is None

    def test_whitespace_only_returns_none(self):
        assert extract_verdict("   \n   \n   ") is None

    def test_partial_match_not_approved(self):
        """'APPROVED' starts with 'APPROVE' — verify this is caught."""
        assert extract_verdict("APPROVED: looks great") == "approve"

    def test_request_changes_with_colon(self):
        assert extract_verdict("REQUEST_CHANGES: fix indentation") == "request_changes"

    def test_approve_with_inline_text(self):
        assert extract_verdict("APPROVE — well done") == "approve"


# ---------------------------------------------------------------------------
# 2. Unit tests for _resolve_next_phase() content routing
# ---------------------------------------------------------------------------


class TestResolveNextPhaseContentRouting:
    """Direct unit tests for StateMachineSequencer._resolve_next_phase."""

    def _make_seq(self, phases: List[PhaseDefinition]) -> StateMachineSequencer:
        tmpl = _make_template(phases)
        runner = MagicMock()
        runner.executors = []
        runner.queue = MagicMock()
        return StateMachineSequencer(template=tmpl, runner=runner)

    def _result(self, text: str) -> dict:
        """Minimal result dict with output text."""
        return {"state": "success", "result": {"text": text}}

    # -- Verdict-based routing active (verdict keys in transitions) --------

    def test_approve_verdict_routes_to_approve_target(self):
        phase = _make_phase("review", transitions={"approve": "test", "request_changes": "fix"})
        seq = self._make_seq([phase])
        result = self._result("APPROVE\nLooks good to me.")
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "test"

    def test_request_changes_verdict_routes_to_fix(self):
        phase = _make_phase("review", transitions={"approve": "test", "request_changes": "fix"})
        seq = self._make_seq([phase])
        result = self._result("REQUEST_CHANGES\nFix the null check on line 42.")
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "fix"

    def test_abort_verdict_routes_to_abort_target(self):
        phase = _make_phase("review", transitions={"approve": "test", "abort": "cleanup"})
        seq = self._make_seq([phase])
        result = self._result("ABORT\nFatal design flaw.")
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "cleanup"

    # -- Fallback: no verdict match → use outcome -------------------------

    def test_no_verdict_in_output_falls_back_to_outcome(self):
        """Phase has verdict keys in transitions but output has no verdict keyword."""
        phase = _make_phase("review", transitions={"approve": "test", "success": "fallback"})
        seq = self._make_seq([phase])
        result = self._result("Some analysis without a verdict on first line.")
        # No verdict → outcome.value ("success") is used
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "fallback"

    def test_verdict_present_but_no_matching_key_falls_back(self):
        """Verdict keyword found but its key isn't in transitions → fall back."""
        # transitions has "approve" but NOT "request_changes"
        phase = _make_phase("review", transitions={"approve": "test", "success": "fallback"})
        seq = self._make_seq([phase])
        result = self._result("REQUEST_CHANGES\nFix the bug.")
        # "request_changes" not in transitions → fall back to outcome "success"
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "fallback"

    # -- No verdict keys in transitions → content routing skipped --------

    def test_no_verdict_keys_in_transitions_skips_content_routing(self):
        """Phase with only outcome-key transitions: extract_verdict is never consulted."""
        phase = _make_phase("implement", transitions={"success": "review"})
        seq = self._make_seq([phase])
        # Even if "APPROVE" appears in the text, it should NOT affect routing here
        result = self._result("APPROVE\nDone!")
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "review"

    # -- None result (backward compatibility) ----------------------------

    def test_none_result_falls_back_to_outcome(self):
        phase = _make_phase("review", transitions={"approve": "test", "success": "fallback"})
        seq = self._make_seq([phase])
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, None)
        assert target == "fallback"

    def test_none_result_no_verdict_keys_falls_back_to_outcome(self):
        phase = _make_phase("implement", transitions={"success": "review"})
        seq = self._make_seq([phase])
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, None)
        assert target == "review"

    # -- Text extraction from flat dict fallback -------------------------

    def test_flat_text_key_in_result_also_extracted(self):
        """If result.result is missing, flat result['text'] is also tried."""
        phase = _make_phase("review", transitions={"approve": "test", "success": "fallback"})
        seq = self._make_seq([phase])
        result = {"state": "success", "text": "APPROVE\nOK"}
        target = seq._resolve_next_phase(phase, PhaseOutcome.SUCCESS, result)
        assert target == "test"


# ---------------------------------------------------------------------------
# 3. End-to-end integration tests with StateMachineSequencer
# ---------------------------------------------------------------------------


class TestReviewFixLoopEndToEnd:
    """End-to-end tests for the review→fix loop using StateMachineSequencer."""

    def _build_review_fix_template(self) -> PipelineTemplate:
        """Build a 3-phase template: implement → review → fix (with loop back)."""
        implement = _make_phase("implement", transitions={"success": "review"})
        review = _make_phase(
            "review",
            transitions={
                "approve": "done",
                "request_changes": "fix",
                "success": "done",  # fallback
            },
            max_iterations=3,
        )
        fix = _make_phase(
            "fix",
            transitions={"success": "review"},
            max_iterations=3,
        )
        done = _make_phase("done", transitions={})
        return _make_template([implement, review, fix, done])

    def test_approve_on_first_review_skips_fix(self):
        """If review outputs APPROVE immediately, fix phase is never executed."""
        executed: List[str] = []

        def execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            executed.append(phase_id)
            text = "APPROVE\nCode looks great."
            return _result_with_text(task_spec, text=text)

        tmpl = self._build_review_fix_template()
        seq = _make_sequencer(tmpl, execute)
        seq.execute({})

        assert "implement" in executed
        assert "review" in executed
        assert "fix" not in executed
        assert "done" in executed

    def test_request_changes_routes_to_fix_then_approve_routes_to_done(self):
        """First review → REQUEST_CHANGES → fix runs → second review → APPROVE → done."""
        call_count: Dict[str, int] = {}

        def execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            call_count[phase_id] = call_count.get(phase_id, 0) + 1

            if phase_id == "review":
                # First call → REQUEST_CHANGES; subsequent → APPROVE
                if call_count["review"] == 1:
                    text = "REQUEST_CHANGES\nFix the null check."
                else:
                    text = "APPROVE\nLooks great now."
            else:
                text = f"Done with {phase_id}"
            return _result_with_text(task_spec, text=text)

        tmpl = self._build_review_fix_template()
        seq = _make_sequencer(tmpl, execute)
        seq.execute({})

        assert call_count.get("review", 0) == 2
        assert call_count.get("fix", 0) == 1
        assert call_count.get("done", 0) == 1

    def test_max_iterations_stops_fix_loop(self):
        """After max_iterations fix cycles, the loop terminates via cycle guard."""
        call_count: Dict[str, int] = {}

        def execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            call_count[phase_id] = call_count.get(phase_id, 0) + 1
            # Always REQUEST_CHANGES — loop should hit max_iterations
            text = "REQUEST_CHANGES\nMore issues found." if phase_id == "review" else "Fixed."
            return _result_with_text(task_spec, text=text)

        tmpl = self._build_review_fix_template()
        seq = _make_sequencer(tmpl, execute)
        result = seq.execute({})

        # fix and review both have max_iterations=3 — pipeline must terminate
        # (either by cycle guard or max_iterations abort — either way, it ends)
        assert result is not None, "Pipeline must return a result (not hang)"
        total_fix = call_count.get("fix", 0)
        assert total_fix <= 3, f"fix ran {total_fix} times — exceeds max_iterations"

    def test_non_review_phase_unaffected_by_verdict(self):
        """implement phase (no verdict keys in transitions) is never content-routed."""
        executed: List[str] = []

        def execute(task_spec, **kwargs):
            phase_id = task_spec.payload.get("phase_id", "?")
            executed.append(phase_id)
            # implement outputs "APPROVE" — should NOT affect its own routing
            text = "APPROVE\nSpec done" if phase_id == "implement" else "APPROVE\nOK"
            return _result_with_text(task_spec, text=text)

        tmpl = self._build_review_fix_template()
        seq = _make_sequencer(tmpl, execute)
        seq.execute({})

        # implement should transition to review, not done (which is the "approve" target)
        assert executed.index("review") > executed.index("implement")
