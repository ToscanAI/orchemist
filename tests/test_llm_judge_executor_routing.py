"""Comprehensive tests for LLMJudgeGrader executor routing (Issue #171).

Covers every acceptance criterion and edge case for the executor-routing
feature added to LLMJudgeGrader:

1.  Constructor — all parameter combinations + WARNING-4 (api_key isolation)
2.  Dispatch priority order — dry-run > executor > api_key > no-key
3.  Dry-run mode — all edge cases (priority, custom score, clamping)
4.  Executor routing — happy path, score parsing, error handling
5.  Score parsing edge cases — decimals, integers, clamping, case-insensitive
6.  Output text extraction (holdout principle) — priority order for all fields
7.  Holdout principle — metadata never reaches the judge
8.  TaskSpec construction — payload, system prompt, type, created_by
9.  Model tier mapping — all known models + unknown fallback
10. Executor result extraction — dict/str/missing result data formats
11. API-key path — backward compatibility (unchanged urllib behaviour)
12. No-key fallback — graceful degradation when nothing is configured
13. ScenarioRunner — executor forwarding from constructor through grade()
14. Dispatch priority — executor beats api_key, dry-run beats everything
"""

from __future__ import annotations

import json
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable in CI environments
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scenario_runner.graders.llm_judge import (
    LLMJudgeGrader,
    _JUDGE_SYSTEM_PROMPT,
    _SCORE_RE,
)
from scenario_runner.models import GradeResult
from scenario_runner.runner import ScenarioRunner


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _make_task_result(
    response_text: str = "Score: 0.80\nGood article.",
    success: bool = True,
    result_data: Any = None,
) -> MagicMock:
    """Build a mock TaskResult.

    Parameters
    ----------
    response_text:
        Text placed in ``result["text"]``.
    success:
        If True, ``state`` is ``TaskState.SUCCESS``; otherwise ``FAILED``.
    result_data:
        Override the ``result`` attribute directly (for non-standard formats).
    """
    from orchestration_engine.schemas import TaskState

    mock = MagicMock()
    mock.state = TaskState.SUCCESS if success else TaskState.FAILED
    if result_data is not None:
        mock.result = result_data
    else:
        mock.result = {"text": response_text} if success else {}
    mock.errors = (
        []
        if success
        else [MagicMock(message="Simulated executor failure")]
    )
    return mock


def _make_executor(
    response_text: str = "Score: 0.80\nGood article.",
    success: bool = True,
    side_effect=None,
    result_data: Any = None,
) -> MagicMock:
    """Return a mock executor whose ``execute()`` returns a controlled TaskResult."""
    executor = MagicMock()
    if side_effect is not None:
        executor.execute.side_effect = side_effect
    else:
        executor.execute.return_value = _make_task_result(
            response_text=response_text,
            success=success,
            result_data=result_data,
        )
    return executor


def _capture_task_spec_from_grade(
    output: dict,
    rubric: str,
    judge_model: str,
    output_field=None,
):
    """Grade with an executor that captures the TaskSpec; return (result, task_spec)."""
    from orchestration_engine.schemas import TaskSpec

    captured: list[TaskSpec] = []

    def capture(task: TaskSpec):
        captured.append(task)
        return _make_task_result("Score: 0.75\nGood.")

    executor = MagicMock()
    executor.execute.side_effect = capture
    grader = LLMJudgeGrader(executor=executor)
    result = grader.grade(output, rubric, judge_model, output_field=output_field)
    return result, captured[0] if captured else None


# ===========================================================================
# 1. Constructor tests
# ===========================================================================


class TestLLMJudgeGraderConstructor:
    """LLMJudgeGrader.__init__ — parameter handling and initialisation invariants."""

    def test_no_params_uses_env_api_key(self):
        """With no params and ANTHROPIC_API_KEY set, api_key is read from env."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-key"}):
            grader = LLMJudgeGrader()
        assert grader.api_key == "sk-env-key"
        assert grader.executor is None

    def test_no_params_no_env_key_api_key_is_none(self):
        """With no params and no env var, api_key is None."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            grader = LLMJudgeGrader()
        assert grader.api_key is None

    def test_explicit_api_key_stored(self):
        """Explicit api_key is stored as-is."""
        grader = LLMJudgeGrader(api_key="sk-explicit")
        assert grader.api_key == "sk-explicit"

    def test_explicit_api_key_beats_env_var(self):
        """Explicit api_key parameter takes precedence over ANTHROPIC_API_KEY env var."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env"}):
            grader = LLMJudgeGrader(api_key="sk-explicit")
        assert grader.api_key == "sk-explicit"

    # WARNING-4: executor presence suppresses api_key resolution entirely
    def test_executor_provided_api_key_is_none_even_with_explicit_key(self):
        """When executor is given, api_key is set to None — executor wins.

        This is the WARNING-4 fix: executor failures must surface explicitly
        rather than silently retrying via the urllib path.
        """
        executor = _make_executor()
        grader = LLMJudgeGrader(api_key="sk-should-not-be-stored", executor=executor)
        assert grader.api_key is None
        assert grader.executor is executor

    def test_executor_provided_api_key_is_none_even_with_env_var(self):
        """When executor is given, ANTHROPIC_API_KEY env var is ignored."""
        executor = _make_executor()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-key"}):
            grader = LLMJudgeGrader(executor=executor)
        assert grader.api_key is None
        assert grader.executor is executor

    def test_executor_none_env_var_still_used(self):
        """When executor is None (default), ANTHROPIC_API_KEY env var is still read."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-key"}):
            grader = LLMJudgeGrader(executor=None)
        assert grader.api_key == "sk-env-key"

    def test_default_dry_run_stub_score(self):
        """Default dry_run_stub_score is 0.8."""
        grader = LLMJudgeGrader()
        assert grader.dry_run_stub_score == pytest.approx(0.8)

    def test_custom_dry_run_stub_score(self):
        """Custom dry_run_stub_score is stored accurately."""
        grader = LLMJudgeGrader(dry_run_stub_score=0.55)
        assert grader.dry_run_stub_score == pytest.approx(0.55)

    def test_stub_score_clamped_above_one(self):
        """dry_run_stub_score > 1.0 is clamped to 1.0."""
        grader = LLMJudgeGrader(dry_run_stub_score=2.5)
        assert grader.dry_run_stub_score == pytest.approx(1.0)

    def test_stub_score_clamped_below_zero(self):
        """dry_run_stub_score < 0.0 is clamped to 0.0."""
        grader = LLMJudgeGrader(dry_run_stub_score=-1.0)
        assert grader.dry_run_stub_score == pytest.approx(0.0)

    def test_stub_score_exactly_zero_stored(self):
        """dry_run_stub_score=0.0 is stored without modification."""
        grader = LLMJudgeGrader(dry_run_stub_score=0.0)
        assert grader.dry_run_stub_score == pytest.approx(0.0)

    def test_stub_score_exactly_one_stored(self):
        """dry_run_stub_score=1.0 is stored without modification."""
        grader = LLMJudgeGrader(dry_run_stub_score=1.0)
        assert grader.dry_run_stub_score == pytest.approx(1.0)

    def test_executor_stored_as_attribute(self):
        """Executor object is stored at self.executor."""
        executor = _make_executor()
        grader = LLMJudgeGrader(executor=executor)
        assert grader.executor is executor


