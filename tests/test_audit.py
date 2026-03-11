"""Tests for Issue #4.1.4 — AuditPhase, AuditResult, AuditIssue.

Covers:
- AuditIssue dataclass shape and fields
- AuditResult dataclass shape, __post_init__ clamping, to_dict
- AuditPhase instantiation (with and without executor)
- AuditPhase.run() with mocked executor
- Prompt building (adversarial format, original verdict/issues included)
- Cross-referencing issues (missed_by_reviewer flag)
- reviewer_accuracy_score calculation
- false_approval detection
- Stub mode (no executor → APPROVE with no issues)
- Executor error fallback → APPROVE
- Executor returning object with .text attribute
- Empty review outcome (no issues, no verdict)
- parse_review_output reuse (via real parser, no mock needed)
- Module exports via __init__.py
- DEFAULT_WEIGHTS includes adversarial_audit (renamed from audit_catch_rate in Issue #4.1.6)

All tests are independent — no shared mutable state, no real LLM calls.
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.audit import (
    AuditIssue,
    AuditPhase,
    AuditResult,
)
from orchestration_engine.confidence import DEFAULT_WEIGHTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_review_outcome(
    verdict: str | None = "APPROVE",
    issues: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a minimal review outcome dict."""
    return {
        "verdict": verdict,
        "issues_found": issues if issues is not None else [],
    }


def _make_issue_dict(
    severity: str = "MINOR",
    category: str = "correctness",
    description: str = "a test issue",
) -> Dict[str, Any]:
    return {"severity": severity, "category": category, "description": description}


def _executor_returning(text: str) -> MagicMock:
    """Build a mock executor whose execute() returns *text*."""
    mock = MagicMock()
    mock.execute.return_value = text
    mock.model = "test-model"
    return mock


# ===========================================================================
# TestAuditIssue — dataclass shape
# ===========================================================================


class TestAuditIssue:
    def test_fields_exist(self):
        names = {f.name for f in fields(AuditIssue)}
        assert "severity" in names
        assert "category" in names
        assert "description" in names
        assert "missed_by_reviewer" in names
        assert "raw" in names

    def test_field_count(self):
        assert len(fields(AuditIssue)) == 5

    def test_instantiation(self):
        issue = AuditIssue(
            severity="BLOCKER",
            category="security",
            description="SQL injection found",
            missed_by_reviewer=True,
            raw="[BLOCKER][security] SQL injection found",
        )
        assert issue.severity == "BLOCKER"
        assert issue.category == "security"
        assert issue.description == "SQL injection found"
        assert issue.missed_by_reviewer is True
        assert "BLOCKER" in issue.raw

    def test_missed_by_reviewer_false(self):
        issue = AuditIssue(
            severity="MINOR",
            category="style",
            description="missing docstring",
            missed_by_reviewer=False,
            raw="[MINOR][style] missing docstring",
        )
        assert issue.missed_by_reviewer is False


# ===========================================================================
# TestAuditResult — dataclass shape, __post_init__, to_dict
# ===========================================================================


