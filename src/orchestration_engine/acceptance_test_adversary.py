"""
acceptance_test_adversary.py — Adversarial acceptance test reviewer.

Provides parsing for the ``acceptance_test_adversary`` phase of the coding
pipeline.  The adversary phase reviews acceptance tests produced by the
``acceptance_test`` phase before implementation begins, checking for coverage
gaps, trivial satisfaction, implementation leakage, and specificity defects.

Public API (all exported via ``__all__``)
-----------------------------------------
* :class:`AcceptanceTestAdversaryFinding` — single finding from an adversary review
* :class:`AcceptanceTestAdversaryVerdict` — full structured verdict (APPROVE or REQUEST_CHANGES)
* :func:`parse_adversary_output` — parse raw LLM text into an
  :class:`AcceptanceTestAdversaryVerdict`

No third-party dependencies — stdlib only.
No ``compute_reward()`` / ``persist_reward()`` — deferred.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .text_utils import FINDING_RE_EMPTY_OK
from .verdict_parser import extract_verdict as _extract_verdict

__all__ = [
    "AcceptanceTestAdversaryFinding",
    "AcceptanceTestAdversaryVerdict",
    "parse_adversary_output",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid finding categories (case-insensitive on input, stored lowercase)
# ---------------------------------------------------------------------------

_VALID_CATEGORIES: frozenset = frozenset(
    ["coverage", "trivial_satisfaction", "leakage", "specificity"]
)

# Shared empty-tolerant finding matcher (see text_utils.FINDING_RE_EMPTY_OK).
# Aliased to a module-local name for the parse loop below; kept identical to
# the shared object so the two single-bracket parsers do not diverge.
# Group 1 → category (letters and underscores)
# Group 2 → description (anything after the optional whitespace — may be empty)
_FINDING_RE = FINDING_RE_EMPTY_OK

# Primary verdict scan — first-match-wins, structured VERDICT: lines only
# Mirrors verdict_parser._PASS1_RE but restricted to approve/request_changes
# and using forward (first-match) rather than reverse (last-match) semantics.
_VERDICT_LINE_RE = re.compile(
    r"^\s*verdict\s*:\s*[\*_`#>~\-]*\s*(approve|request_changes)\s*[\*_`#>~\-]*\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scan_first_verdict(text: str) -> Optional[str]:
    """Forward scan for the first ``VERDICT: <keyword>`` line.

    Returns the lowercase verdict keyword, or ``None`` if no matching line
    is found.  First match wins — important for adversary output where
    revision rounds may contain multiple VERDICT tokens.
    """
    for line in text.splitlines():
        m = _VERDICT_LINE_RE.match(line)
        if m:
            keyword = m.group(1).lower()
            if keyword in {"approve", "request_changes"}:
                return keyword
    return None


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceTestAdversaryFinding:
    """A single finding produced by the acceptance test adversarial reviewer.

    Attributes:
        category:    One of the four recognised weakness categories:
                     ``coverage``, ``trivial_satisfaction``, ``leakage``,
                     ``specificity``.  Always stored in lowercase.
        description: Human-readable description of the weakness.  Preserved
                     verbatim from the adversary output (text after the
                     ``[category] `` prefix).
    """

    category: str
    description: str


@dataclass
class AcceptanceTestAdversaryVerdict:
    """Structured result of parsing acceptance test adversary output.

    Attributes:
        verdict:   ``"APPROVE"`` or ``"REQUEST_CHANGES"``.
        findings:  List of :class:`AcceptanceTestAdversaryFinding` objects.
                   Empty when the verdict is ``"APPROVE"`` and no finding lines
                   are present; populated independently of verdict when tagged
                   lines exist.
        raw_text:  The original, unmodified input string (or ``str(input)``
                   when input was coerced from a non-string type).  Preserved
                   for downstream traceability.
    """

    verdict: str
    findings: List[AcceptanceTestAdversaryFinding] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_adversary_output(text) -> AcceptanceTestAdversaryVerdict:
    """Parse raw adversary LLM output into a structured :class:`AcceptanceTestAdversaryVerdict`.

    Parsing algorithm
    -----------------
    1. **Coerce non-string input** — if *text* is not a string (e.g. ``None``),
       attempt ``str(text)``; on failure use ``""``.
    2. **Find the verdict** — forward scan for the first
       ``VERDICT: <keyword>`` line (first-match-wins).  Falls back to
       :func:`verdict_parser.extract_verdict` for markdown-heavy output where
       the primary scan finds nothing.
    3. **Parse findings** — all lines matching ``[category] description``
       (``_FINDING_RE``) are parsed as findings.  Category tokens are
       normalised to lowercase.  Lines that do not match the pattern are
       silently skipped.  Lines with unrecognised category tokens (including
       ``vague``, which is valid for ``spec_adversary`` but not here) are also
       silently skipped.  Findings are parsed **independently** of the verdict —
       an ``APPROVE`` verdict with tagged finding lines still populates
       ``findings``.
    4. **No verdict found** — defaults to ``REQUEST_CHANGES`` with a single
       explanatory finding.  This is the safe default: never assume approval
       from ambiguous output.

    Graceful degradation
    --------------------
    * Never raises an exception on any input.
    * Empty string input → ``REQUEST_CHANGES`` with one explanatory finding.
    * Non-string input → coerced via ``str()``; same safe-default behaviour.

    Args:
        text: Raw LLM output from the ``acceptance_test_adversary`` phase.

    Returns:
        :class:`AcceptanceTestAdversaryVerdict` populated from the parsed output.
    """
    # ── 1. Coerce non-string input ────────────────────────────────────────────
    raw_text: str
    if not isinstance(text, str):
        try:
            raw_text = str(text)
        except Exception:  # noqa: BLE001
            raw_text = ""
    else:
        raw_text = text

    lines = raw_text.splitlines()

    # ── 2. Verdict extraction — first-match-wins forward scan ─────────────────
    _parsed_verdict: Optional[str] = _scan_first_verdict(raw_text)

    # Fallback: shared verdict_parser handles markdown-decorated output and
    # other edge cases not covered by the primary structured scan.
    if _parsed_verdict is None:
        _parsed_verdict = _extract_verdict(
            text=raw_text, allowed_verdicts={"approve", "request_changes"}
        )

    verdict: Optional[str] = _parsed_verdict.upper() if _parsed_verdict else None

    # ── 3. Finding extraction (independent of verdict) ────────────────────────
    findings: List[AcceptanceTestAdversaryFinding] = []
    for line in lines:
        m = _FINDING_RE.match(line)
        if not m:
            logger.debug(
                "acceptance_test_adversary: skipping non-finding line: %r",
                line.strip()[:80],
            )
            continue

        category_token = m.group(1).lower()
        description = m.group(2).strip()

        if category_token not in _VALID_CATEGORIES:
            logger.debug(
                "acceptance_test_adversary: unknown category %r on line %r — skipping",
                category_token,
                line.strip()[:80],
            )
            continue

        findings.append(
            AcceptanceTestAdversaryFinding(
                category=category_token,
                description=description,
            )
        )

    # ── 4. Safe default: no verdict found → REQUEST_CHANGES + explanatory finding
    if verdict is None:
        logger.warning(
            "acceptance_test_adversary: no recognisable verdict found in output "
            "(first 120 chars): %r",
            raw_text[:120],
        )
        explanatory_finding = AcceptanceTestAdversaryFinding(
            category="coverage",
            description=(
                "Acceptance test adversary output contained no recognisable verdict "
                "(expected 'APPROVE' or 'REQUEST_CHANGES'). "
                "Defaulting to REQUEST_CHANGES for safety."
            ),
        )
        return AcceptanceTestAdversaryVerdict(
            verdict="REQUEST_CHANGES",
            findings=[explanatory_finding],
            raw_text=raw_text,
        )

    return AcceptanceTestAdversaryVerdict(
        verdict=verdict,
        findings=findings,
        raw_text=raw_text,
    )
