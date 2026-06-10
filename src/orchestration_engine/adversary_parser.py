"""Generic adversary output parser (Issue #701).

Provides a config-driven parser that replaces per-phase adversary parser modules.
The parsing behaviour is fully controlled by :class:`AdversaryConfig`, making it
reusable across any pipeline phase that needs adversarial review.

Public API (all exported via ``__all__``)
-----------------------------------------
* :class:`AdversaryConfig`   — configuration for a single adversary phase
* :class:`AdversaryFinding`  — a single finding from an adversary review
* :class:`AdversaryVerdict`  — full structured verdict (APPROVE or REQUEST_CHANGES)
* :func:`parse_adversary_output` — parse raw LLM text into an :class:`AdversaryVerdict`

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .text_utils import FINDING_RE
from .verdict_parser import extract_verdict

__all__ = [
    "AdversaryConfig",
    "AdversaryFinding",
    "AdversaryVerdict",
    "parse_adversary_output",
]

logger = logging.getLogger(__name__)

# Tagged finding-line matcher ([category] description) is the canonical
# ``FINDING_RE`` imported from :mod:`text_utils` (single source of truth shared
# with spec_adversary). The separator is the canonical ``\s+`` (>=1 whitespace);
# the captured description (group 2) is still preserved verbatim (NOT stripped)
# at the call site below. For the single-space lines that adversary phases emit,
# this is byte-identical to the former literal-space pattern; the only delta is
# that a run of multiple leading separator whitespace is now treated wholly as
# the separator (extra leading spaces no longer leak into the description) — a
# path no test or real adversary output exercises.


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AdversaryConfig:
    """Configuration for a generic adversary parsing phase.

    Attributes:
        valid_categories:  Non-empty list of category tokens that the parser
                           accepts.  Finding lines with any other category are
                           silently skipped.
        fallback_category: Category used for the explanatory finding when no
                           verdict is detected.  When ``None`` (default), the
                           first entry of ``valid_categories`` is used.
        verdict_scan:      Scan order for Pass 1 verdict extraction.
                           ``"last"`` (default) = last VERDICT: line wins;
                           ``"first"`` = first VERDICT: line wins.
        reward_enabled:    Parsed but NOT acted on in this phase (reserved for
                           a future reward-computation pass).
        reward_filename:   Parsed but NOT acted on in this phase (reserved for
                           a future reward-persistence pass).
    """

    valid_categories: List[str]
    fallback_category: Optional[str] = None
    verdict_scan: str = "last"
    reward_enabled: bool = False
    reward_filename: str = "adversary_reward.json"


@dataclass
class AdversaryFinding:
    """A single finding produced by an adversary reviewer.

    Attributes:
        category:    Category token from the tagged finding line.  Always
                     stored in lowercase and guaranteed to be a member of
                     the ``AdversaryConfig.valid_categories`` list.
        description: Human-readable description of the weakness.  Preserved
                     verbatim from the adversary output (everything after the
                     ``[category] `` prefix on the line).
    """

    category: str
    description: str


@dataclass
class AdversaryVerdict:
    """Structured result of parsing adversary output.

    Attributes:
        verdict:   ``"APPROVE"`` or ``"REQUEST_CHANGES"``.
        findings:  List of :class:`AdversaryFinding` objects.  Populated
                   independently of the verdict — APPROVE responses can also
                   carry findings.  Empty when no tagged lines were found.
        raw_text:  The original, unmodified input string (or ``str(input)``
                   when the input was coerced from a non-string type).
                   Preserved for downstream traceability.
    """

    verdict: str
    findings: List[AdversaryFinding] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_adversary_output(text: Any, config: AdversaryConfig) -> AdversaryVerdict:
    """Parse raw adversary LLM output into a structured :class:`AdversaryVerdict`.

    Parsing algorithm
    -----------------
    1. **Coerce non-string input** — if *text* is not a ``str`` (e.g. ``None``,
       an int, a dict), coerce via ``str(text)``; on failure fall back to ``""``.
    2. **Extract verdict** — delegate to :func:`~verdict_parser.extract_verdict`
       using ``scan_order=config.verdict_scan`` and
       ``allowed_verdicts={"approve", "request_changes"}``.
    3. **Parse finding lines** — scan every line for the pattern
       ``[category] description``.  Only lines whose category (lowercased) is
       present in ``config.valid_categories`` are accepted; all others are
       silently skipped.
    4. **No verdict found** — default to ``REQUEST_CHANGES`` with a single
       explanatory finding whose category is ``config.fallback_category`` (or
       the first entry of ``config.valid_categories`` when
       ``config.fallback_category is None``).

    The function never raises an exception regardless of input type.

    Args:
        text:   Raw LLM output from the adversary phase.  Any type accepted.
        config: :class:`AdversaryConfig` instance that controls parsing.

    Returns:
        :class:`AdversaryVerdict` populated from the parsed output.
    """
    # ── Step 1: coerce non-string input ──────────────────────────────────────
    raw_text: str
    if isinstance(text, str):
        raw_text = text
    else:
        try:
            raw_text = str(text)
        except Exception:  # noqa: BLE001
            raw_text = ""

    # ── Step 2: extract verdict via shared parser ─────────────────────────────
    parsed_verdict = extract_verdict(
        text=raw_text,
        scan_order=config.verdict_scan,
        allowed_verdicts={"approve", "request_changes"},
    )
    verdict: Optional[str] = parsed_verdict.upper() if parsed_verdict else None

    # ── Step 3: parse finding lines ───────────────────────────────────────────
    valid_set = set(cat.lower() for cat in config.valid_categories)
    findings: List[AdversaryFinding] = []

    for line in raw_text.splitlines():
        m = FINDING_RE.match(line)
        if not m:
            continue
        category_token = m.group(1).lower()
        description = m.group(2)  # preserved verbatim (no strip)

        if category_token not in valid_set:
            logger.debug(
                "adversary_parser: unknown/invalid category %r on line %r — skipping",
                category_token,
                line[:80],
            )
            continue

        findings.append(AdversaryFinding(category=category_token, description=description))

    # ── Step 4: no verdict found → safe default REQUEST_CHANGES ───────────────
    if verdict is None:
        fallback_cat = config.fallback_category
        if fallback_cat is None:
            fallback_cat = config.valid_categories[0]

        logger.warning(
            "adversary_parser: no recognisable verdict found in output " "(first 120 chars): %r",
            raw_text[:120],
        )
        explanatory_finding = AdversaryFinding(
            category=fallback_cat.lower(),
            description=(
                "Adversary output contained no recognisable verdict "
                "(expected 'APPROVE' or 'REQUEST_CHANGES'). "
                "Defaulting to REQUEST_CHANGES for safety."
            ),
        )
        return AdversaryVerdict(
            verdict="REQUEST_CHANGES",
            findings=[explanatory_finding],
            raw_text=raw_text,
        )

    return AdversaryVerdict(verdict=verdict, findings=findings, raw_text=raw_text)
