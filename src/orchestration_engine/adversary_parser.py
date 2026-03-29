"""
adversary_parser.py — Generic adversary output parser for pipeline phases.

Provides a config-driven parser that can be used by any adversary phase,
replacing per-phase hardcoded parsers.  Behavior is entirely driven by the
:class:`AdversaryConfig` instance passed at call time.

Public API (all exported via ``__all__``):
  - :class:`AdversaryConfig`  — configuration dataclass
  - :class:`AdversaryFinding` — single finding dataclass
  - :class:`AdversaryVerdict` — full verdict dataclass
  - :func:`parse_adversary_output` — generic parser function

No third-party dependencies — stdlib only (plus internal verdict_parser).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .verdict_parser import extract_verdict

__all__ = [
    "AdversaryConfig",
    "AdversaryFinding",
    "AdversaryVerdict",
    "parse_adversary_output",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex for tagged finding lines: [category] description
# Group 1 → category (letters and underscores only)
# Group 2 → description (everything after the single literal space following "]")
# Preserves all leading/trailing whitespace in description verbatim.
# ---------------------------------------------------------------------------
_FINDING_RE = re.compile(r"^\s*\[([A-Za-z_]+)\] (.*)$")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AdversaryConfig:
    """Configuration for the generic adversary parser.

    Attributes:
        valid_categories:  Ordered list of accepted finding category tokens
                           (case-sensitive — should match what the LLM emits,
                           typically lowercase).
        fallback_category: Category used when no verdict is found and an
                           explanatory finding must be synthesised.  When
                           ``None`` (default), the first entry in
                           ``valid_categories`` is used instead.
        verdict_scan:      ``"last"`` (default) — last ``VERDICT:`` line wins;
                           ``"first"`` — first ``VERDICT:`` line wins.
                           Passed directly to :func:`~verdict_parser.extract_verdict`.
        reward_enabled:    Parsed and stored but not acted on in this phase.
                           Reserved for Phase 2 reward computation (#702).
        reward_filename:   Reward output filename.  Parsed but not acted on here.
    """

    valid_categories: List[str]
    fallback_category: Optional[str] = None
    verdict_scan: str = "last"
    reward_enabled: bool = False
    reward_filename: str = "adversary_reward.json"


@dataclass
class AdversaryFinding:
    """A single finding produced by an adversary phase.

    Attributes:
        category:    Category token (always lowercase, must be in
                     :attr:`AdversaryConfig.valid_categories`).
        description: Human-readable finding description.  Preserved verbatim
                     from the LLM output line (everything after the
                     ``[category] `` prefix).
    """

    category: str
    description: str


@dataclass
class AdversaryVerdict:
    """Structured result of parsing adversary output.

    Attributes:
        verdict:   ``"APPROVE"`` or ``"REQUEST_CHANGES"``.
        findings:  List of :class:`AdversaryFinding` objects.  Populated
                   independently of the verdict — an APPROVE with tagged
                   finding lines still produces non-empty findings.
        raw_text:  Original input preserved verbatim (or ``str(input)`` when
                   the input was coerced from a non-string type).
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
    1. **Coerce non-string input** — if *text* is not a ``str`` (including
       ``None``), convert via ``str(text)``; on failure use ``""``.
    2. **Extract verdict** via :func:`~verdict_parser.extract_verdict` with
       ``scan_order=config.verdict_scan`` and
       ``allowed_verdicts={"approve", "request_changes"}``.
    3. **Parse findings** — all lines matching ``[category] description``
       (``_FINDING_RE``) are parsed.  Category tokens are normalised to
       lowercase.  Lines that do not match the pattern, or whose category is
       not in ``config.valid_categories``, are silently skipped.
    4. **No verdict found** — defaults to ``REQUEST_CHANGES`` with a single
       finding whose category is ``config.fallback_category`` (or the first
       entry in ``config.valid_categories`` when ``fallback_category`` is
       ``None``).  This is the safe default: never assume approval from
       ambiguous output.

    Graceful degradation
    --------------------
    * **Never raises** — all exceptions are swallowed internally.
    * ``None`` input → coerced to ``"None"``; returns ``REQUEST_CHANGES``.
    * Empty / whitespace-only string → returns ``REQUEST_CHANGES``.
    * Any non-string type → coerced via ``str()``; returns ``REQUEST_CHANGES``.

    Args:
        text:   Raw LLM output.  Any Python type is accepted.
        config: :class:`AdversaryConfig` controlling parsing behaviour.

    Returns:
        :class:`AdversaryVerdict` populated from the parsed output.
    """
    # ── 1. Coerce non-string input ─────────────────────────────────────────
    if not isinstance(text, str):
        try:
            raw_text: str = str(text)
        except Exception:
            raw_text = ""
    else:
        raw_text = text

    # ── 2. Extract verdict ─────────────────────────────────────────────────
    _parsed_verdict = extract_verdict(
        text=raw_text,
        scan_order=config.verdict_scan,
        allowed_verdicts={"approve", "request_changes"},
    )
    verdict: Optional[str] = _parsed_verdict.upper() if _parsed_verdict else None

    # ── 3. Parse findings (independent of verdict) ────────────────────────
    valid_set = set(config.valid_categories)
    findings: List[AdversaryFinding] = []

    for line in raw_text.splitlines():
        m = _FINDING_RE.match(line)
        if not m:
            continue
        category_token = m.group(1).lower()
        description = m.group(2)  # verbatim — no strip

        if category_token not in valid_set:
            logger.debug(
                "adversary_parser: skipping category %r — not in valid_categories %r",
                category_token,
                list(config.valid_categories),
            )
            continue

        findings.append(AdversaryFinding(category=category_token, description=description))

    # ── 4. Safe default: no verdict found ─────────────────────────────────
    if verdict is None:
        fallback_cat: str
        if config.fallback_category is not None:
            fallback_cat = config.fallback_category
        elif config.valid_categories:
            fallback_cat = config.valid_categories[0]
        else:
            fallback_cat = "unknown"

        logger.warning(
            "adversary_parser: no recognisable verdict in output "
            "(first 120 chars): %r — defaulting to REQUEST_CHANGES",
            raw_text[:120],
        )
        return AdversaryVerdict(
            verdict="REQUEST_CHANGES",
            findings=[
                AdversaryFinding(
                    category=fallback_cat,
                    description=(
                        "Adversary output contained no recognisable verdict "
                        "(expected 'APPROVE' or 'REQUEST_CHANGES'). "
                        "Defaulting to REQUEST_CHANGES for safety."
                    ),
                )
            ],
            raw_text=raw_text,
        )

    return AdversaryVerdict(verdict=verdict, findings=findings, raw_text=raw_text)
