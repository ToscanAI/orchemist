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
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "Severity",
    "ReviewIssue",
    "ReviewResult",
    "ReviewOutcome",
    "parse_review_output",
    "extract_verdict",
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

# Valid verdict tokens in explicit priority order — REQUEST_CHANGES first because
# it is the safer default when a single line contains both tokens as substrings.
# A tuple (not a frozenset) avoids non-deterministic iteration across Python
# invocations (hash randomization), so precedence is always well-defined.
_VALID_VERDICTS: tuple[str, ...] = ("REQUEST_CHANGES", "APPROVE")

# Matches the opening (or closing) fence of a fenced code block.
_CODE_BLOCK_RE = re.compile(r"^```")

# Lowercase prefixes that indicate a verdict token is being cited in the
# *negative* (e.g. "would not APPROVE", "don't REQUEST_CHANGES").
# When any of these appear immediately before the verdict token (in the
# lowercased line), the line is skipped to avoid false positives.
_NEGATIVE_PREFIXES: tuple[str, ...] = (
    "not ",
    "don't ",
    "do not ",
    "would not ",
    "wouldn't ",
    "no need for ",
    "rather than ",
    "instead of ",
    "avoid ",
    "without ",
    "isn't ",
    "is not ",
)


def _has_negative_prefix(prefix_region: str) -> bool:
    """Return True if *prefix_region* ends with a negative qualifier.

    Used to detect lines like "would not APPROVE" or "don't REQUEST_CHANGES"
    that cite a verdict in the negative rather than asserting it.

    Args:
        prefix_region: The lowercased portion of the line *before* the verdict
                       token (may include trailing spaces).

    Returns:
        ``True`` if the region ends with any entry in ``_NEGATIVE_PREFIXES``.
    """
    return any(
        prefix_region.endswith(neg) or prefix_region.endswith(neg.rstrip())
        for neg in _NEGATIVE_PREFIXES
    )


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