# ===========================================================================
# 2. Dispatch priority order
# ===========================================================================


class TestDispatchPriorityOrder:
    """grade() dispatch tree: dry-run > executor > api_key > no-key."""

    def test_dry_run_beats_executor(self):
        """ORCH_DRY_RUN=1 takes priority over executor — executor never called."""
        executor = _make_executor("Score: 0.99\nPerfect.")
        grader = LLMJudgeGrader(executor=executor)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({"article": "text"}, "rubric", "model")

        executor.execute.assert_not_called()
        assert result.score == pytest.approx(0.8)  # default stub

    def test_dry_run_beats_api_key(self):
        """ORCH_DRY_RUN=1 takes priority over api_key — urllib never called."""
        grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
                result = grader.grade({"article": "text"}, "rubric", "model")

        mock_urlopen.assert_not_called()
        assert result.score == pytest.approx(0.8)

    def test_executor_beats_api_key(self):
        """Executor path wins when executor is set (api_key is set to None at init)."""
        executor = _make_executor("Score: 0.70\nDecent.")
        # api_key param is discarded by WARNING-4 logic when executor given
        grader = LLMJudgeGrader(api_key="sk-should-not-be-used", executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = grader.grade({"article": "text"}, "rubric", "model")

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result.score == pytest.approx(0.70)

    def test_executor_beats_env_api_key(self):
        """Executor path wins even when ANTHROPIC_API_KEY env var is set."""
        executor = _make_executor("Score: 0.65\nOK.")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-key"}):
            grader = LLMJudgeGrader(executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = grader.grade({"article": "text"}, "rubric", "model")

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result.score == pytest.approx(0.65)

    def test_dry_run_zero_does_not_activate_dry_run(self):
        """ORCH_DRY_RUN=0 does NOT activate dry-run — executor proceeds normally."""
        executor = _make_executor("Score: 0.90\nExcellent.")
        grader = LLMJudgeGrader(executor=executor)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "0"}):
            result = grader.grade({"article": "text"}, "rubric", "model")

        executor.execute.assert_called_once()
        assert result.score == pytest.approx(0.90)

    def test_dry_run_true_string_does_not_activate(self):
        """ORCH_DRY_RUN=true (not exactly '1') does NOT activate dry-run."""
        executor = _make_executor("Score: 0.88\nGood.")
        grader = LLMJudgeGrader(executor=executor)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "true"}):
            result = grader.grade({"article": "text"}, "rubric", "model")

        executor.execute.assert_called_once()
        assert result.score == pytest.approx(0.88)  # not the stub 0.8

    def test_no_key_no_executor_returns_graceful_error(self):
        """No api_key + no executor + ORCH_DRY_RUN not set → 'No API key' result."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ORCH_DRY_RUN")
        }
        with patch.dict("os.environ", env, clear=True):
            grader = LLMJudgeGrader()

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert "No API key" in result.details
        assert result.grader_type == "llm_judge"


# ===========================================================================
# 3. Dry-run mode — comprehensive
# ===========================================================================


class TestDryRunMode:
    """ORCH_DRY_RUN=1 short-circuit path — all edge cases."""

    def test_default_stub_score_returned(self):
        """ORCH_DRY_RUN=1 → score=0.8 (default), grader_type='llm_judge'."""
        grader = LLMJudgeGrader()

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.score == pytest.approx(0.8)
        assert result.passed is True
        assert result.grader_type == "llm_judge"

    def test_custom_stub_score_0_3_fails(self):
        """Custom stub score 0.3 → passed=False (below 0.5 baseline)."""
        grader = LLMJudgeGrader(dry_run_stub_score=0.3)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({}, "rubric", "model")

        assert result.score == pytest.approx(0.3)
        assert result.passed is False

    def test_stub_score_0_5_boundary_passes(self):
        """Stub score 0.5 → passed=True (boundary: score >= 0.5 is True)."""
        grader = LLMJudgeGrader(dry_run_stub_score=0.5)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({}, "rubric", "model")

        assert result.score == pytest.approx(0.5)
        assert result.passed is True

    def test_stub_score_0_49_boundary_fails(self):
        """Stub score 0.49 → passed=False (just below boundary)."""
        grader = LLMJudgeGrader(dry_run_stub_score=0.49)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({}, "rubric", "model")

        assert result.score == pytest.approx(0.49)
        assert result.passed is False

    def test_details_mention_dry_run_context(self):
        """GradeResult.details mentions 'dry-run' or 'ORCH_DRY_RUN'."""
        grader = LLMJudgeGrader()

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({}, "rubric", "model")

        combined = (result.details or "").lower()
        assert "dry-run" in combined or "orch_dry_run" in result.details

    def test_executor_never_called_in_dry_run(self):
        """executor.execute() is never invoked when ORCH_DRY_RUN=1."""
        executor = _make_executor("Score: 1.0\nPerfect.")
        grader = LLMJudgeGrader(executor=executor)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            grader.grade({}, "rubric", "model")

        executor.execute.assert_not_called()

    def test_urllib_never_called_in_dry_run(self):
        """urllib.request.urlopen is never invoked when ORCH_DRY_RUN=1."""
        grader = LLMJudgeGrader(api_key="sk-test")

        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
                grader.grade({}, "rubric", "model")

        mock_urlopen.assert_not_called()

    def test_out_of_range_stub_score_clamped_to_1(self):
        """dry_run_stub_score=5.0 is clamped to 1.0 at construction."""
        grader = LLMJudgeGrader(dry_run_stub_score=5.0)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({}, "rubric", "model")

        assert result.score == pytest.approx(1.0)

    def test_negative_stub_score_clamped_to_0(self):
        """dry_run_stub_score=-99 is clamped to 0.0 at construction."""
        grader = LLMJudgeGrader(dry_run_stub_score=-99.0)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade({}, "rubric", "model")

        assert result.score == pytest.approx(0.0)

    def test_grade_signature_unchanged_in_dry_run(self):
        """grade() accepts output_field kwarg without error even in dry-run."""
        grader = LLMJudgeGrader()

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"body": "text"},
                rubric="rubric",
                judge_model="model",
                output_field="body",
            )

        assert result.score == pytest.approx(0.8)


# ===========================================================================
# 4. Executor routing — happy path and error paths
# ===========================================================================


class TestExecutorRouting:
    """_grade_with_executor: happy path, failure modes, edge cases."""

    def test_happy_path_score_parsed_correctly(self):
        """Executor returns 'Score: 0.85' → GradeResult(score=0.85, passed=True)."""
        executor = _make_executor("Score: 0.85\nGood article.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "Some text."}, "Rate it.", "claude-haiku-4-5")

        assert result.score == pytest.approx(0.85)
        assert result.passed is True
        assert result.grader_type == "llm_judge"

    def test_executor_called_exactly_once(self):
        """executor.execute() is called exactly once per grade() call."""
        executor = _make_executor("Score: 0.70\nOK.")
        grader = LLMJudgeGrader(executor=executor)

        grader.grade({"article": "text"}, "rubric", "model")

        assert executor.execute.call_count == 1

    def test_urllib_not_called_when_executor_set(self):
        """When executor is provided, urllib path is never reached."""
        executor = _make_executor("Score: 0.75\nGood.")
        grader = LLMJudgeGrader(executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            grader.grade({"article": "text"}, "rubric", "model")

        mock_urlopen.assert_not_called()

    def test_executor_failure_state_returns_zero(self):
        """Executor returning TaskState.FAILED → score=0.0, passed=False."""
        executor = _make_executor(success=False)
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert result.grader_type == "llm_judge"

    def test_executor_failure_details_mention_non_success_state(self):
        """GradeResult.details mentions 'non-success state' on executor failure."""
        executor = _make_executor(success=False)
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert "non-success state" in result.details.lower()

    def test_executor_runtime_error_returns_error_result(self):
        """RuntimeError from executor.execute() → score=0.0, error in details."""
        executor = _make_executor(side_effect=RuntimeError("gateway down"))
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert "RuntimeError" in result.details or "gateway down" in result.details

    def test_executor_connection_error_returns_zero(self):
        """ConnectionError from executor → score=0.0."""
        executor = _make_executor(side_effect=ConnectionError("no route to host"))
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert result.grader_type == "llm_judge"

    def test_executor_exception_type_appears_in_details(self):
        """The exception class name appears in error GradeResult.details."""
        executor = _make_executor(side_effect=ValueError("bad config"))
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert "ValueError" in result.details

    def test_empty_response_text_returns_zero(self):
        """Empty 'text' in executor result → score=0.0 and 'empty' in details."""
        executor = _make_executor(response_text="")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.score == pytest.approx(0.0)
        assert result.passed is False
        assert "empty" in result.details.lower()

    def test_whitespace_only_response_treated_as_empty(self):
        """Response text consisting only of whitespace → score=0.0."""
        executor = _make_executor(response_text="   \n  ")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        # Whitespace is falsy → treated as empty → score=0.0
        assert result.score == pytest.approx(0.0)

    def test_response_without_score_line_defaults_to_zero(self):
        """Executor response with no 'Score: X' line → score=0.0."""
        executor = _make_executor("This article looks decent to me.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.score == pytest.approx(0.0)
        assert "No score found" in result.details

    def test_no_score_details_include_original_response(self):
        """When no score found, original response text is preserved in details."""
        response = "The article is well-structured but misses key points."
        executor = _make_executor(response)
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert response[:40] in result.details or "No score found" in result.details

    def test_details_truncated_to_1000_chars(self):
        """Long executor responses are truncated to 1000 chars in details."""
        long_response = "Score: 0.75\n" + "A" * 2000
        executor = _make_executor(long_response)
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert len(result.details) <= 1000

    def test_passed_true_at_exactly_0_5(self):
        """passed=True when score == 0.5 (boundary: score >= 0.5)."""
        executor = _make_executor("Score: 0.5\nBorderline.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is True
        assert result.score == pytest.approx(0.5)

    def test_passed_false_just_below_0_5(self):
        """passed=False when score < 0.5."""
        executor = _make_executor("Score: 0.49\nJust below.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.49)


# ===========================================================================
# 5. Score parsing edge cases
# ===========================================================================


class TestScoreParsing:
    """_SCORE_RE regex and score extraction — all variants and clamping."""

    def _grade_with_response(self, response_text: str) -> GradeResult:
        """Helper: run grade() with a given executor response text."""
        executor = _make_executor(response_text)
        grader = LLMJudgeGrader(executor=executor)
        return grader.grade({"article": "text"}, "rubric", "model")

    def test_score_0_85(self):
        assert self._grade_with_response("Score: 0.85\n").score == pytest.approx(0.85)

    def test_score_1_0(self):
        assert self._grade_with_response("Score: 1.0\n").score == pytest.approx(1.0)

    def test_score_0_0(self):
        result = self._grade_with_response("Score: 0.0\n")
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_score_integer_1(self):
        """Score: 1 (integer, no decimal) → score=1.0."""
        assert self._grade_with_response("Score: 1\n").score == pytest.approx(1.0)

    def test_score_integer_0(self):
        """Score: 0 → score=0.0."""
        assert self._grade_with_response("Score: 0\n").score == pytest.approx(0.0)

    def test_score_three_decimals(self):
        """Score: 0.333 → score≈0.333."""
        assert self._grade_with_response("Score: 0.333\n").score == pytest.approx(0.333, abs=1e-3)

    def test_score_case_insensitive_uppercase(self):
        """SCORE: 0.75 (uppercase) → score=0.75."""
        assert self._grade_with_response("SCORE: 0.75\n").score == pytest.approx(0.75)

    def test_score_case_insensitive_mixed(self):
        """sCoRe: 0.6 → score=0.6."""
        assert self._grade_with_response("sCoRe: 0.6\n").score == pytest.approx(0.6)

    def test_score_clamped_above_1(self):
        """Score: 1.5 is clamped to 1.0."""
        assert self._grade_with_response("Score: 1.5\n").score == pytest.approx(1.0)

    def test_score_clamped_below_0(self):
        """Score: -0.1 is clamped to 0.0."""
        assert self._grade_with_response("Score: -0.1\n").score == pytest.approx(0.0)

    def test_score_extracted_from_middle_of_response(self):
        """Score line in the middle of a multi-line response is found."""
        response = "Analysis: The article covers the topic well.\nScore: 0.77\nOverall good."
        assert self._grade_with_response(response).score == pytest.approx(0.77)

    def test_score_at_end_of_long_response(self):
        """Score line at the very end of a long response is correctly extracted."""
        response = ("Long feedback sentence. " * 50) + "\nScore: 0.62"
        assert self._grade_with_response(response).score == pytest.approx(0.62)

    def test_first_score_line_wins_when_multiple(self):
        """When multiple 'Score: X' lines appear, the first match is used."""
        response = "Score: 0.90\nMore detail.\nScore: 0.20"
        assert self._grade_with_response(response).score == pytest.approx(0.90)

    def test_score_regex_module_level_constant_is_compiled(self):
        """_SCORE_RE is a compiled regex accessible at module level."""
        assert hasattr(_SCORE_RE, "search")
        assert _SCORE_RE.search("Score: 0.8") is not None
        assert _SCORE_RE.search("No score here") is None


# ===========================================================================
# 6. Output text extraction (holdout principle)
# ===========================================================================


class TestOutputTextExtraction:
    """Text extraction from output dict follows the documented priority order.

    Priority (when output_field is None):
      1. output["output"]
      2. output["result"]
      3. output["article"]
      4. output["text"]
      5. output["content"]
      6. output["final"] sub-dict → JSON-serialised
      7. Full JSON of the output dict
    """

    def _captured_prompt(self, output: dict, output_field=None) -> str:
        """Return the prompt string from the captured TaskSpec."""
        _, task = _capture_task_spec_from_grade(output, "rubric", "model", output_field)
        return task.payload["prompt"] if task else ""

    def test_explicit_output_field_used_first(self):
        """When output_field is given, that key is used regardless of others."""
        output = {"output": "wrong", "article": "also-wrong", "body": "correct-body"}
        prompt = self._captured_prompt(output, output_field="body")
        assert "correct-body" in prompt

    def test_explicit_output_field_missing_key_gives_empty(self):
        """output_field that doesn't exist → empty string (no other fields used)."""
        output = {"article": "article text"}
        prompt = self._captured_prompt(output, output_field="nonexistent")
        assert "article text" not in prompt

    def test_output_key_wins_over_result(self):
        """'output' key takes priority over 'result'."""
        output = {"output": "output-text", "result": "result-text"}
        prompt = self._captured_prompt(output)
        assert "output-text" in prompt
        assert "result-text" not in prompt

    def test_result_key_wins_over_article(self):
        """'result' takes priority over 'article' when 'output' absent."""
        output = {"result": "result-text", "article": "article-text"}
        prompt = self._captured_prompt(output)
        assert "result-text" in prompt
        assert "article-text" not in prompt

    def test_article_key_wins_over_text(self):
        """'article' takes priority over 'text'."""
        output = {"article": "article-text", "text": "text-value"}
        prompt = self._captured_prompt(output)
        assert "article-text" in prompt
        assert "text-value" not in prompt

    def test_text_key_wins_over_content(self):
        """'text' takes priority over 'content'."""
        output = {"text": "text-val", "content": "content-val"}
        prompt = self._captured_prompt(output)
        assert "text-val" in prompt
        assert "content-val" not in prompt

    def test_content_key_used_when_highest_available(self):
        """'content' key is used when no higher-priority key exists."""
        output = {"content": "content-text", "other": "ignored"}
        prompt = self._captured_prompt(output)
        assert "content-text" in prompt

    def test_final_sub_dict_serialised_as_json(self):
        """'final' sub-dict is JSON-serialised when no main text key present."""
        output = {"final": {"title": "AI Research", "body": "Article body."}}
        prompt = self._captured_prompt(output)
        assert "AI Research" in prompt

    def test_full_json_fallback_for_unknown_keys(self):
        """When no known text key is found, full output dict is JSON-serialised."""
        output = {"mystery_key": "some value", "count": 42}
        prompt = self._captured_prompt(output)
        assert "mystery_key" in prompt or "some value" in prompt

    def test_empty_output_dict_still_grades(self):
        """Empty output dict doesn't raise — grade returns a GradeResult."""
        result, _ = _capture_task_spec_from_grade({}, "rubric", "model")
        assert result.grader_type == "llm_judge"