class TestAuditResult:
    def test_fields_exist(self):
        names = {f.name for f in fields(AuditResult)}
        assert "audit_id" in names
        assert "run_id" in names
        assert "audit_model" in names
        assert "original_verdict" in names
        assert "audit_verdict" in names
        assert "caught_issues" in names
        assert "reviewer_accuracy_score" in names
        assert "false_approval" in names
        assert "created_at" in names

    def test_created_at_auto_populated(self):
        result = AuditResult(
            audit_id=str(uuid.uuid4()),
            run_id="run-001",
            audit_model="test-model",
            original_verdict="APPROVE",
            audit_verdict="REQUEST_CHANGES",
            caught_issues=[],
            reviewer_accuracy_score=0.5,
        )
        assert result.created_at is not None
        assert "T" in result.created_at  # ISO-8601 format

    def test_reviewer_accuracy_score_clamped_above_one(self):
        result = AuditResult(
            audit_id=str(uuid.uuid4()),
            run_id="run-001",
            audit_model="test-model",
            original_verdict=None,
            audit_verdict=None,
            caught_issues=[],
            reviewer_accuracy_score=1.5,
        )
        assert result.reviewer_accuracy_score == 1.0

    def test_reviewer_accuracy_score_clamped_below_zero(self):
        result = AuditResult(
            audit_id=str(uuid.uuid4()),
            run_id="run-001",
            audit_model="test-model",
            original_verdict=None,
            audit_verdict=None,
            caught_issues=[],
            reviewer_accuracy_score=-0.5,
        )
        assert result.reviewer_accuracy_score == 0.0

    def test_false_approval_defaults_to_false(self):
        result = AuditResult(
            audit_id=str(uuid.uuid4()),
            run_id="run-001",
            audit_model="test-model",
            original_verdict="APPROVE",
            audit_verdict=None,
            caught_issues=[],
            reviewer_accuracy_score=1.0,
        )
        assert result.false_approval is False

    def test_to_dict_keys(self):
        result = AuditResult(
            audit_id="aid-1",
            run_id="run-001",
            audit_model="test-model",
            original_verdict="APPROVE",
            audit_verdict="REQUEST_CHANGES",
            caught_issues=[
                AuditIssue(
                    severity="BLOCKER",
                    category="security",
                    description="injection",
                    missed_by_reviewer=True,
                    raw="[BLOCKER][security] injection",
                )
            ],
            reviewer_accuracy_score=0.0,
            false_approval=True,
        )
        d = result.to_dict()
        assert d["audit_id"] == "aid-1"
        assert d["run_id"] == "run-001"
        assert d["audit_model"] == "test-model"
        assert d["original_verdict"] == "APPROVE"
        assert d["audit_verdict"] == "REQUEST_CHANGES"
        assert d["reviewer_accuracy_score"] == 0.0
        assert d["false_approval"] is True
        assert isinstance(d["caught_issues"], list)
        assert len(d["caught_issues"]) == 1
        issue_dict = d["caught_issues"][0]
        assert issue_dict["severity"] == "BLOCKER"
        assert issue_dict["missed_by_reviewer"] is True

    def test_to_dict_empty_issues(self):
        result = AuditResult(
            audit_id="aid-2",
            run_id="run-002",
            audit_model="test-model",
            original_verdict=None,
            audit_verdict="APPROVE",
            caught_issues=[],
            reviewer_accuracy_score=1.0,
        )
        d = result.to_dict()
        assert d["caught_issues"] == []


# ===========================================================================
# TestAuditPhaseInit
# ===========================================================================


class TestAuditPhaseInit:
    def test_default_model(self):
        auditor = AuditPhase()
        assert auditor.model == "audit-model"

    def test_custom_model(self):
        auditor = AuditPhase(model="claude-opus-4-6")
        assert auditor.model == "claude-opus-4-6"

    def test_no_executor(self):
        auditor = AuditPhase()
        assert auditor._executor is None

    def test_with_executor(self):
        mock_exec = MagicMock()
        auditor = AuditPhase(executor=mock_exec)
        assert auditor._executor is mock_exec


# ===========================================================================
# TestAuditPhaseStubMode — no executor
# ===========================================================================


class TestAuditPhaseStubMode:
    def test_stub_returns_approve_verdict(self):
        auditor = AuditPhase()
        result = auditor.run(
            run_id="run-stub",
            review_outcome=_make_review_outcome(),
        )
        assert result.audit_verdict == "APPROVE"

    def test_stub_returns_empty_issues(self):
        auditor = AuditPhase()
        result = auditor.run(
            run_id="run-stub",
            review_outcome=_make_review_outcome(),
        )
        assert result.caught_issues == []

    def test_stub_accuracy_score_is_one(self):
        auditor = AuditPhase()
        result = auditor.run(
            run_id="run-stub",
            review_outcome=_make_review_outcome(),
        )
        assert result.reviewer_accuracy_score == 1.0

    def test_stub_false_approval_is_false(self):
        auditor = AuditPhase()
        result = auditor.run(
            run_id="run-stub",
            review_outcome=_make_review_outcome("APPROVE"),
        )
        assert result.false_approval is False

    def test_stub_audit_id_is_uuid(self):
        auditor = AuditPhase()
        result = auditor.run(
            run_id="run-stub",
            review_outcome=_make_review_outcome(),
        )
        # Should not raise
        uuid.UUID(result.audit_id)

    def test_stub_run_id_propagated(self):
        auditor = AuditPhase()
        result = auditor.run(
            run_id="my-run-123",
            review_outcome=_make_review_outcome(),
        )
        assert result.run_id == "my-run-123"


