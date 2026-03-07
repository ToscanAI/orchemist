"""
review_parser.py — Structured parser for code review phase output.

Parses the output produced by the ``review`` phase of ``coding-pipeline-v1.yaml``
into typed, queryable Python objects.  The review phase emits:

1. A **verdict** on line 1 — either ``APPROVE`` or ``REQUEST_CHANGES``.
2. Zero or more **tagged issue lines** in the form::

       [SEVERITY][category] description of the issue

   Where ``SEVERITY`` is one of :class:`Severity`'s members
   (``BLOCKER``, ``MAJOR``, ``MINOR``, ``NITPICK``) and ``category`` is a
   short free-form label (e.g. ``security``, ``correctness``, ``style``).

Graceful-degradation guarantees (mirrors :mod:`output_parser`):

* Empty or non-string input → ``ReviewResult`` with ``verdict=None``, no issues.
* Missing verdict line → ``ReviewResult`` with ``verdict=None``.
* Malformed tag lines → silently skipped; warnings emitted via ``logging``.
* Unknown severity tokens → silently skipped with a warning.
* Never raises; always returns a :class:`ReviewResult`.

Usage::

    from orchestration_engine.review_parser import parse_review_output

    result = parse_review_output(raw_text)
    if result.verdict == "APPROVE":
        ...
    for issue in result.issues:
        print(issue.severity, issue.category, issue.description)

"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Severity",
    "ReviewIssue",
    "ReviewResult",
    "parse_review_output",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled regular expressions
# ---------------------------------------------------------------------------

# Matches a tagged issue line:   [SEVERITY][category] description
# Group 1 → severity token (upper-cased before Enum lookup)
# Group 2 → category label (stripped)
# Group 3 → description (stripped)
_ISSUE_RE = re.compile(
    r"^\s*\[([A-Za-z]+)\]\[([^\]]+)\]\s+(.+)$"
)

# Valid verdict tokens (exactly as they must appear on line 1)
_VALID_VERDICTS = frozenset({"APPROVE", "REQUEST_CHANGES"})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class Severity(Enum):
    """Issue severity levels, ordered from most to least critical.

    Values match the tag tokens used in the review output format::

        [BLOCKER][security] ...
        [MAJOR][correctness] ...
        [MINOR][style] ...
        [NITPICK][style] ...
    """

    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    NITPICK = "NITPICK"


@dataclass
class ReviewIssue:
    """A single parsed issue from a review.

    Attributes:
        severity:    Structured :class:`Severity` enum value.
        category:    Short label extracted from the tag (e.g. ``"security"``).
        description: Human-readable description of the issue.
        raw:         The original unmodified line from the review text.
    """

    severity: Severity
    category: str
    description: str
    raw: str


@dataclass
class ReviewResult:
    """Structured result of parsing a code review phase output.

    Attributes:
        verdict:    ``"APPROVE"``, ``"REQUEST_CHANGES"``, or ``None`` when the
                    review text is empty / malformed and no verdict line was found.
        issues:     List of :class:`ReviewIssue` objects extracted from tagged
                    lines.  Empty list when the review is clean or unparseable.
        raw_text:   The original input string, preserved byte-for-byte.
        has_issues: Computed in :meth:`__post_init__`; ``True`` iff
                    ``len(issues) > 0``.  Always consistent with ``bool(issues)``.
    """

    verdict: str | None
    issues: list[ReviewIssue]
    raw_text: str
    has_issues: bool = field(init=False)

    def __post_init__(self) -> None:
        self.has_issues = len(self.issues) > 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_review_output(text: str) -> ReviewResult:
    """Parse structured code review output into a :class:`ReviewResult`.

    Expects *text* to conform to the format mandated by
    ``coding-pipeline-v1.yaml``'s review prompt:

    * **Line 1** must be exactly ``APPROVE`` or ``REQUEST_CHANGES`` (stripped
      of surrounding whitespace).
    * **Subsequent lines** may contain zero or more tagged issue lines of the
      form ``[SEVERITY][category] description``.  Lines that do not match this
      pattern are silently ignored (e.g. blank lines, prose commentary that the
      LLM adds despite the prompt instruction).

    Issue scanning covers **all** lines (including line 1): if a tag line
    appears before or instead of the verdict, it is still captured as an
    issue.  The verdict is extracted independently from the first non-blank
    line only.

    Graceful-degradation behaviour:

    * **Non-string input** is coerced via ``str()``; on failure ``""`` is used.
    * **Empty string / no lines** → ``verdict=None``, ``issues=[]``.
    * **Unrecognised first line** → ``verdict=None``; issue parsing still
      proceeds on all lines so that partial results are surfaced.
    * **Malformed tag line** → skipped; ``DEBUG`` logged.
    * **Unknown severity token** (not in :class:`Severity`) → skipped;
      ``WARNING`` logged.

    Args:
        text: Raw LLM output from the review phase.

    Returns:
        :class:`ReviewResult` populated with the extracted verdict and issues.

    Examples::

        result = parse_review_output(
            "REQUEST_CHANGES\\n"
            "[BLOCKER][security] SQL injection in db.py:42\\n"
            "[MINOR][style] Missing docstring on _helper()\\n"
        )
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.issues) == 2
        assert result.issues[0].severity == Severity.BLOCKER

        clean = parse_review_output("APPROVE\\n")
        assert clean.verdict == "APPROVE"
        assert not clean.has_issues
    """
    # ── Coerce non-string input gracefully ───────────────────────────────────
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            text = ""

    lines = text.splitlines()

    # ── Extract verdict from first non-blank line ─────────────────────────────
    verdict: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped:
            if stripped in _VALID_VERDICTS:
                verdict = stripped
            else:
                logger.warning(
                    "review_parser: unrecognised verdict on first non-blank line: %r",
                    stripped,
                )
            break  # verdict position is always the first non-blank line only

    # ── Parse tagged issue lines (all lines scanned) ──────────────────────────
    issues: list[ReviewIssue] = []

    for line in lines:
        m = _ISSUE_RE.match(line)
        if not m:
            logger.debug(
                "review_parser: skipping non-tag line: %r", line.strip()[:80]
            )
            continue

        severity_token = m.group(1).upper()
        category = m.group(2).strip()
        description = m.group(3).strip()

        try:
            severity = Severity(severity_token)
        except ValueError:
            logger.warning(
                "review_parser: unknown severity token %r on line %r — skipping",
                severity_token,
                line.strip()[:80],
            )
            continue

        issues.append(
            ReviewIssue(
                severity=severity,
                category=category,
                description=description,
                raw=line,
            )
        )

    return ReviewResult(verdict=verdict, issues=issues, raw_text=text)