# ===========================================================================
# 7. Holdout principle — metadata must not reach the judge
# ===========================================================================


class TestHoldoutPrinciple:
    """Only article text + rubric appear in the prompt sent to the judge."""

    def _captured_prompt(self, output: dict, rubric: str = "Rate it.") -> str:
        _, task = _capture_task_spec_from_grade(output, rubric, "model")
        return task.payload["prompt"] if task else ""

    def test_article_text_appears_in_prompt(self):
        """The article text is present in the executor prompt."""
        article = "Quantum computing harnesses superposition."
        prompt = self._captured_prompt({"article": article})
        assert article in prompt

    def test_rubric_text_appears_in_prompt(self):
        """The rubric text is present in the executor prompt."""
        rubric = "Evaluate technical accuracy between 0.0 and 1.0."
        prompt = self._captured_prompt({"article": "text"}, rubric)
        assert rubric in prompt

    def test_scenario_id_not_in_prompt(self):
        """scenario_id metadata does NOT leak into the judge prompt."""
        output = {"article": "Some article.", "scenario_id": "secret-scenario-999"}
        prompt = self._captured_prompt(output)
        assert "secret-scenario-999" not in prompt

    def test_threshold_not_in_prompt(self):
        """threshold value does NOT leak into the judge prompt."""
        output = {"article": "Article.", "threshold": 0.95}
        prompt = self._captured_prompt(output)
        assert "0.95" not in prompt

    def test_pipeline_name_not_in_prompt(self):
        """Pipeline config info does NOT leak into the judge prompt."""
        output = {"article": "Article.", "pipeline": "ultra-secret-pipeline-v2"}
        prompt = self._captured_prompt(output)
        assert "ultra-secret-pipeline-v2" not in prompt

    def test_tags_not_in_prompt(self):
        """Tags metadata does NOT leak into the judge prompt."""
        output = {"article": "Article.", "tags": ["adversarial", "edge-case"]}
        prompt = self._captured_prompt(output)
        assert "adversarial" not in prompt


