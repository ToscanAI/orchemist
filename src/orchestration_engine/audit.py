"""Adversarial audit phase — post-pipeline second-opinion judge (Issue #388 / 4.1.4).

This module implements an independent re-review step that runs *after* the
main coding pipeline completes.  It submits the same code artefacts to a
**different** model (or an adversarial variant of the same model) and
cross-references the audit findings against the original reviewer's issue list
to surface things the reviewer missed.

Key types
---------
:class:`AuditIssue`
    A single issue the auditor found.  Adds ``missed_by_reviewer`` on top of
    the :class:`~review_parser.ReviewIssue` shape.

:class:`AuditResult`
    Structured output of one audit run.  Carries ``caught_issues``,
    ``reviewer_accuracy_score``, ``false_approval``, both verdicts, and
    bookkeeping metadata.

:class:`AuditPhase`
    Orchestrator.  Accepts an *executor* (object with ``.execute()`` method),
    builds the adversarial prompt, invokes the LLM, parses the response,
    cross-references findings with description substring matching, and returns
    an :class:`AuditResult`.  :meth:`AuditPhase.run` **never raises** — all
    exceptions are caught and a safe APPROVE stub is returned on failure.

Reviewer accuracy score
-----------------------
Simple count-based metric::

    caught_count = len([i for i in caught_issues if not i.missed_by_reviewer])
    reviewer_accuracy_score = caught_count / total_issues   # 0.0 when all missed
    # 1.0 when no issues found or reviewer caught everything

False approval detection
------------------------
``false_approval = True`` when:
* The original reviewer's verdict was ``"APPROVE"``
* The auditor found at least one BLOCKER or MAJOR issue that was missed by
  the reviewer (``missed_by_reviewer=True``)

MINOR and NITPICK misses do not trigger false approval — they are considered
tolerable gaps.

Cross-referencing (description substring matching)
--------------------------------------------------
An audit issue is considered **NOT missed** by the reviewer when the original
``issues_found`` list contains at least one entry whose ``description`` is a
substring of (or contains) the auditor's issue description (case-insensitive).

Executor protocol
-----------------
:class:`AuditPhase` accepts an optional *executor* object.  When present, the
executor's ``.execute(prompt)`` method is called.  The return value is coerced
to ``str`` via:

1. If the return value is a ``str``, use it directly.
2. If it has a ``.text`` attribute, use ``str(result.text)``.
3. Otherwise, ``str(result)`` is used.

When no executor is provided (stub mode), :meth:`AuditPhase.run` returns a
clean APPROVE result with no issues and ``reviewer_accuracy_score = 1.0``.

Usage example::

    from orchestration_engine.audit import AuditPhase

    phase = AuditPhase(
        model="claude-opus-4-6",
        executor=my_executor,
    )
    result = phase.run(
        run_id="run-abc123",
        review_outcome={
            "verdict": "APPROVE",
            "issues_found": [{"description": "missing null check", ...}],
        },
        code_diff="+ def foo(): pass",
    )
    print(result.false_approval)
    print(result.reviewer_accuracy_score)
    for issue in result.caught_issues:
        print(issue.missed_by_reviewer, issue.description)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .review_parser import parse_review_output

__all__ = [
    "AuditIssue",
    "AuditResult",
    "AuditPhase",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity thresholds for false-approval detection.
# BLOCKER and MAJOR missed issues with APPROVE verdict → false_approval=True.
# MINOR and NITPICK misses are tolerated.
# ---------------------------------------------------------------------------
_FALSE_APPROVAL_SEVERITIES = frozenset({"BLOCKER", "MAJOR"})

# ---------------------------------------------------------------------------
# Prompt template for the adversarial audit
# ---------------------------------------------------------------------------

_AUDIT_PROMPT_TEMPLATE = """\
You are an adversarial code auditor performing a second-opinion security and \
correctness review.  Assume the original reviewer may have missed issues.  \
Be thorough, sceptical, and security-focused.

Original reviewer verdict: {original_verdict}
Original reviewer found {original_issue_count} issue(s):
{original_issues_summary}

