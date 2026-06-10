"""Tests for the canonical tagged-finding regex (#919, item 4b; #929 part B).

``FINDING_RE`` lives in ``orchestration_engine.text_utils`` and is consumed by
the single-bracket adversary parser (``adversary_parser``). It requires >=1
whitespace separator and a NON-EMPTY description.

``FINDING_RE_EMPTY_OK`` is the empty-tolerant sibling (same module); it accepts
an EMPTY description. The two patterns differ ONLY in their quantifiers
(``\\s* + (.*)`` vs ``\\s+ + (.+)``). (#703 deleted the legacy
``spec_adversary`` / ``acceptance_test_adversary`` consumers; the regex objects
themselves remain exported + tested here.)
"""

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
    """The surviving adversary parser imports the SAME compiled regex object
    as text_utils (the canonical shared FINDING_RE). #703 deleted the legacy
    spec_adversary consumer; the shared-object identity is still pinned here."""
    from orchestration_engine.adversary_parser import FINDING_RE as B

    assert B is text_utils.FINDING_RE


def test_b3_empty_tolerance_preserved_and_divergent():
    """The empty-tolerant sibling matches a bare ``[coverage]`` (groups
    ``("coverage", "")``); the canonical FINDING_RE still does NOT (the
    intentional behavioral divergence between the two siblings is preserved)."""
    m = FINDING_RE_EMPTY_OK.match("[coverage]")
    assert m is not None
    assert m.groups() == ("coverage", "")
    assert FINDING_RE.match("[coverage]") is None