@dataclass
class ReviewOutcome:
    """Persistent record of a single review phase execution.

    Attributes:
        review_id:      Unique identifier for this outcome record (UUID).
        run_id:         The pipeline run that produced this review.
        phase_id:       The phase within the run (e.g. ``"review"``).
        reviewer_model: The model tier/name used for reviewing.
        verdict:        ``"APPROVE"``, ``"REQUEST_CHANGES"``, or ``None``.
        issues_found:   List of dicts (serialised ReviewIssue fields):
                        each with ``severity``, ``category``, ``description``,
                        and ``raw`` keys.
        fix_verified:   ``True`` when a subsequent fix phase ran and passed,
                        ``False`` otherwise.  Defaults to ``False`` at
                        record-creation time; updated post-merge.
        created_at:     ISO-8601 timestamp string (UTC) of when this record
                        was created.  Defaults to the current UTC time when
                        not explicitly supplied.
    """

    review_id: str
    run_id: str
    phase_id: str
    reviewer_model: Optional[str]
    verdict: Optional[str]
    issues_found: List[Dict[str, Any]]
    fix_verified: bool = False
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the outcome to a plain dict suitable for DB insertion.

        ``issues_found`` is serialised to a JSON string via :func:`json.dumps`
        by the DB layer; this method returns the Python list representation.

        Returns:
            Dict with all fields.  ``issues_found`` is a Python list (not
            JSON-encoded) — the DB insert method handles JSON encoding.
        """
        return {
            "review_id": self.review_id,
            "run_id": self.run_id,
            "phase_id": self.phase_id,
            "reviewer_model": self.reviewer_model,
            "verdict": self.verdict,
            "issues_found": self.issues_found,
            "fix_verified": self.fix_verified,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Cascade helpers (private)
# ---------------------------------------------------------------------------


def _smart_full_text_scan(lines: list[str]) -> Optional[str]:
    """Layer 1 — Scan *all* lines for a verdict token.

    Skips:
    - Lines inside fenced code blocks (toggled by ````` `` ` `` ``` `` fence lines).
    - Lines where the verdict token is preceded by a negative prefix (to avoid
      false positives like "would not APPROVE" or "don't REQUEST_CHANGES").

    Returns the first unambiguous verdict found, or ``None`` if none is found.
    """
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        if _CODE_BLOCK_RE.match(stripped):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if not stripped:
            continue
        lower = stripped.lower()
        for verdict in _VALID_VERDICTS:
            verdict_lower = verdict.lower()
            # Use word-boundary regex so that "disapprove", "unapproved",
            # "preapprove", etc. do NOT falsely match as APPROVE.
            m = re.search(r"\b" + re.escape(verdict_lower) + r"\b", lower)
            if m is None:
                continue
            # Check if any negative prefix appears immediately before the verdict
            # token (allowing spaces between prefix and token).
            prefix_region = lower[: m.start()]
            if _has_negative_prefix(prefix_region):
                logger.debug(
                    "review_parser: skipping negative-context verdict %r on line %r",
                    verdict,
                    stripped[:80],
                )
                continue
            return verdict
    return None


def _tail_weighted_scan(lines: list[str]) -> Optional[str]:
    """Layer 2 — Scan the *last 20 non-blank, non-code-block lines* for a verdict.

    LLM reviewers often conclude with the verdict in the final paragraph.
    This layer re-scans only the tail of the document (skipping fenced code
    blocks and negative-prefix lines, matching the behaviour of Layer 1) to
    surface those late verdicts quickly.

    Returns the first verdict found scanning from the *end*, or ``None``.
    """
    # Pre-filter: remove lines that live inside fenced code blocks so that a
    # verdict token appearing only inside a ``` block is not falsely returned.
    filtered: list[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        if _CODE_BLOCK_RE.match(stripped):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped:
            filtered.append(stripped)

    tail = filtered[-20:] if len(filtered) > 20 else filtered
    # Scan tail in reverse (bottom-up) to find the most-final verdict first.
    for stripped in reversed(tail):
        lower = stripped.lower()
        for verdict in _VALID_VERDICTS:
            verdict_lower = verdict.lower()
            # Use word-boundary regex to prevent "disapprove" / "unapproved"
            # from matching as APPROVE (same fix as Layer 1).
            m = re.search(r"\b" + re.escape(verdict_lower) + r"\b", lower)
            if m is None:
                continue
            prefix_region = lower[: m.start()]
            if _has_negative_prefix(prefix_region):
                continue
            return verdict
    return None


def _haiku_extraction(text: str) -> Optional[str]:  # noqa: ARG001
    """Layer 3 — Stub for future LLM-assisted verdict extraction.

    In a future iteration this would call a lightweight model (Haiku) with
    a zero-shot prompt: "What is the verdict in the following review?  Answer
    with exactly one word: APPROVE or REQUEST_CHANGES."

    Currently returns ``None`` (no-op) because making synchronous LLM calls
    from inside the parser would introduce latency and external dependencies
    inappropriate for a unit-testable utility module.

    Args:
        text: Full review text (unused in this stub).

    Returns:
        ``None`` — reserved for future implementation.
    """
    return None


# ---------------------------------------------------------------------------
# Public cascade API
# ---------------------------------------------------------------------------


def extract_verdict(text: str) -> Optional[str]:
    """Extract a verdict from review text using a 4-layer cascade strategy.

    Each layer tries progressively harder extraction strategies before
    falling back to ``None``.  The cascade stops as soon as any layer
    succeeds.

    **Layer 0 — Quick first-5-lines scan (existing behaviour)**
        Replicates the legacy ``parse_review_output`` heuristic: scan up to
        5 non-blank lines for an exact verdict word.  Fast and reliable for
        well-formed reviews.

    **Layer 1 — Smart full-text scan** (:func:`_smart_full_text_scan`)
        Scans *all* lines, skipping fenced code blocks and lines where the
        verdict is preceded by a negative prefix (e.g. "would not APPROVE").
        Catches verdicts buried deep in prose.

    **Layer 2 — Tail-weighted scan** (:func:`_tail_weighted_scan`)
        Re-scans the final 20 non-blank lines from the bottom up.  Catches
        "In conclusion… APPROVE" patterns near the end of long reviews.

    **Layer 3 — Haiku extraction** (:func:`_haiku_extraction`)
        Reserved for future LLM-assisted extraction.  Currently a no-op stub.

    **Layer 4 — Fallback**
        Returns ``None``.  A missing verdict triggers human review in the
        auto-merge path.

    Args:
        text: Raw LLM output from the review phase.

    Returns:
        ``"APPROVE"``, ``"REQUEST_CHANGES"``, or ``None``.
    """
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            text = ""

    lines = text.splitlines()

    # ── Layer 0: Quick first-5-lines scan (legacy behaviour) ─────────────────
    _scan_limit = 5
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        first_word = stripped.split()[0].upper()
        if first_word in _VALID_VERDICTS:
            return first_word
        _scan_limit -= 1
        if _scan_limit <= 0:
            break

    # ── Layer 1: Smart full-text scan ─────────────────────────────────────────
    verdict = _smart_full_text_scan(lines)
    if verdict is not None:
        logger.debug("review_parser: extract_verdict found %r via Layer 1 (full-text scan).", verdict)
        return verdict

    # ── Layer 2: Tail-weighted scan ───────────────────────────────────────────
    verdict = _tail_weighted_scan(lines)
    if verdict is not None:
        logger.debug("review_parser: extract_verdict found %r via Layer 2 (tail scan).", verdict)
        return verdict

    # ── Layer 3: Haiku extraction (stub) ──────────────────────────────────────
    verdict = _haiku_extraction(text)
    if verdict is not None:
        logger.debug("review_parser: extract_verdict found %r via Layer 3 (haiku).", verdict)
        return verdict

    # ── Layer 4: Fallback ─────────────────────────────────────────────────────
    logger.warning(
        "review_parser: extract_verdict could not determine verdict after all layers; "
        "text preview: %r",
        text[:120],
    )
    return None


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

    # ── Extract verdict via cascading strategy ────────────────────────────────
    # Delegates to extract_verdict() which implements a 4-layer cascade:
    # Layer 0 (quick 5-line scan) → Layer 1 (full-text) → Layer 2 (tail) →
    # Layer 3 (haiku stub) → None.  This is strictly more capable than the
    # previous inline 5-line scan while remaining backward-compatible.
    verdict: str | None = extract_verdict(text)

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