---CODE DIFF UNDER REVIEW---
{code_diff}
---END CODE DIFF---

Perform a fresh, independent review.  Look especially for issues the original \
reviewer may have missed — security vulnerabilities, logic errors, edge cases, \
and correctness problems.

Respond in EXACTLY this format:
Line 1: APPROVE or REQUEST_CHANGES
Subsequent lines (if any): [SEVERITY][category] description
  where SEVERITY is one of: BLOCKER, MAJOR, MINOR, NITPICK
  and category is one of: security, correctness, style, logic, performance

Do not add prose explanations outside this format.
"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AuditIssue:
    """A single issue found by the adversarial auditor.

    Mirrors :class:`~review_parser.ReviewIssue` but adds the
    :attr:`missed_by_reviewer` flag, which is ``True`` when the original
    reviewer did not flag a substantially similar issue.

    Attributes:
        severity:           ``"BLOCKER"``, ``"MAJOR"``, ``"MINOR"``, or
                            ``"NITPICK"``.
        category:           Short label (e.g. ``"security"``,
                            ``"correctness"``).
        description:        Human-readable description of the issue.
        missed_by_reviewer: ``True`` if the original reviewer did not
                            surface a substantially similar issue.
        raw:                The raw line from the audit LLM output.
    """

    severity: str
    category: str
    description: str
    missed_by_reviewer: bool
    raw: str