# ===========================================================================
# 8. TaskSpec construction
# ===========================================================================


class TestTaskSpecConstruction:
    """The TaskSpec passed to executor.execute() has the correct structure."""

    def test_payload_has_prompt_key(self):
        """TaskSpec.payload contains 'prompt' key."""
        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric", "model")
        assert "prompt" in task.payload

    def test_payload_has_system_key(self):
        """TaskSpec.payload contains 'system' key."""
        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric", "model")
        assert "system" in task.payload

    def test_payload_system_matches_module_constant(self):
        """TaskSpec.payload['system'] is the module-level _JUDGE_SYSTEM_PROMPT."""
        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric", "model")
        assert task.payload["system"] == _JUDGE_SYSTEM_PROMPT

    def test_judge_system_prompt_contains_score_instruction(self):
        """_JUDGE_SYSTEM_PROMPT instructs the model to produce 'Score: X.X'."""
        assert "Score:" in _JUDGE_SYSTEM_PROMPT

    def test_task_type_is_analysis(self):
        """TaskSpec.type is TaskType.ANALYSIS."""
        from orchestration_engine.schemas import TaskType

        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric", "model")
        assert task.type == TaskType.ANALYSIS

    def test_created_by_is_llm_judge_grader(self):
        """TaskSpec.created_by == 'llm_judge_grader'."""
        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric", "model")
        assert task.created_by == "llm_judge_grader"

    def test_prompt_contains_rubric(self):
        """The prompt field contains the rubric text."""
        rubric = "Evaluate factual accuracy."
        _, task = _capture_task_spec_from_grade({"article": "x"}, rubric, "model")
        assert rubric in task.payload["prompt"]

    def test_prompt_contains_article(self):
        """The prompt field contains the article text."""
        article = "The Mars rover discovered water ice."
        _, task = _capture_task_spec_from_grade({"article": article}, "rubric", "model")
        assert article in task.payload["prompt"]

    def test_prompt_has_rubric_section_header(self):
        """The prompt includes a 'Rubric' section header."""
        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric text", "model")
        assert "Rubric" in task.payload["prompt"]

    def test_prompt_has_article_section_header(self):
        """The prompt includes an 'Article' section header."""
        _, task = _capture_task_spec_from_grade({"article": "article text"}, "rubric", "model")
        assert "Article" in task.payload["prompt"]


