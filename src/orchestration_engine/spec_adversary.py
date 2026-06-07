"""
spec_adversary.py — Adversarial spec reviewer with reward logic.

Provides parsing and reward-tracking for the ``spec_adversary`` phase of the
coding pipeline.  The adversary phase reviews behavioral contracts produced by
the ``spec`` phase before acceptance tests are written, catching vague
contracts, trivially-satisfiable specs, missing edge cases, implementation
leakage, and spec-vs-implementation divergence.

Public API (all exported via ``__all__``)
-----------------------------------------
* :class:`AdversaryFinding` — single finding from an adversary review
* :class:`AdversaryVerdict` — full structured verdict (APPROVE or REQUEST_CHANGES)
* :func:`parse_adversary_output` — parse raw LLM text into an :class:`AdversaryVerdict`
* :func:`compute_reward` — compute the reward score from a verdict
* :func:`persist_reward` — write ``adversary_reward.json`` to the output directory

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .text_utils import FINDING_RE
from .verdict_parser import extract_verdict as _extract_verdict

__all__ = [
    "AdversaryFinding",
    "AdversaryVerdict",
    "parse_adversary_output",
    "compute_reward",
    "persist_reward",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid finding categories (case-insensitive on input, stored lowercase)
# ---------------------------------------------------------------------------

_VALID_CATEGORIES: frozenset[str] = frozenset(
    ["vague", "trivial", "missing_edge_case", "leakage", "divergence"]
)

# Tagged finding-line matcher ([category] description) is the canonical
# ``FINDING_RE`` imported from :mod:`text_utils` (single source of truth shared
# with adversary_parser). This phase strips the captured description at the call
# site below.

# Valid verdict tokens — checked as exact stripped first-word match
_VALID_VERDICTS: tuple[str, ...] = ("APPROVE", "REQUEST_CHANGES")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AdversaryFinding:
    """A single finding produced by the adversarial spec reviewer.

    Attributes:
        category:    One of the five recognised weakness categories:
                     ``vague``, ``trivial``, ``missing_edge_case``,
                     ``leakage``, ``divergence``.  Always stored in lowercase.
        description: Human-readable description of the weakness.  Preserved
                     verbatim from the adversary output.
    """

    category: str
    description: str


@dataclass
class AdversaryVerdict:
    """Structured result of parsing adversary output.

    Attributes:
        verdict:   ``"APPROVE"`` or ``"REQUEST_CHANGES"``.
        findings:  List of :class:`AdversaryFinding` objects.  Empty when the
                   verdict is ``"APPROVE"`` or when no tagged findings were
                   found in a ``REQUEST_CHANGES`` response.
        raw_text:  The original, unmodified input string.  Preserved for
                   downstream traceability.
    """

    verdict: str
    findings: List[AdversaryFinding] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_adversary_output(text) -> AdversaryVerdict:
    """Parse raw adversary LLM output into a structured :class:`AdversaryVerdict`.

    Parsing algorithm
    -----------------
    1. **Coerce non-string input** — if *text* is not a string (e.g. ``None``),
       attempt ``str(text)``; on failure use ``""``.
    2. **Find the verdict** — scan lines in order; the first non-blank line
       whose first whitespace-stripped token exactly matches ``APPROVE`` or
       ``REQUEST_CHANGES`` (case-insensitive) is taken as the verdict.
    3. **Parse findings** — all lines matching ``[category] description``
       (``FINDING_RE``) are parsed as findings.  Category tokens are
       normalised to lowercase.  Lines that do not match the pattern are
       silently skipped.  Lines with unrecognised category tokens are also
       silently skipped.
    4. **No verdict found** — defaults to ``REQUEST_CHANGES`` with a single
       ``vague`` finding that explains the parse failure.  This is the safe
       default: never assume approval from ambiguous output.

    Graceful degradation
    --------------------
    * Never raises an exception.
    * Empty string input → ``REQUEST_CHANGES`` with one explanatory finding.
    * Non-string input → coerced; same safe-default behaviour.

    Args:
        text: Raw LLM output from the ``spec_adversary`` phase.

    Returns:
        :class:`AdversaryVerdict` populated from the parsed output.
    """
    # ── Coerce non-string input ───────────────────────────────────────────────
    raw_text: str
    if not isinstance(text, str):
        try:
            raw_text = str(text)
        except Exception:
            raw_text = ""
    else:
        raw_text = text

    lines = raw_text.splitlines()

    # ── Verdict extraction via shared parser (Issue #678) ───────────────────
    _parsed_verdict = _extract_verdict(
        text=raw_text, allowed_verdicts={"approve", "request_changes"}
    )
    verdict: str | None = _parsed_verdict.upper() if _parsed_verdict else None

    # ── Finding extraction (all lines matching [category] description) ────────
    findings: List[AdversaryFinding] = []
    for line in lines:
        m = FINDING_RE.match(line)
        if not m:
            logger.debug(
                "spec_adversary: skipping non-finding line: %r", line.strip()[:80]
            )
            continue
        category_token = m.group(1).lower()
        description = m.group(2).strip()

        if category_token not in _VALID_CATEGORIES:
            logger.debug(
                "spec_adversary: unknown category %r on line %r — skipping",
                category_token,
                line.strip()[:80],
            )
            continue

        findings.append(AdversaryFinding(category=category_token, description=description))

    # ── Safe default: no verdict found → REQUEST_CHANGES + explanatory finding
    if verdict is None:
        logger.warning(
            "spec_adversary: no recognisable verdict found in output "
            "(first 120 chars): %r",
            raw_text[:120],
        )
        explanatory_finding = AdversaryFinding(
            category="vague",
            description=(
                "Adversary output contained no recognisable verdict "
                "(expected 'APPROVE' or 'REQUEST_CHANGES' as the first "
                "non-blank line). Defaulting to REQUEST_CHANGES for safety."
            ),
        )
        return AdversaryVerdict(
            verdict="REQUEST_CHANGES",
            findings=[explanatory_finding],
            raw_text=raw_text,
        )

    return AdversaryVerdict(verdict=verdict, findings=findings, raw_text=raw_text)


def compute_reward(verdict: AdversaryVerdict) -> int:
    """Compute the reward score for an adversary verdict.

    The reward score captures how many actionable weaknesses the adversary
    found.  It is used to track adversary quality over time: a higher score
    means the adversary found more problems (and the spec needs more work).

    Behaviour
    ---------
    * ``REQUEST_CHANGES`` → ``len(verdict.findings)`` — each distinct finding
      is one unit of reward.
    * ``APPROVE`` → ``0`` — a clean spec earns no adversary reward.

    Args:
        verdict: A parsed :class:`AdversaryVerdict`.

    Returns:
        Non-negative integer reward score.
    """
    if verdict.verdict == "APPROVE":
        return 0
    # REQUEST_CHANGES (or any unexpected verdict): count findings
    return len(verdict.findings)


def persist_reward(output_dir, verdict: AdversaryVerdict, reward: int) -> None:
    """Write adversary reward data to ``adversary_reward.json`` in *output_dir*.

    File format
    -----------
    .. code-block:: json

        {
          "verdict": "APPROVE" | "REQUEST_CHANGES",
          "reward_score": <int>,
          "findings_count": <int>,
          "findings": [{"category": "...", "description": "..."}],
          "persisted_at": "<ISO 8601 timestamp>"
        }

    Graceful degradation
    --------------------
    * If *output_dir* is ``None``, logs a WARNING and returns without raising.
    * If the directory does not exist, logs a WARNING and returns without raising.
    * Any other I/O error is caught, logged as WARNING, and swallowed.

    Args:
        output_dir: Path (string or :class:`~pathlib.Path`) to the output
                    directory.  May be ``None``.
        verdict:    The :class:`AdversaryVerdict` to persist.
        reward:     Pre-computed reward score (output of :func:`compute_reward`).
    """
    if output_dir is None:
        logger.warning(
            "spec_adversary: persist_reward called with output_dir=None "
            "— skipping reward file write."
        )
        return

    dir_path = Path(output_dir)
    if not dir_path.exists():
        logger.warning(
            "spec_adversary: persist_reward — output_dir %r does not exist "
            "— skipping reward file write.",
            str(output_dir),
        )
        return

    payload = {
        "verdict": verdict.verdict,
        "reward_score": reward,
        "findings_count": len(verdict.findings),
        "findings": [
            {"category": f.category, "description": f.description}
            for f in verdict.findings
        ],
        "persisted_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    reward_path = dir_path / "adversary_reward.json"
    try:
        reward_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(
            "spec_adversary: reward persisted to %r "
            "(verdict=%s, reward=%d, findings=%d)",
            str(reward_path),
            verdict.verdict,
            reward,
            len(verdict.findings),
        )
    except Exception as exc:
        logger.warning(
            "spec_adversary: failed to write %r — %s",
            str(reward_path),
            exc,
        )
