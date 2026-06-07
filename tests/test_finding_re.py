"""Tests for the canonical tagged-finding regex (#919, item 4b).

``FINDING_RE`` lives in ``orchestration_engine.text_utils`` and is shared by the
single-bracket adversary parsers (``spec_adversary`` and ``adversary_parser``).
It requires >=1 whitespace separator and a NON-EMPTY description.
``acceptance_test_adversary`` is intentionally NOT a consumer (it accepts an
empty description via its own ``\\s*(.*)$`` pattern).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine import text_utils
from orchestration_engine.text_utils import FINDING_RE


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


def test_acceptance_test_adversary_is_not_a_consumer():
    """acceptance_test_adversary keeps its own distinct (empty-desc) pattern."""
    from orchestration_engine import acceptance_test_adversary as ata

    assert ata._FINDING_RE is not text_utils.FINDING_RE
    # Its pattern still matches an empty description (the reason it is excluded).
    assert ata._FINDING_RE.match("[coverage]") is not None
