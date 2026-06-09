"""Tests for the canonical tagged-finding regex (#919, item 4b; #929 part B).

``FINDING_RE`` lives in ``orchestration_engine.text_utils`` and is shared by the
single-bracket adversary parsers (``spec_adversary`` and ``adversary_parser``).
It requires >=1 whitespace separator and a NON-EMPTY description.

``acceptance_test_adversary`` intentionally accepts an EMPTY description, so it
consumes the empty-tolerant sibling ``FINDING_RE_EMPTY_OK`` (same module). The
two patterns differ ONLY in their quantifiers (``\\s* + (.*)`` vs ``\\s+ +
(.+)``); #929 part B dedups the previously-private copy onto the shared object.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine import text_utils
from orchestration_engine.text_utils import FINDING_RE, FINDING_RE_EMPTY_OK


def test_matches_category_and_description():
    assert FINDING_RE.match("[vague] something").groups() == ("vague", "something")


def test_tolerates_leading_whitespace():
    m = FINDING_RE.match("  [trivial] x")
    assert m is not None
    assert m.groups() == ("trivial", "x")


def test_empty_description_does_not_match():
    """The canonical contract: a bare ``[coverage]`` (no desc) does NOT match."""
    assert FINDING_RE.match("[coverage]") is None


def test_underscore_category_allowed():
    assert FINDING_RE.match("[missing_edge_case] detail").groups() == (
        "missing_edge_case",
        "detail",
    )


def test_shared_identity_with_consumers():
    """Both adversary parsers import the SAME compiled regex object."""
    from orchestration_engine.spec_adversary import FINDING_RE as A
    from orchestration_engine.adversary_parser import FINDING_RE as B

    assert A is B is text_utils.FINDING_RE


def test_acceptance_test_adversary_shares_empty_ok_variant():
    """#929 part B (EXTEND): acceptance_test_adversary now consumes the shared
    empty-tolerant sibling ``FINDING_RE_EMPTY_OK`` rather than a private copy.

    Its module-level ``_FINDING_RE`` is an ALIAS to ``text_utils.FINDING_RE_EMPTY_OK``
    (shared object), distinct from the canonical non-empty ``FINDING_RE``, and the
    empty-tolerance behaviour is preserved.
    """
    from orchestration_engine import acceptance_test_adversary as ata

    # The alias shares the shared object (EXTEND, not a private duplicate).
    assert ata._FINDING_RE is text_utils.FINDING_RE_EMPTY_OK
    # It is the empty-tolerant sibling, NOT the canonical non-empty one.
    assert ata._FINDING_RE is not text_utils.FINDING_RE
    # Empty-tolerance still holds.
    m = ata._FINDING_RE.match("[coverage]")
    assert m is not None
    assert m.groups() == ("coverage", "")


# ---------------------------------------------------------------------------
# §6.B — differential parse: the shared FINDING_RE_EMPTY_OK is byte-identical
# in behaviour to the OLD private acceptance_test_adversary._FINDING_RE.
# ---------------------------------------------------------------------------

# Reference: the EXACT pattern string the old private _FINDING_RE used.
_OLD_FINDING_RE = re.compile(r"^\s*\[([A-Za-z_]+)\]\s*(.*)$")

# Representative corpus exercising the empty-desc / zero-whitespace edges plus
# normal, leading-ws, non-finding, bad-category and empty lines.
_CORPUS = [
    "[coverage] missing edge case",      # normal finding
    "[coverage]",                        # bare, empty description
    "[coverage]x",                       # zero-whitespace, non-empty
    "[specificity] ",                    # trailing-whitespace-only desc
    "  [trivial_satisfaction]   spaces ",  # leading whitespace + spaces
    "not a finding line",                # non-finding
    "[BadCat] desc",                     # category shape ok, value invalid
    "",                                  # empty line
    "[leakage] something",               # normal finding
]


def test_b1_differential_parse_zero_diffs():
    """For every corpus line, the shared variant matches identically to the
    OLD private pattern (None-ness AND .groups()) — proving 0 behavioral diffs."""
    from orchestration_engine import acceptance_test_adversary as ata

    for line in _CORPUS:
        old = _OLD_FINDING_RE.match(line)
        new = FINDING_RE_EMPTY_OK.match(line)
        alias = ata._FINDING_RE.match(line)

        # None-ness identical.
        assert (old is None) == (new is None) == (alias is None), line
        if old is not None:
            assert new.groups() == old.groups(), line
            assert alias.groups() == old.groups(), line


def test_b2_shared_identity_extend():
    """The alias shares the shared object; the empty-tolerant sibling is
    distinct from the canonical non-empty FINDING_RE."""
    from orchestration_engine import acceptance_test_adversary as ata

    assert ata._FINDING_RE is FINDING_RE_EMPTY_OK
    assert FINDING_RE_EMPTY_OK is not text_utils.FINDING_RE


def test_b3_empty_tolerance_preserved_and_divergent():
    """The empty-tolerant sibling matches a bare ``[coverage]`` (groups
    ``("coverage", "")``); the canonical FINDING_RE still does NOT (the
    intentional behavioral divergence between the two siblings is preserved)."""
    m = FINDING_RE_EMPTY_OK.match("[coverage]")
    assert m is not None
    assert m.groups() == ("coverage", "")
    assert FINDING_RE.match("[coverage]") is None


def test_b4_end_to_end_parse_includes_empty_description():
    """The consumer's end-to-end behaviour is unchanged: a ``[coverage]``
    (empty-desc) line plus a normal ``[leakage] something`` line both yield
    findings, with the empty one carrying ``description == ""``."""
    from orchestration_engine.acceptance_test_adversary import parse_adversary_output

    text = "VERDICT: REQUEST_CHANGES\n[coverage]\n[leakage] something\n"
    result = parse_adversary_output(text)
    by_cat = {f.category: f.description for f in result.findings}
    assert by_cat.get("coverage") == ""
    assert by_cat.get("leakage") == "something"