# ===========================================================================
# TestAuditPhaseWithExecutor
# ===========================================================================


class TestAuditPhaseWithExecutor:
    def test_executor_called_once(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert mock_exec.execute.call_count == 1

    def test_executor_receives_prompt_with_code_diff(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(),
            code_diff="+ some_code()",
        )
        prompt = mock_exec.execute.call_args[0][0]
        assert "+ some_code()" in prompt

    def test_executor_receives_original_verdict_in_prompt(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(verdict="REQUEST_CHANGES"),
        )
        prompt = mock_exec.execute.call_args[0][0]
        assert "REQUEST_CHANGES" in prompt

    def test_executor_receives_original_issues_in_prompt(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        original_issues = [_make_issue_dict(description="original bug found")]
        auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(issues=original_issues),
        )
        prompt = mock_exec.execute.call_args[0][0]
        assert "original bug found" in prompt

    def test_approve_verdict_parsed(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert result.audit_verdict == "APPROVE"

    def test_request_changes_verdict_parsed(self):
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n[MINOR][style] missing docstring\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert result.audit_verdict == "REQUEST_CHANGES"

    def test_issues_parsed(self):
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] SQL injection in query\n"
            "[MINOR][style] missing docstring\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert len(result.caught_issues) == 2
        severities = {i.severity for i in result.caught_issues}
        assert "BLOCKER" in severities
        assert "MINOR" in severities

    def test_model_embedded_in_result(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec, model="claude-opus-4-6")
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert result.audit_model == "claude-opus-4-6"

    def test_executor_error_falls_back_to_approve(self):
        mock_exec = MagicMock()
        mock_exec.execute.side_effect = RuntimeError("connection refused")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert result.audit_verdict == "APPROVE"
        assert result.caught_issues == []

    def test_executor_returning_text_attribute(self):
        """Executor returns an object with a .text attribute."""
        mock_response = MagicMock()
        mock_response.text = "APPROVE\n[MINOR][style] minor style issue\n"
        mock_exec = MagicMock()
        mock_exec.execute.return_value = mock_response
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert len(result.caught_issues) == 1
        assert result.caught_issues[0].severity == "MINOR"


# ===========================================================================
# TestCrossReferencing — missed_by_reviewer flag
# ===========================================================================


class TestCrossReferencing:
    """Tests for _cross_reference_issues logic."""

    def test_issue_missed_by_reviewer(self):
        """Auditor finds issue not in reviewer's list → missed_by_reviewer=True."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] SQL injection\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                verdict="APPROVE",
                issues=[],  # reviewer found nothing
            ),
        )
        assert len(result.caught_issues) == 1
        assert result.caught_issues[0].missed_by_reviewer is True

    def test_issue_not_missed_when_reviewer_also_flagged(self):
        """Auditor repeats an issue the reviewer already caught → missed=False."""
        original_desc = "SQL injection in database query"
        mock_exec = _executor_returning(
            f"REQUEST_CHANGES\n"
            f"[BLOCKER][security] {original_desc}\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                verdict="REQUEST_CHANGES",
                issues=[_make_issue_dict(description=original_desc)],
            ),
        )
        assert len(result.caught_issues) == 1
        # Description matches → should not be marked as missed
        assert result.caught_issues[0].missed_by_reviewer is False

    def test_partial_overlap_counts_as_caught(self):
        """Substring overlap → reviewer is credited with catching it."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[MAJOR][correctness] missing null check\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                issues=[_make_issue_dict(description="missing null check in _parse()")]
            ),
        )
        assert result.caught_issues[0].missed_by_reviewer is False

    def test_unrelated_issues_are_missed(self):
        """Reviewer flagged unrelated issue; auditor finds new one → missed=True."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] auth bypass vulnerability\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                issues=[_make_issue_dict(description="missing docstring")]
            ),
        )
        assert result.caught_issues[0].missed_by_reviewer is True

    def test_multiple_issues_mixed(self):
        """Some issues caught, some missed."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] SQL injection\n"
            "[MINOR][style] missing docstring\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                issues=[_make_issue_dict(description="missing docstring")]
            ),
        )
        assert len(result.caught_issues) == 2
        missed = [i for i in result.caught_issues if i.missed_by_reviewer]
        caught = [i for i in result.caught_issues if not i.missed_by_reviewer]
        assert len(missed) == 1  # SQL injection was missed
        assert len(caught) == 1  # docstring was caught


