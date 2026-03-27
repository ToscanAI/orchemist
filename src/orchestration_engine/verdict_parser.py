"""Shared verdict parser for pipeline phase outputs (Issue #678).

Two-pass extraction:
  - Pass 1: Structured ``VERDICT: <keyword>`` lines (last-match-wins, reverse scan).
  - Pass 2: Fallback regex scan stripping markdown (priority: REQUEST_CHANGES > ABORT > APPROVE).

Public API:
  - :func:`extract_verdict` — extract verdict from text or file.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

__all__ = ["extract_verdict"]

logger = logging.getLogger(__name__)

_VERDICT_KEYWORDS = {"approve", "request_changes", "abort"}

# Pass 1: match "VERDICT: <keyword>" lines, allowing markdown around the keyword
# and extra whitespace.  Case-insensitive matching done via re.IGNORECASE.
_PASS1_RE = re.compile(
    r"^\s*verdict\s*:\s*[\*_`#>~\-]*\s*(approve|request_changes|abort)\s*[\*_`#>~\-]*\s*$",
    re.IGNORECASE,
)

# Pass 2: keyword at a markdown-leader boundary (start of meaningful content),
# with trailing boundary that rejects alphanumeric continuation or _[A-Za-z0-9].
# Strips common markdown leaders: #, >, -, *, digits., backticks, bold/italic markers.
_PASS2_RE = re.compile(
    r"^[\s#>*\-`_\d.]*"                          # leading markdown noise
    r"(?:(?:verdict|decision)\s*:\s*)?"           # optional conversational prefix
    r"[\s*_`]*"                                   # more markdown around keyword
    r"(APPROVE|REQUEST_CHANGES|ABORT)"
    r"(?![A-Za-z0-9]|_[A-Za-z0-9])",             # trailing boundary
    re.IGNORECASE,
)

# Pass 2 priority: REQUEST_CHANGES > ABORT > APPROVE
_PRIORITY = ("request_changes", "abort", "approve")


def extract_verdict(
    text: Optional[str] = None,
    file_path: Optional[str] = None,
    allowed_verdicts: Optional[set] = None,
) -> Optional[str]:
    """Extract a verdict keyword from *text* or *file_path*.

    Parameters
    ----------
    text : str, optional
        Raw phase output text.
    file_path : str, optional
        Path to an output file.  Takes priority over *text*; falls back to
        *text* with a warning if the file is missing or empty.
    allowed_verdicts : set of str, optional
        When provided, only verdicts in this set (lowercase) are returned.

    Returns
    -------
    str or None
        Lowercase verdict (``"approve"``, ``"request_changes"``, ``"abort"``),
        or ``None`` if no verdict is found.
    """
    content = _resolve_content(text, file_path)
    if not content or not content.strip():
        return None

    # Pass 1: structured VERDICT: lines — scan in reverse (last match wins)
    verdict = _pass1(content)
    if verdict is not None:
        if allowed_verdicts is not None and verdict not in allowed_verdicts:
            return None
        return verdict

    # Pass 2: full-file regex fallback with priority ordering
    # allowed_verdicts filtering happens inside _pass2 so lower-priority
    # keywords can still win when higher-priority ones are filtered out.
    return _pass2(content, allowed_verdicts)


def _resolve_content(text: Optional[str], file_path: Optional[str]) -> Optional[str]:
    """Return the text to parse, preferring file_path when available."""
    if file_path is not None:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            if content:
                return content
            # Empty file — fall through to text
            logger.warning(
                "verdict_parser: file %r is empty — falling back to text param",
                file_path,
            )
        except (OSError, IOError):
            logger.warning(
                "verdict_parser: file %r not found — falling back to text param",
                file_path,
            )
    return text


def _pass1(content: str) -> Optional[str]:
    """Pass 1: scan lines in reverse for last VERDICT: <keyword> line."""
    for line in reversed(content.splitlines()):
        m = _PASS1_RE.match(line)
        if m:
            return m.group(1).lower()
    return None


def _pass2(content: str, allowed_verdicts: Optional[set] = None) -> Optional[str]:
    """Pass 2: regex scan all lines, collect keywords, apply priority."""
    found: set = set()
    for line in content.splitlines():
        m = _PASS2_RE.match(line)
        if m:
            found.add(m.group(1).lower())

    for keyword in _PRIORITY:
        if keyword in found:
            if allowed_verdicts is not None and keyword not in allowed_verdicts:
                continue
            return keyword
    return None