# ===========================================================================
# 9. Model tier mapping
# ===========================================================================


class TestModelTierMapping:
    """judge_model string → ModelTier in TaskSpec.preferred_model."""

    def _preferred_model(self, judge_model: str):
        _, task = _capture_task_spec_from_grade({"article": "text"}, "rubric", judge_model)
        return task.preferred_model

    def test_haiku_variant_maps_to_haiku_tier(self):
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("claude-haiku-4-5-20241022") == ModelTier.HAIKU

    def test_sonnet_variant_maps_to_sonnet_tier(self):
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("claude-sonnet-4-6") == ModelTier.SONNET

    def test_opus_variant_maps_to_opus_tier(self):
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("claude-opus-4-6") == ModelTier.OPUS

    def test_haiku_substring_anywhere_in_name(self):
        """Any model name containing 'haiku' → HAIKU."""
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("some-haiku-variant") == ModelTier.HAIKU

    def test_sonnet_substring_anywhere_in_name(self):
        """Any model name containing 'sonnet' → SONNET."""
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("my-custom-sonnet-model") == ModelTier.SONNET

    def test_opus_substring_anywhere_in_name(self):
        """Any model name containing 'opus' → OPUS."""
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("anthropic/opus-style-model") == ModelTier.OPUS

    def test_unknown_model_defaults_to_haiku(self):
        """Unrecognised model string → ModelTier.HAIKU (safe fallback)."""
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("gpt-4o") == ModelTier.HAIKU

    def test_empty_model_string_defaults_to_haiku(self):
        """Empty model string → ModelTier.HAIKU."""
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("") == ModelTier.HAIKU

    def test_opus_checked_before_sonnet(self):
        """Model name containing both 'opus' and 'sonnet' → OPUS (opus checked first)."""
        from orchestration_engine.schemas import ModelTier

        assert self._preferred_model("claude-opus-sonnet-hybrid") == ModelTier.OPUS


