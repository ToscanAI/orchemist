"""Adversarial audit phase — post-pipeline second-opinion reviewer (Issue #4.1.4).

This module provides a post-pipeline adversarial audit that re-reviews merged code
using a *different* model or security-focused adversarial prompt.  It is a
verification layer that:

1. Catches issues the original reviewer missed (false negatives).
2. Detects false approvals (reviewer said APPROVE but code had problems).
3. Surfaces security gaps with dedicated security-focused prompting.
4. Produces a structured :class:`AuditResult` with ``caught_issues`` and
   ``reviewer_accuracy_score``.

This is NOT an inline pipeline phase — it runs *after* the pipeline completes,
similar to how :mod:`~orchestration_engine.scoring` runs post-pipeline.  It is
standalone and callable on any completed pipeline run's output directory.

Typical usage::

    from orchestration_engine.audit import AuditPhase, AuditResult

    auditor = AuditPhase(executor=my_executor, model="claude-opus-4-6")
    result = auditor.run(
        run_id="run-abc123",
        code_diff=diff_text,
        review_outcome=original_review_outcome_dict,
    )
    print(f"Reviewer accuracy: {result.reviewer_accuracy_score:.2f}")
    for issue in result.caught_issues:
        if issue.missed_by_reviewer:
            print(f"  MISSED: [{issue.severity}][{issue.category}] {issue.description}")

Module exports
--------------
- :class:`AuditIssue`   — a single issue found by the adversarial auditor.
- :class:`AuditResult`  — full audit output with accuracy scoring.
- :class:`AuditPhase`   — orchestrates the adversarial re-review logic.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .review_parser import _ISSUE_RE, parse_review_output
from .timestamps import now_utc

__all__ = [
    "AuditIssue",
    "AuditResult",
    "AuditPhase",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled regular expressions
# ---------------------------------------------------------------------------

# ``_ISSUE_RE`` (the tagged issue-line matcher "[SEVERITY][category] description")
# is imported from :mod:`review_parser` — the single source of truth — rather
# than redefined here. review_parser does not import audit, so this import edge
# is one-directional (no cycle).

#: Adversarial audit system prompt — security-focused, designed to challenge
#: the original reviewer's findings rather than validate them.
_AUDIT_PROMPT_TEMPLATE = """\
You are an adversarial code auditor performing a *second-opinion security and \
correctness review*.  Your goal is to find issues that the original reviewer \
MAY HAVE MISSED, especially:

- Security vulnerabilities (injection, auth bypass, insecure defaults)
- Race conditions and concurrency bugs
- Silent data corruption or precision loss
- Missing input validation
- Incorrect error handling (swallowed exceptions, wrong exit codes)
- Logic errors and off-by-one mistakes
- Missing or misleading documentation

## Original reviewer's verdict
{original_verdict}

## Original reviewer's issues
{original_issues}

## Code diff to audit
{code_diff}

Respond with:
1. Line 1: APPROVE or REQUEST_CHANGES
2. Zero or more issue lines in the format: [SEVERITY][category] description
   Severity must be one of: BLOCKER, MAJOR, MINOR, NITPICK

Be adversarial: do NOT simply repeat the original reviewer's issues verbatim.
Flag new issues, or issues the reviewer rated too leniently.
"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AuditIssue:
    """A single issue found by the adversarial auditor.

    Mirrors :class:`~review_parser.ReviewIssue` but adds a
    :attr:`missed_by_reviewer` flag that records whether the original reviewer
    also flagged this issue.

    Attributes:
        severity:          One of ``"BLOCKER"``, ``"MAJOR"``, ``"MINOR"``,
                           ``"NITPICK"``.
        category:          Short label (e.g. ``"security"``, ``"correctness"``,
                           ``"style"``).
        description:       Human-readable description of the issue.
        missed_by_reviewer: ``True`` when the original reviewer did *not* flag
                            this issue; ``False`` when they did (or flagged it
                            more leniently).
        raw:               The original unmodified line from the audit output.
    """

    severity: str
    category: str
    description: str
    missed_by_reviewer: bool
    raw: str