@dataclass
class AuditResult:
    """Structured result of one adversarial audit run.

    Attributes:
        audit_id:               UUID uniquely identifying this audit run.
        run_id:                 The pipeline run being audited.
        audit_model:            Name/tier of the model used for auditing.
        original_verdict:       Verdict from the original reviewer
                                (``"APPROVE"``, ``"REQUEST_CHANGES"``, or
                                ``None``).
        audit_verdict:          Verdict from the auditor (``"APPROVE"``,
                                ``"REQUEST_CHANGES"``, or ``None``).
        caught_issues:          All issues the auditor found.
        reviewer_accuracy_score: Float in ``[0, 1]`` — fraction of auditor
                                 issues that the original reviewer also caught.
                                 Clamped to ``[0, 1]`` in
                                 :meth:`__post_init__`.  ``1.0`` when no
                                 issues were found.
        false_approval:         ``True`` when the original verdict was
                                ``"APPROVE"`` but the auditor found BLOCKER or
                                MAJOR issues the reviewer missed.
        created_at:             ISO-8601 UTC timestamp (auto-populated).
    """

    audit_id: str
    run_id: str
    audit_model: str
    original_verdict: Optional[str]
    audit_verdict: Optional[str]
    caught_issues: List[AuditIssue]
    reviewer_accuracy_score: float
    false_approval: bool = False
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        # Clamp score to [0, 1]
        self.reviewer_accuracy_score = max(
            0.0, min(1.0, self.reviewer_accuracy_score)
        )
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()

    @property
    def missed_issues(self) -> List[AuditIssue]:
        """Issues the auditor found that the original reviewer missed."""
        return [i for i in self.caught_issues if i.missed_by_reviewer]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON / DB storage."""
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
    """Orchestrates the adversarial audit of a completed pipeline run.

    Instantiate with an optional *executor* and a *model* label.  Call
    :meth:`run` with the pipeline ``run_id``, the original reviewer's outcome
    dict, and an optional code diff string.

    When no executor is provided, :meth:`run` operates in **stub mode**:
    it returns a clean APPROVE result without calling any LLM.

    :meth:`run` **never raises**.  All exceptions are caught and a safe
    fallback APPROVE result is returned.

    Args:
        model:    Human-readable model name or tier for the auditor (recorded
                  in the result; does not control which model the executor
                  uses — that is the executor's concern).
                  Defaults to ``"audit-model"``.
        executor: Optional executor object with a ``.execute(prompt: str)``
                  method.  When ``None``, stub mode is used.

    Example::

        phase = AuditPhase(model="claude-opus-4-6", executor=my_exec)
        result = phase.run(
            run_id="run-123",
            review_outcome={"verdict": "APPROVE", "issues_found": []},
            code_diff="+ import os",
        )
    """

    def __init__(
        self,
        model: str = "audit-model",
        executor: Optional[Any] = None,
    ) -> None:
        #: Model label embedded in every :class:`AuditResult`.
        self.model: str = model
        self._executor: Optional[Any] = executor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        run_id: str,
        review_outcome: Optional[Dict[str, Any]] = None,
        code_diff: str = "",
    ) -> AuditResult:
        """Execute the adversarial audit and return a structured result.

        This method **never raises**.  If any step fails the returned
        :class:`AuditResult` has ``audit_verdict="APPROVE"``, an empty
        ``caught_issues`` list, and ``reviewer_accuracy_score=1.0``.

        Args:
            run_id:         The pipeline run ID being audited.
            review_outcome: Dict from the original review phase.  Expected
                            keys: ``"verdict"`` (str or None),
                            ``"issues_found"`` (list of dicts, each with at
                            least a ``"description"`` key).  Missing keys are
                            treated as empty/None.
            code_diff:      Optional code diff (or full file) to include in
                            the audit prompt.  Empty string is valid.

        Returns:
            :class:`AuditResult` populated with findings.
        """
        audit_id = str(uuid.uuid4())
        outcome = review_outcome or {}
        original_verdict = outcome.get("verdict")
        original_issues: List[Dict[str, Any]] = outcome.get("issues_found") or []

        try:
            # ── Stub mode: no executor ─────────────────────────────────
            if self._executor is None:
                return self._stub_result(
                    audit_id=audit_id,
                    run_id=run_id,
                    original_verdict=original_verdict,
                )

            # ── 1. Build prompt ────────────────────────────────────────
            prompt = self._build_prompt(
                code_diff=code_diff,
                original_verdict=original_verdict,
                original_issues=original_issues,
            )

            # ── 2. Call executor ───────────────────────────────────────
            raw_response = self._invoke_executor(prompt)

            # ── 3. Parse response ──────────────────────────────────────
            review_result = parse_review_output(raw_response)

            # ── 4. Cross-reference → AuditIssue list ──────────────────
            audit_issues = self._cross_reference_issues(
                review_result.issues, original_issues
            )

            # ── 5. Compute reviewer_accuracy_score ─────────────────────
            accuracy_score = self._compute_accuracy_score(audit_issues)

            # ── 6. Detect false approval ───────────────────────────────
            false_approval = self._detect_false_approval(
                original_verdict=original_verdict,
                audit_issues=audit_issues,
            )

            return AuditResult(
                audit_id=audit_id,
                run_id=run_id,
                audit_model=self.model,
                original_verdict=original_verdict,
                audit_verdict=review_result.verdict,
                caught_issues=audit_issues,
                reviewer_accuracy_score=accuracy_score,
                false_approval=false_approval,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "AuditPhase.run failed for run_id=%r: %s",
                run_id,
                exc,
            )
            return self._stub_result(
                audit_id=audit_id,
                run_id=run_id,
                original_verdict=original_verdict,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _stub_result(
        self,
        audit_id: str,
        run_id: str,
        original_verdict: Optional[str],
    ) -> AuditResult:
        """Return a safe stub APPROVE result (used in stub mode or on error)."""
        return AuditResult(
            audit_id=audit_id,
            run_id=run_id,
            audit_model=self.model,
            original_verdict=original_verdict,
            audit_verdict="APPROVE",
            caught_issues=[],
            reviewer_accuracy_score=1.0,
            false_approval=False,
        )

    def _build_prompt(
        self,
        code_diff: str,
        original_verdict: Optional[str],
        original_issues: List[Dict[str, Any]],
    ) -> str:
        """Build the adversarial audit prompt string."""
        verdict_str = original_verdict or "UNKNOWN"
        issue_count = len(original_issues)

        if original_issues:
            issues_summary = "\n".join(
                f"  - [{i.get('severity', '?')}][{i.get('category', '?')}] "
                f"{i.get('description', '')}"
                for i in original_issues
            )
        else:
            issues_summary = "  (none)"

        return _AUDIT_PROMPT_TEMPLATE.format(
            original_verdict=verdict_str,
            original_issue_count=issue_count,
            original_issues_summary=issues_summary,
            code_diff=code_diff or "(no diff provided)",
        )

    def _invoke_executor(self, prompt: str) -> str:
        """Call the executor's ``.execute()`` method and coerce result to str.

        Coercion order:
        1. Already a ``str`` → use directly.
        2. Has a ``.text`` attribute → use ``str(result.text)``.
        3. Fallback: ``str(result)``.
        """
        result = self._executor.execute(prompt)
        if isinstance(result, str):
            return result
        if hasattr(result, "text"):
            try:
                return str(result.text)
            except Exception:
                pass
        try:
            return str(result)
        except Exception:
            return ""

    def _cross_reference_issues(
        self,
        parsed_issues: list,  # List[ReviewIssue]
        original_issues: List[Dict[str, Any]],
    ) -> List[AuditIssue]:
        """Convert parsed ReviewIssue list to AuditIssue list with missed flags.

        An audit issue is considered **not missed** by the reviewer when the
        original reviewer's ``issues_found`` contains at least one issue whose
        ``description`` is a substring of (or contains as a substring) the
        auditor's issue description (case-insensitive).

        Args:
            parsed_issues:   List of :class:`~review_parser.ReviewIssue`.
            original_issues: Dicts from the original reviewer's
                             ``"issues_found"`` list.

        Returns:
            List of :class:`AuditIssue`.
        """
        # Collect lower-case descriptions from the original reviewer
        original_descs: List[str] = [
            str(oi.get("description", "")).lower().strip()
            for oi in original_issues
        ]

        result: List[AuditIssue] = []
        for issue in parsed_issues:
            severity_str = (
                issue.severity.value
                if hasattr(issue.severity, "value")
                else str(issue.severity)
            )
            audit_desc_lower = issue.description.lower().strip()

            # Check description substring overlap with any original issue
            missed = not any(
                orig_desc and (
                    orig_desc in audit_desc_lower
                    or audit_desc_lower in orig_desc
                )
                for orig_desc in original_descs
            )

            result.append(
                AuditIssue(
                    severity=severity_str,
                    category=issue.category,
                    description=issue.description,
                    missed_by_reviewer=missed,
                    raw=issue.raw,
                )
            )
        return result

    def _compute_accuracy_score(self, audit_issues: List[AuditIssue]) -> float:
        """Compute simple count-based reviewer accuracy score in ``[0, 1]``.

        ``score = caught_count / total_count``

        Returns ``1.0`` when no issues were found (nothing to miss).

        Args:
            audit_issues: All issues the auditor found (after cross-reference).

        Returns:
            Float in ``[0, 1]``.
        """
        total = len(audit_issues)
        if total == 0:
            return 1.0
        caught = sum(1 for i in audit_issues if not i.missed_by_reviewer)
        return caught / total

    def _detect_false_approval(
        self,
        original_verdict: Optional[str],
        audit_issues: List[AuditIssue],
    ) -> bool:
        """Detect a false approval: APPROVE verdict but BLOCKER/MAJOR missed.

        A false approval is flagged when:
        * The original reviewer said ``"APPROVE"``
        * The auditor found at least one BLOCKER or MAJOR issue that was
          missed by the reviewer (``missed_by_reviewer=True``)

        MINOR and NITPICK misses do not constitute a false approval.

        Args:
            original_verdict: The original reviewer's verdict string.
            audit_issues:     All :class:`AuditIssue` objects (post
                              cross-reference).

        Returns:
            ``True`` iff a false approval is detected.
        """
        if original_verdict != "APPROVE":
            return False
        return any(
            i.missed_by_reviewer
            and i.severity.upper() in _FALSE_APPROVAL_SEVERITIES
            for i in audit_issues
        )