# ===========================================================================
# TestReviewerAccuracyScore
# ===========================================================================


class TestReviewerAccuracyScore:
    def test_no_issues_gives_perfect_score(self):
        """No audit issues → nothing to miss → score = 1.0."""
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(run_id="run-001", review_outcome=_make_review_outcome())
        assert result.reviewer_accuracy_score == 1.0

    def test_all_missed_gives_zero(self):
        """Reviewer missed all issues → score = 0.0."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] issue A\n"
            "[MAJOR][correctness] issue B\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(issues=[]),  # reviewer found nothing
        )
        assert result.reviewer_accuracy_score == 0.0

    def test_half_missed_gives_half(self):
        """Reviewer missed 1 of 2 issues → score = 0.5."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] new security issue\n"
            "[MINOR][style] missing docstring\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                issues=[_make_issue_dict(description="missing docstring")]
            ),
        )
        assert result.reviewer_accuracy_score == pytest.approx(0.5)

    def test_all_caught_gives_perfect_score(self):
        """Reviewer caught all audit issues → score = 1.0."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[MINOR][style] missing docstring\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                issues=[_make_issue_dict(description="missing docstring")]
            ),
        )
        assert result.reviewer_accuracy_score == 1.0


# ===========================================================================
# TestFalseApprovalDetection
# ===========================================================================


class TestFalseApprovalDetection:
    def test_false_approval_when_blocker_missed(self):
        """APPROVE + missed BLOCKER → false_approval=True."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] critical security flaw\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(verdict="APPROVE", issues=[]),
        )
        assert result.false_approval is True

    def test_false_approval_when_major_missed(self):
        """APPROVE + missed MAJOR → false_approval=True."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[MAJOR][correctness] logic error\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(verdict="APPROVE", issues=[]),
        )
        assert result.false_approval is True

    def test_no_false_approval_when_minor_only(self):
        """APPROVE + missed MINOR only → false_approval=False (minor is tolerated)."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[MINOR][style] minor style issue\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(verdict="APPROVE", issues=[]),
        )
        assert result.false_approval is False

    def test_no_false_approval_when_request_changes(self):
        """Reviewer said REQUEST_CHANGES → no false approval even if issues missed."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] critical flaw\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(verdict="REQUEST_CHANGES", issues=[]),
        )
        assert result.false_approval is False

    def test_no_false_approval_when_blocker_already_caught(self):
        """APPROVE + BLOCKER but reviewer already caught it → no false approval."""
        mock_exec = _executor_returning(
            "REQUEST_CHANGES\n"
            "[BLOCKER][security] SQL injection\n"
        )
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-001",
            review_outcome=_make_review_outcome(
                verdict="APPROVE",
                issues=[_make_issue_dict(severity="BLOCKER", description="SQL injection")],
            ),
        )
        assert result.false_approval is False


# ===========================================================================
# TestAuditResultFields — original_verdict and run_id propagation
# ===========================================================================


class TestAuditResultPropagation:
    def test_original_verdict_propagated(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-xyz",
            review_outcome=_make_review_outcome(verdict="REQUEST_CHANGES"),
        )
        assert result.original_verdict == "REQUEST_CHANGES"

    def test_original_verdict_none_propagated(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-xyz",
            review_outcome=_make_review_outcome(verdict=None),
        )
        assert result.original_verdict is None

    def test_run_id_propagated(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="unique-run-42",
            review_outcome=_make_review_outcome(),
        )
        assert result.run_id == "unique-run-42"

    def test_empty_issues_outcome(self):
        mock_exec = _executor_returning("APPROVE\n")
        auditor = AuditPhase(executor=mock_exec)
        result = auditor.run(
            run_id="run-empty",
            review_outcome={"verdict": None, "issues_found": None},
        )
        assert result.caught_issues == []


# ===========================================================================
# TestDefaultWeightsAuditCatchRate
# ===========================================================================


class TestDefaultWeightsAuditCatchRate:
    """Issue #4.1.6: audit_catch_rate renamed to adversarial_audit in DEFAULT_WEIGHTS."""

    def test_adversarial_audit_in_default_weights(self):
        assert "adversarial_audit" in DEFAULT_WEIGHTS

    def test_adversarial_audit_value(self):
        assert DEFAULT_WEIGHTS["adversarial_audit"] == pytest.approx(0.10)

    def test_audit_catch_rate_not_in_default_weights(self):
        """Old key must be absent — renamed to adversarial_audit in Issue #4.1.6."""
        assert "audit_catch_rate" not in DEFAULT_WEIGHTS

    def test_historical_calibration_in_default_weights(self):
        """Issue #4.1.6: historical_calibration weight entry added to DEFAULT_WEIGHTS."""
        assert "historical_calibration" in DEFAULT_WEIGHTS
        assert DEFAULT_WEIGHTS["historical_calibration"] == pytest.approx(0.05)

    def test_weights_sum_within_expected_range(self):
        # DEFAULT_WEIGHTS sum > 1.0: several signals are optional (only emitted
        # when their data is present), so _weighted_average renormalises over
        # present signals automatically.
        # Issue #528: acceptance_pass_rate (0.40) added → sum is ~1.35.
        # Issue #533: code_quality (0.20) added → sum is ~1.55.
        total = sum(DEFAULT_WEIGHTS.values())
        assert 1.0 <= total <= 1.8

    def test_all_expected_keys_present(self):
        expected_keys = {
            "acceptance_pass_rate",    # added in Issue #528 — PRIMARY signal
            "llm_judge",
            "test_pass_rate",
            "review_quality",
            "change_complexity",
            "review_catch_value",
            "adversarial_audit",       # renamed from audit_catch_rate in Issue #4.1.6
            "historical_calibration",  # added in Issue #4.1.6
        }
        assert expected_keys.issubset(set(DEFAULT_WEIGHTS.keys()))


# ===========================================================================
# TestModuleExports — imports via __init__.py
# ===========================================================================


class TestModuleExports:
    def test_import_from_package(self):
        from orchestration_engine import AuditPhase as AP
        from orchestration_engine import AuditResult as AR
        from orchestration_engine import AuditIssue as AI
        assert AP is AuditPhase
        assert AR is AuditResult
        assert AI is AuditIssue

    def test_all_exports_present(self):
        from orchestration_engine import __all__
        assert "AuditPhase" in __all__
        assert "AuditResult" in __all__
        assert "AuditIssue" in __all__

    def test_audit_module_no_third_party_imports(self):
        """audit.py must not import third-party libraries."""
        from pathlib import Path
        import orchestration_engine.audit as audit_mod
        source = Path(audit_mod.__file__).read_text()
        for lib in ["pydantic", "requests", "attrs", "numpy", "click"]:
            assert f"import {lib}" not in source, f"Unexpected third-party import: {lib}"
            assert f"from {lib}" not in source, f"Unexpected third-party import: {lib}"