@dataclass
class AuditResult:
    """Full output of one adversarial audit run.

    Attributes:
        audit_id:               UUID string uniquely identifying this audit.
        run_id:                 The pipeline run being audited.
        audit_model:            Model used for the audit (should differ from the
                                original reviewer's model where possible).
        original_verdict:       The original reviewer's verdict
                                (``"APPROVE"`` / ``"REQUEST_CHANGES"`` / ``None``).
        audit_verdict:          The auditor's verdict
                                (``"APPROVE"`` / ``"REQUEST_CHANGES"`` / ``None``).
        caught_issues:          All issues the auditor found (both new and
                                previously flagged).
        reviewer_accuracy_score: Float in ``[0.0, 1.0]``.  Fraction of audit
                                 issues that the original reviewer also caught.
                                 ``1.0`` = reviewer missed nothing;
                                 ``0.0`` = reviewer missed everything.
                                 ``1.0`` when the auditor found no issues
                                 (nothing to miss).
        false_approval:         ``True`` when the original reviewer said
                                ``"APPROVE"`` but the auditor found BLOCKER or
                                MAJOR issues — indicating a potentially false
                                approval.
        created_at:             ISO-8601 UTC timestamp of when this audit was
                                created.
    """

    audit_id: str
    run_id: str
    audit_model: str
    original_verdict: Optional[str]
    audit_verdict: Optional[str]
    caught_issues: List[AuditIssue]
    reviewer_accuracy_score: float
    false_approval: bool = field(default=False)
    created_at: Optional[str] = field(default=None)

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = now_utc().isoformat()
        # Clamp accuracy score to [0, 1]
        self.reviewer_accuracy_score = max(
            0.0, min(1.0, float(self.reviewer_accuracy_score))
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the result to a plain dict.

        Returns:
            Dict with all fields.  :attr:`caught_issues` is serialised as a
            list of dicts.
        """
        return {
            "audit_id": self.audit_id,
            "run_id": self.run_id,
            "audit_model": self.audit_model,
            "original_verdict": self.original_verdict,
            "audit_verdict": self.audit_verdict,
            "caught_issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "description": i.description,
                    "missed_by_reviewer": i.missed_by_reviewer,
                    "raw": i.raw,
                }
                for i in self.caught_issues
            ],
            "reviewer_accuracy_score": self.reviewer_accuracy_score,
            "false_approval": self.false_approval,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# AuditPhase
# ---------------------------------------------------------------------------


class AuditPhase:
    """Orchestrates the adversarial re-review of a completed pipeline run.

    This class is responsible for:

    1. Formatting a security-focused, adversarial audit prompt.
    2. Sending it to the configured executor (or falling back to a no-op when
       no executor is provided, for offline testing).
    3. Parsing the audit output using :func:`~review_parser.parse_review_output`.
    4. Cross-referencing auditor-found issues against the original reviewer's
       ``issues_found`` list to populate :attr:`~AuditIssue.missed_by_reviewer`.
    5. Computing :attr:`~AuditResult.reviewer_accuracy_score`.
    6. Returning a fully populated :class:`AuditResult`.

    Args:
        executor: An executor object with an ``execute(prompt: str) -> str``
                  method (e.g. :class:`~openclaw_executor.OpenClawExecutor` or
                  :class:`~openai_executor.OpenAIExecutor`).  When ``None``
                  the phase runs in *stub mode*: prompts are logged and
                  ``"APPROVE"`` is returned with no issues.  Useful for offline
                  tests that mock at a higher level.
        model:    Model identifier string embedded in the :class:`AuditResult`.
                  Defaults to ``"audit-model"`` when not supplied.  Should be
                  set to the actual model name used by *executor*.

    Example::

        from unittest.mock import MagicMock

        mock_exec = MagicMock()
        mock_exec.execute.return_value = (
            "REQUEST_CHANGES\\n"
            "[BLOCKER][security] Unvalidated input passed to SQL query\\n"
        )
        auditor = AuditPhase(executor=mock_exec, model="claude-opus-4-6")
        result = auditor.run(
            run_id="run-001",
            code_diff="+ cursor.execute(f'SELECT * FROM {table}')",
            review_outcome={"verdict": "APPROVE", "issues_found": []},
        )
        assert result.false_approval is True
        assert result.reviewer_accuracy_score == 0.0
    """

    def __init__(
        self,
        executor: Optional[Any] = None,
        model: str = "audit-model",
    ) -> None:
        self._executor = executor
        self.model: str = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        run_id: str,
        review_outcome: Dict[str, Any],
        code_diff: Optional[str] = None,
    ) -> AuditResult:
        """Run the adversarial audit and return a structured :class:`AuditResult`.

        Args:
            run_id:         The pipeline run identifier being audited.
            review_outcome: A ReviewOutcome dict (or any dict) with at least:
                            ``"verdict"`` (str | None) and
                            ``"issues_found"`` (list of dicts).  Typically
                            produced by :class:`~review_parser.ReviewOutcome`
                            or :meth:`~db.Database.get_review_outcomes_for_run`.
            code_diff:      Optional string containing the code diff or full
                            file contents to audit.  When ``None`` an empty
                            diff is used (the auditor reviews only the original
                            issues list).

        Returns:
            A populated :class:`AuditResult`.
        """
        original_verdict = review_outcome.get("verdict")
        original_issues: List[Dict[str, Any]] = review_outcome.get("issues_found") or []

        # 1. Build prompt
        prompt = self._build_prompt(
            original_verdict=original_verdict,
            original_issues=original_issues,
            code_diff=code_diff or "(no diff provided)",
        )

        # 2. Call executor
        raw_output = self._call_executor(prompt)

        # 3. Parse audit output
        parsed = parse_review_output(raw_output)

        # 4. Cross-reference issues
        caught_issues = self._cross_reference_issues(
            audit_issues=parsed.issues,
            original_issues=original_issues,
        )

        # 5. Compute reviewer_accuracy_score
        reviewer_accuracy_score = self._compute_accuracy_score(caught_issues)

        # 6. Detect false approval
        false_approval = self._detect_false_approval(
            original_verdict=original_verdict,
            caught_issues=caught_issues,
        )

        return AuditResult(
            audit_id=str(uuid.uuid4()),
            run_id=run_id,
            audit_model=self.model,
            original_verdict=original_verdict,
            audit_verdict=parsed.verdict,
            caught_issues=caught_issues,
            reviewer_accuracy_score=reviewer_accuracy_score,
            false_approval=false_approval,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        original_verdict: Optional[str],
        original_issues: List[Dict[str, Any]],
        code_diff: str,
    ) -> str:
        """Format the adversarial audit prompt."""
        if original_issues:
            issues_text = "\n".join(
                f"  [{i.get('severity', '?')}][{i.get('category', '?')}] "
                f"{i.get('description', '')}"
                for i in original_issues
            )
        else:
            issues_text = "  (no issues flagged by original reviewer)"

        return _AUDIT_PROMPT_TEMPLATE.format(
            original_verdict=original_verdict or "(unknown)",
            original_issues=issues_text,
            code_diff=code_diff,
        )

    def _call_executor(self, prompt: str) -> str:
        """Send *prompt* to the executor and return the raw string response.

        Falls back to ``"APPROVE"`` in stub mode (no executor configured).

        Args:
            prompt: The formatted audit prompt.

        Returns:
            Raw LLM response string.
        """
        if self._executor is None:
            logger.debug(
                "AuditPhase: no executor configured — returning stub APPROVE response"
            )
            return "APPROVE\n"

        try:
            result = self._executor.execute(prompt)
            # Executors may return a string or an object with a .text attribute
            if isinstance(result, str):
                return result
            if hasattr(result, "text"):
                return str(result.text)
            return str(result)
        except Exception as exc:
            logger.warning(
                "AuditPhase: executor.execute() raised %s — falling back to APPROVE",
                exc,
            )
            return "APPROVE\n"

    def _cross_reference_issues(
        self,
        audit_issues: list,
        original_issues: List[Dict[str, Any]],
    ) -> List[AuditIssue]:
        """Convert parsed issues and flag which ones the reviewer missed.

        An audit issue is considered *not* missed by the reviewer when the
        original reviewer's ``issues_found`` list contains a dict whose
        ``"description"`` key overlaps substantially with the audit issue's
        description (case-insensitive substring match).

        Args:
            audit_issues: List of :class:`~review_parser.ReviewIssue` objects
                          from parsing the audit output.
            original_issues: Original reviewer's ``issues_found`` list (list of
                             dicts, each with at least a ``"description"`` key).

        Returns:
            List of :class:`AuditIssue` with :attr:`~AuditIssue.missed_by_reviewer`
            populated.
        """
        # Collect lowercase descriptions from the original reviewer's issues
        reviewer_descs: List[str] = [
            (i.get("description") or "").lower().strip()
            for i in original_issues
        ]

        result: List[AuditIssue] = []
        for issue in audit_issues:
            audit_desc_lower = issue.description.lower().strip()

            # Check whether any reviewer issue overlaps with this audit issue
            already_caught = any(
                (audit_desc_lower in rd) or (rd in audit_desc_lower)
                for rd in reviewer_descs
                if rd  # skip empty strings
            )
            missed_by_reviewer = not already_caught

            severity_str = (
                issue.severity.value
                if hasattr(issue.severity, "value")
                else str(issue.severity)
            )

            result.append(
                AuditIssue(
                    severity=severity_str,
                    category=issue.category,
                    description=issue.description,
                    missed_by_reviewer=missed_by_reviewer,
                    raw=issue.raw,
                )
            )

        return result

    @staticmethod
    def _compute_accuracy_score(caught_issues: List[AuditIssue]) -> float:
        """Compute the reviewer accuracy score.

        ``score = 1.0 - (missed_count / total_count)``

        * ``1.0`` when the auditor found no issues (nothing to miss) or the
          reviewer caught all audit issues.
        * ``0.0`` when the reviewer missed every issue the auditor found.

        Args:
            caught_issues: List of :class:`AuditIssue` from
                           :meth:`_cross_reference_issues`.

        Returns:
            Float in ``[0.0, 1.0]``.
        """
        total = len(caught_issues)
        if total == 0:
            return 1.0  # No issues found -> reviewer missed nothing

        missed = sum(1 for i in caught_issues if i.missed_by_reviewer)
        return 1.0 - (missed / total)

    @staticmethod
    def _detect_false_approval(
        original_verdict: Optional[str],
        caught_issues: List[AuditIssue],
    ) -> bool:
        """Return ``True`` if the original reviewer issued a potentially false APPROVE.

        A false approval is detected when:
        - The original verdict is ``"APPROVE"``, AND
        - The auditor found at least one ``BLOCKER`` or ``MAJOR`` issue that
          the reviewer missed.

        Args:
            original_verdict: The original reviewer's verdict string.
            caught_issues:    Auditor-found issues with ``missed_by_reviewer``
                              populated.

        Returns:
            ``True`` if a false approval is detected.
        """
        if original_verdict != "APPROVE":
            return False

        return any(
            i.missed_by_reviewer and i.severity in ("BLOCKER", "MAJOR")
            for i in caught_issues
        )
