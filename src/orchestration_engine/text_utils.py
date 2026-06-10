"""Shared text-normalisation helpers used across the engine.

Canonical home for slug / snake-case / identifier transformations that were
previously duplicated across :mod:`issue_automation` (``slugify_branch``)
and :mod:`importers.plugin_command` (``slugify``, ``snake_case``).

The algorithm intentionally matches the prior implementations byte-for-byte
for ASCII input; non-ASCII characters decompose via NFKD and drop their
combining marks (so ``Über`` → ``uber`` and ``résumé`` → ``resume``).
Characters with no ASCII approximation (CJK, emoji) are dropped after
transliteration.

Test parity with the deprecated call sites is enforced by the existing
test files :mod:`tests.test_issue_automation` (slugify_branch) and
:mod:`tests.test_plugin_command` (slugify).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["FINDING_RE", "FINDING_RE_EMPTY_OK", "slugify", "slugify_branch", "snake_case"]


#: Canonical tagged-finding line matcher used by the single-bracket adversary
#: parsers: "[category] description". Group 1 = category (letters/underscore),
#: group 2 = description. Requires >=1 whitespace separator and a NON-EMPTY
#: description. NOTE: acceptance_test_adversary intentionally accepts an EMPTY
#: description and so consumes the empty-tolerant sibling
#: :data:`FINDING_RE_EMPTY_OK` (same module) rather than this constant.
FINDING_RE = re.compile(r"^\s*\[([A-Za-z_]+)\]\s+(.+)$")


#: Empty-tolerant variant of FINDING_RE for acceptance_test_adversary, which
#: intentionally accepts findings with NO description and ZERO whitespace after
#: the category bracket (e.g. "[coverage]"). Group 1 = category, group 2 =
#: description (possibly empty). Differs from FINDING_RE only in the quantifiers
#: (\s* + (.*) vs \s+ + (.+)). Shared so the two parsers do not duplicate the
#: pattern; see #929 / #919 item 4b.
FINDING_RE_EMPTY_OK = re.compile(r"^\s*\[([A-Za-z_]+)\]\s*(.*)$")


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe, hyphenated slug.

    Algorithm: NFKD-normalise → encode to ASCII (drops combining diacritics,
    transliterates accented Latin characters) → lowercase → replace runs of
    non-alphanumeric characters with ``-`` → strip leading/trailing hyphens.

    Transliteration covers the common case of accented Latin characters
    (``Ü`` → ``U``, ``é`` → ``e``, etc.), preventing silent ID collisions
    between titles that differ only in diacritics.  Characters with no ASCII
    approximation (CJK, emoji, etc.) are dropped after transliteration, so
    purely non-ASCII titles may still produce a short or empty slug — callers
    should handle the empty-string case explicitly (see ``slugify_branch``
    for the branch-name variant which substitutes ``"issue"``).

    Examples::

        slugify("Campaign Plan")        # "campaign-plan"
        slugify("  Hello, World! ")     # "hello-world"
        slugify("Brand Voice & Tone")   # "brand-voice-tone"
        slugify("Über Plan")            # "uber-plan"
        slugify("🎯 Campaign")          # "campaign"
        slugify("")                     # ""
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def slugify_branch(title: str, max_length: int = 40) -> str:
    """Convert an issue title to a git-branch-safe slug.

    Wraps :func:`slugify` with two additions required by issue-driven
    branch creation: truncate to *max_length* characters (stripping any
    trailing hyphen the truncation may leave dangling) and substitute the
    placeholder ``"issue"`` when the resulting slug is empty (purely
    whitespace input, or input that NFKD strips to empty).

    Args:
        title:      Raw title string (may contain Unicode, spaces, special chars).
        max_length: Maximum length of the returned slug.  Defaults to ``40``.

    Returns:
        A lowercase, hyphen-separated slug safe for use in a git branch name.
        Never starts or ends with a hyphen.

    Examples::

        slugify_branch("Fix null pointer in pipeline runner")
        # → "fix-null-pointer-in-pipeline-runner"

        slugify_branch("Add résumé parser 🚀")
        # → "add-resume-parser"

        slugify_branch("")
        # → "issue"
    """
    if not title or not title.strip():
        return "issue"

    slug = slugify(title)[:max_length].rstrip("-")
    return slug or "issue"


def snake_case(text: str) -> str:
    """Convert *text* to a snake_case identifier.

    Algorithm: lowercase → replace runs of non-alphanumeric characters with
    ``_`` → strip leading/trailing underscores.

    Examples::

        snake_case("Campaign goal")               # "campaign_goal"
        snake_case("Target audience (optional)")  # "target_audience_optional"
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")