# ===========================================================================
# 10. Executor result data formats
# ===========================================================================


class TestExecutorResultFormats:
    """_grade_with_executor handles various shapes of TaskResult.result."""

    def test_standard_dict_result_with_text_key(self):
        """Standard dict result['text'] is parsed correctly."""
        executor = _make_executor(response_text="Score: 0.77\nGood.")
        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade({"article": "text"}, "rubric", "model")
        assert result.score == pytest.approx(0.77)

    def test_dict_result_missing_text_key_returns_zero(self):
        """Dict result without 'text' key → treated as empty → score=0.0."""
        task_result = _make_task_result()
        task_result.result = {"output": "Score: 0.80"}  # 'text' key absent
        executor = MagicMock()
        executor.execute.return_value = task_result
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")
        assert result.score == pytest.approx(0.0)

    def test_result_none_returns_zero(self):
        """TaskResult.result is None → empty response → score=0.0."""
        task_result = _make_task_result()
        task_result.result = None
        executor = MagicMock()
        executor.execute.return_value = task_result
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")
        assert result.score == pytest.approx(0.0)

    def test_result_plain_string_used_directly(self):
        """TaskResult.result is a plain string → used as response text."""
        from orchestration_engine.schemas import TaskState

        task_result = MagicMock()
        task_result.state = TaskState.SUCCESS
        task_result.result = "Score: 0.65\nString result."
        task_result.errors = []
        executor = MagicMock()
        executor.execute.return_value = task_result
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")
        assert result.score == pytest.approx(0.65)

    def test_result_text_empty_string_in_dict(self):
        """TaskResult.result['text'] == '' → empty response → score=0.0."""
        executor = _make_executor(response_text="")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")
        assert result.score == pytest.approx(0.0)

    def test_result_text_none_in_dict_returns_zero(self):
        """TaskResult.result['text'] is None → empty response → score=0.0."""
        task_result = _make_task_result()
        task_result.result = {"text": None}
        executor = MagicMock()
        executor.execute.return_value = task_result
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade({"article": "text"}, "rubric", "model")
        assert result.score == pytest.approx(0.0)


# ===========================================================================
# 11. API-key path — backward compatibility
# ===========================================================================


class TestApiKeyPathBackwardCompatibility:
    """_grade_with_api_key: existing urllib behaviour is unchanged."""

    def _mock_response(self, response_text: str):
        body = json.dumps({"content": [{"text": response_text}]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_api_key_path_parses_score(self):
        """api_key path: response with 'Score: 0.78' → score=0.78."""
        grader = LLMJudgeGrader(api_key="sk-test")
        with patch("urllib.request.urlopen", return_value=self._mock_response("Score: 0.78\nGood.")):
            result = grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")
        assert result.score == pytest.approx(0.78)
        assert result.grader_type == "llm_judge"

    def test_api_key_path_no_score_defaults_zero(self):
        """api_key path: response without Score line → score=0.0."""
        grader = LLMJudgeGrader(api_key="sk-test")
        with patch("urllib.request.urlopen", return_value=self._mock_response("Interesting article.")):
            result = grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")
        assert result.score == pytest.approx(0.0)

    def test_api_key_http_429_handled(self):
        """HTTP 429 from API → score=0.0 with '429' in details."""
        import urllib.error

        grader = LLMJudgeGrader(api_key="sk-test")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "https://api.anthropic.com/v1/messages",
                429, "Too Many Requests", None, BytesIO(b"rate limit"),
            ),
        ):
            result = grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")
        assert result.score == pytest.approx(0.0)
        assert "429" in result.details

    def test_api_key_http_401_handled(self):
        """HTTP 401 → score=0.0, passed=False."""
        import urllib.error

        grader = LLMJudgeGrader(api_key="sk-invalid")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "https://api.anthropic.com/v1/messages",
                401, "Unauthorized", None, BytesIO(b"invalid key"),
            ),
        ):
            result = grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_api_key_network_error_handled(self):
        """Network error → score=0.0 with error in details."""
        grader = LLMJudgeGrader(api_key="sk-test")
        with patch("urllib.request.urlopen", side_effect=OSError("Network unreachable")):
            result = grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")
        assert result.score == pytest.approx(0.0)
        assert result.grader_type == "llm_judge"

    def test_api_key_request_sent_to_correct_url(self):
        """urllib call hits api.anthropic.com/v1/messages."""
        grader = LLMJudgeGrader(api_key="sk-test")
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return self._mock_response("Score: 0.90\nExcellent.")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")

        assert len(captured) == 1
        assert "api.anthropic.com" in captured[0].full_url

    def test_api_key_header_set_correctly(self):
        """x-api-key header is set to the stored api_key value."""
        grader = LLMJudgeGrader(api_key="sk-my-special-key")
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return self._mock_response("Score: 0.80\nOK.")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")

        headers_lower = {k.lower(): v for k, v in captured[0].headers.items()}
        assert headers_lower.get("x-api-key") == "sk-my-special-key"

    def test_api_key_executor_attribute_is_none(self):
        """When api_key is used (no executor), grader.executor is None."""
        grader = LLMJudgeGrader(api_key="sk-test")
        assert grader.executor is None


# ===========================================================================
# 12. No-key fallback
# ===========================================================================


class TestNoKeyFallback:
    """When no api_key, no executor, and ORCH_DRY_RUN != '1'."""

    def test_grade_returns_graceful_error_result(self):
        """GradeResult indicates 'No API key configured'."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ORCH_DRY_RUN")
        }
        with patch.dict("os.environ", env, clear=True):
            grader = LLMJudgeGrader()

        result = grader.grade({"article": "text"}, "rubric", "model")

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert "No API key" in result.details
        assert result.grader_type == "llm_judge"

    def test_grade_does_not_raise(self):
        """No-key path must NOT raise — it returns a GradeResult."""
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ORCH_DRY_RUN")
        }
        with patch.dict("os.environ", env, clear=True):
            grader = LLMJudgeGrader()

        # Should not raise any exception
        result = grader.grade({}, "rubric", "model")
        assert isinstance(result, GradeResult)

    def test_grade_signature_unchanged(self):
        """grade(output, rubric, judge_model, output_field=None) signature is unchanged."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            grader = LLMJudgeGrader()

        # All four params should work without error
        result = grader.grade(
            output={},
            rubric="rubric text",
            judge_model="model-name",
            output_field=None,
        )
        assert isinstance(result, GradeResult)


# ===========================================================================
# 13. ScenarioRunner — executor forwarding
# ===========================================================================


class TestScenarioRunnerExecutorForwarding:
    """ScenarioRunner accepts and forwards executor to LLMJudgeGrader."""

    def test_runner_accepts_executor_parameter(self, tmp_path: Path):
        """ScenarioRunner(executor=...) constructs without error."""
        executor = _make_executor("Score: 0.85\nGood.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)
        assert runner._llm_grader.executor is executor

    def test_runner_without_executor_has_none(self, tmp_path: Path):
        """ScenarioRunner() without executor → grader.executor is None (backward compat)."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        assert runner._llm_grader.executor is None

    def test_runner_forwards_executor_to_grader(self, tmp_path: Path):
        """The same executor object is stored in the grader."""
        executor = _make_executor()
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)
        assert runner._llm_grader.executor is executor

    def test_runner_routes_llm_judge_through_executor(self, tmp_path: Path):
        """run_scenario dispatches llm_judge criteria through executor, not urllib."""
        executor = _make_executor("Score: 0.88\nWell written.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "executor-routing-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate article quality. Score: [0.0-1.0]",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = runner.run_scenario(scenario, {"article": "A well-crafted article."})

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result.criterion_results[0].grade.score == pytest.approx(0.88)

    def test_runner_dry_run_overrides_executor(self, tmp_path: Path):
        """ORCH_DRY_RUN=1 takes priority even when executor is provided."""
        executor = _make_executor("Score: 0.99\nPerfect.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "dry-run-override-test",
            "acceptance": [
                {
                    "id": "judge",
                    "type": "llm_judge",
                    "rubric": "Rate quality.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, {"article": "Some text."})

        executor.execute.assert_not_called()
        assert result.criterion_results[0].grade.score == pytest.approx(0.8)

    def test_runner_executor_error_doesnt_crash_run_scenario(self, tmp_path: Path):
        """Executor failure in llm_judge criterion returns a failed grade, not an exception."""
        executor = _make_executor(side_effect=RuntimeError("connection refused"))
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "executor-error-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate quality.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        # Should NOT raise
        result = runner.run_scenario(scenario, {"article": "text"})

        assert result.criterion_results[0].grade.score == pytest.approx(0.0)
        assert result.criterion_results[0].grade.passed is False

    def test_runner_executor_mixed_criteria_only_llm_uses_executor(self, tmp_path: Path):
        """Executor is used only for llm_judge criteria; assertion grader is unaffected."""
        executor = _make_executor("Score: 0.80\nGood.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "mixed-criteria-test",
            "acceptance": [
                {
                    "id": "not_empty",
                    "type": "assertion",
                    "check": "len(output.get('article', '')) > 0",
                    "weight": 0,
                },
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate quality.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                },
            ],
            "scoring": {"pass_threshold": 0.0},
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = runner.run_scenario(scenario, {"article": "Real article text."})

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()

        # Gate (assertion) should pass, llm_judge should use executor
        assert result.gates_passed is True
        assert result.criterion_results[1].grade.score == pytest.approx(0.80)


# ===========================================================================
# 14. Grade method signature contract
# ===========================================================================


class TestGradeMethodSignatureContract:
    """grade() public interface must be unchanged (Issue #171 constraint 6)."""

    def test_grade_accepts_output_dict(self):
        """grade() accepts output as the first positional argument."""
        executor = _make_executor("Score: 0.75\nGood.")
        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade({"article": "text"}, "rubric", "claude-haiku-4-5")
        assert isinstance(result, GradeResult)

    def test_grade_accepts_rubric_as_second_arg(self):
        """grade() accepts rubric as second positional argument."""
        executor = _make_executor("Score: 0.75\nGood.")
        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade({}, "my rubric text", "claude-haiku-4-5")
        assert isinstance(result, GradeResult)

    def test_grade_accepts_judge_model_as_third_arg(self):
        """grade() accepts judge_model as third positional argument."""
        executor = _make_executor("Score: 0.75\nGood.")
        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade({}, "rubric", "claude-opus-4-6")
        assert isinstance(result, GradeResult)

    def test_grade_accepts_output_field_kwarg(self):
        """grade() accepts optional output_field keyword argument."""
        executor = _make_executor("Score: 0.75\nGood.")
        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade(
            output={"body": "content"},
            rubric="rubric",
            judge_model="model",
            output_field="body",
        )
        assert isinstance(result, GradeResult)

    def test_grade_always_returns_grade_result(self):
        """grade() always returns a GradeResult regardless of routing path."""
        # Executor path
        executor = _make_executor("Score: 0.80\nGood.")
        grader_executor = LLMJudgeGrader(executor=executor)
        assert isinstance(grader_executor.grade({}, "r", "m"), GradeResult)

        # Dry-run path
        grader_dry = LLMJudgeGrader()
        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            assert isinstance(grader_dry.grade({}, "r", "m"), GradeResult)

        # No-key path
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            grader_nokey = LLMJudgeGrader()
        assert isinstance(grader_nokey.grade({}, "r", "m"), GradeResult)

    def test_grade_result_has_required_fields(self):
        """GradeResult always has passed, score, details, grader_type."""
        executor = _make_executor("Score: 0.72\nOK.")
        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade({"article": "text"}, "rubric", "model")

        assert hasattr(result, "passed")
        assert hasattr(result, "score")
        assert hasattr(result, "details")
        assert hasattr(result, "grader_type")
        assert result.grader_type == "llm_judge"
        assert 0.0 <= result.score <= 1.0


# ===========================================================================
# 15. WARNING-4 — api_key isolation when executor provided
# ===========================================================================


class TestApiKeyIsolationWhenExecutorProvided:
    """WARNING-4: executor presence prevents silent urllib fallback on failure."""

    def test_executor_failure_does_not_fall_back_to_urllib(self):
        """When executor fails, grade() does NOT fall back to urllib even if
        ANTHROPIC_API_KEY is set in the environment."""
        executor = _make_executor(side_effect=RuntimeError("executor down"))

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-would-work"}):
            grader = LLMJudgeGrader(executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = grader.grade({"article": "text"}, "rubric", "model")

        # urllib must never be called — executor failure is final
        mock_urlopen.assert_not_called()
        assert result.score == pytest.approx(0.0)
        assert "RuntimeError" in result.details or "executor down" in result.details

    def test_executor_non_success_state_does_not_fall_back_to_urllib(self):
        """Executor FAILED state does NOT trigger urllib fallback."""
        executor = _make_executor(success=False)

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-would-work"}):
            grader = LLMJudgeGrader(executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = grader.grade({"article": "text"}, "rubric", "model")

        mock_urlopen.assert_not_called()
        assert result.score == pytest.approx(0.0)

    def test_api_key_attr_is_none_when_executor_given(self):
        """grader.api_key is None when executor is provided (invariant)."""
        executor = _make_executor()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-key"}):
            grader = LLMJudgeGrader(api_key="sk-explicit", executor=executor)
        assert grader.api_key is None

    def test_grader_executor_attr_is_set(self):
        """grader.executor is the exact object passed in (identity check)."""
        executor = _make_executor()
        grader = LLMJudgeGrader(executor=executor)
        assert grader.executor is executor
