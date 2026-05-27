"""Regression tests for #868 — single source-of-truth for verdict prose
in ``templates/coding-pipeline-standard.yaml``.

Before this PR, the four SPEC-author verdict definitions
(CONSUME / EXTEND / DIVERGENT / NEW-OK) were enumerated three times in
the YAML:

  1. ``existing_symbols_inventory`` phase §5 — canonical definition
  2. SPEC phase prompt (~lines 295-304) — near-identical restatement
  3. SPEC_ADVERSARY phase prompt (~lines 482-487) — near-identical restatement

Drift had already begun (the three NEW-OK descriptions diverged across
the three sites). The fix replaces the SPEC and SPEC_ADVERSARY
restatements with an indirection sentence pointing each downstream prompt
at inventory §5 — which they already read as part of Phase 0 sticky
inventory consumption.

These tests assert the post-merge invariants. They MUST NOT be modified
to make them pass — any future change to the YAML must satisfy every
test as written, or these tests must be DELETED with a documented
follow-up.

Sealed before the implementation pass per Orchemist acceptance-test
phase contract.
"""

from __future__ import annotations

import pytest

from orchestration_engine.templates import TemplateEngine


@pytest.fixture(scope="module")
def yaml_text() -> str:
    """Resolve the production pipeline template via the canonical loader API
    (not a hardcoded filesystem path — see ``tests/test_lint_no_templates_hardcode.py``
    issue #632) and return its raw YAML text."""
    path = TemplateEngine().resolve_template("coding-pipeline-standard")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def yaml_lines(yaml_text: str) -> list[str]:
    """Return the YAML as a list of lines (1-indexed access via [n-1])."""
    return yaml_text.splitlines()


# ---------------------------------------------------------------------------
# Section A: §5 canonical definition is intact
# ---------------------------------------------------------------------------


class TestCanonicalSection5Preserved:
    """The canonical §5 wording in ``existing_symbols_inventory`` MUST be
    preserved verbatim — this is the single source-of-truth after the PR.
    """

    def test_section_5_header_present(self, yaml_text: str) -> None:
        """The §5 header line is the anchor downstream prompts indirect to."""
        assert (
            "## 5. Consume-vs-author guidance (sub-check 7d enforcement)"
            in yaml_text
        ), (
            "The §5 header in existing_symbols_inventory must remain — it is "
            "the canonical anchor that SPEC and SPEC_ADVERSARY indirect to."
        )

    def test_four_verdicts_label_present(self, yaml_text: str) -> None:
        """The 'four SPEC-author verdicts' phrase must remain in §5."""
        assert "four SPEC-author verdicts" in yaml_text, (
            "The phrase 'four SPEC-author verdicts' must remain in §5 — "
            "downstream prompts reference it by name."
        )

    def test_consume_definition_present(self, yaml_text: str) -> None:
        """§5's CONSUME definition (with the '(preferred)' qualifier and
        'byte-identical, no signature change' clause) must remain."""
        assert (
            "**CONSUME** (preferred) — import the existing symbol byte-identical"
            in yaml_text
        ), "§5 canonical CONSUME definition missing or modified."

    def test_extend_definition_present(self, yaml_text: str) -> None:
        """§5's EXTEND definition (including the §3a dual-path-helper
        connection added in v2.2.0) must remain."""
        assert "**EXTEND**" in yaml_text, "§5 EXTEND definition missing."
        assert "parameterize the existing symbol" in yaml_text, (
            "§5 EXTEND parameterization clause missing."
        )
        assert "EXTEND-ing an existing dual-path helper surfaced in §3a" in yaml_text, (
            "§5 EXTEND-with-§3a clause missing (v2.2.0 wiring)."
        )

    def test_divergent_definition_present(self, yaml_text: str) -> None:
        """§5's DIVERGENT definition (with the divergence-justification
        subsection requirement) must remain."""
        assert "**DIVERGENT**" in yaml_text, "§5 DIVERGENT definition missing."
        assert "## Divergence justification" in yaml_text, (
            "§5 DIVERGENT justification-subsection clause missing."
        )

    def test_new_ok_definition_present(self, yaml_text: str) -> None:
        """§5's NEW-OK definition (the canonical wording chosen as the
        single source-of-truth) must remain."""
        assert "**NEW-OK** — genuinely new" in yaml_text, (
            "§5 NEW-OK definition missing."
        )
        assert "grep returned zero plausibly-related symbols" in yaml_text, (
            "§5 NEW-OK grep-zero clause missing — this is the canonical wording."
        )

    def test_blocked_escape_present(self, yaml_text: str) -> None:
        """§5's BLOCKED escape (IMPLEMENT-time, not a §6 verdict) must remain."""
        assert (
            "**BLOCKED** — IMPLEMENT-phase escape" in yaml_text
        ), "§5 BLOCKED escape definition missing."


# ---------------------------------------------------------------------------
# Section B: SPEC + SPEC_ADVERSARY restatements REMOVED
# ---------------------------------------------------------------------------


def _find_phase_bounds(yaml_lines: list[str], phase_id: str) -> tuple[int, int]:
    """Return (start, end) line numbers (1-indexed inclusive) of the phase
    whose ``id:`` equals ``phase_id``. The phase ends at the next ``- id:``
    line OR the top-level ``auto_merge:`` key.
    """
    start = -1
    end = len(yaml_lines)
    for idx, line in enumerate(yaml_lines, 1):
        if line.strip() == f"- id: {phase_id}":
            start = idx
            continue
        if start > 0 and (
            line.lstrip().startswith("- id: ")
            or line.startswith("auto_merge:")
            or line.startswith("routing_config:")
        ):
            end = idx - 1
            break
    if start < 0:
        raise AssertionError(f"phase id '{phase_id}' not found in YAML")
    return start, end


class TestSpecRestatementRemoved:
    """The SPEC phase prompt MUST NOT restate the four-verdict enumeration.
    The indirection sentence MUST replace it.
    """

    def test_spec_phase_bounds_known(self, yaml_lines: list[str]) -> None:
        """Sanity: spec phase is locatable."""
        start, end = _find_phase_bounds(yaml_lines, "spec")
        assert start > 0 and end > start, (
            f"spec phase bounds not findable: ({start}, {end})"
        )

    def test_spec_no_consume_byte_identical_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC must not restate the CONSUME definition prose. The signature
        phrase 'byte-identical' inside the SPEC phase indicates the
        enumeration is still present."""
        start, end = _find_phase_bounds(yaml_lines, "spec")
        spec_block = "\n".join(yaml_lines[start - 1 : end])
        # The phrase 'byte-identical' appears in §5's CONSUME definition only.
        # If it appears in the SPEC phase, the restatement is still present.
        assert "byte-identical" not in spec_block, (
            "SPEC phase must not contain 'byte-identical' — that signature "
            "phrase comes from §5's CONSUME definition. The restatement "
            "should have been replaced with §5 indirection."
        )

    def test_spec_no_extend_parameterize_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC must not restate the EXTEND parameterization prose."""
        start, end = _find_phase_bounds(yaml_lines, "spec")
        spec_block = "\n".join(yaml_lines[start - 1 : end])
        assert "parameterize the existing symbol" not in spec_block, (
            "SPEC phase must not contain 'parameterize the existing symbol' — "
            "that prose comes from §5's EXTEND definition. The restatement "
            "should have been replaced with §5 indirection."
        )

    def test_spec_no_divergent_justification_subsection_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC must not restate the DIVERGENT divergence-justification clause
        verbatim — that text comes from §5."""
        start, end = _find_phase_bounds(yaml_lines, "spec")
        spec_block = "\n".join(yaml_lines[start - 1 : end])
        # The exact §5 DIVERGENT phrase. SPEC may still REFERENCE divergence
        # in other contexts; this assertion is narrow to the restated bullet.
        assert "near-equivalent with a contract-required difference" not in spec_block, (
            "SPEC phase must not contain the §5 DIVERGENT prose."
        )

    def test_spec_no_new_ok_overlap_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC must not restate the NEW-OK 'no overlap with sections 1-4'
        prose verbatim — the drifted SPEC version conflicts with §5."""
        start, end = _find_phase_bounds(yaml_lines, "spec")
        spec_block = "\n".join(yaml_lines[start - 1 : end])
        assert "genuinely new; no overlap with sections 1-4" not in spec_block, (
            "SPEC phase must not restate the NEW-OK prose — the drifted "
            "version diverged from §5's canonical wording."
        )

    def test_spec_contains_section_5_indirection(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC must explicitly indirect at §5 for the verdict definitions."""
        start, end = _find_phase_bounds(yaml_lines, "spec")
        spec_block = "\n".join(yaml_lines[start - 1 : end])
        assert "existing_symbols.md" in spec_block, (
            "SPEC phase must still reference existing_symbols.md."
        )
        assert "§5" in spec_block, (
            "SPEC phase must contain a §5 reference — that is the indirection "
            "anchor for the verdict definitions."
        )


class TestSpecAdversaryRestatementRemoved:
    """The SPEC_ADVERSARY phase prompt MUST NOT restate the four-verdict
    enumeration. The indirection sentence MUST replace it.
    """

    def test_spec_adversary_phase_bounds_known(
        self, yaml_lines: list[str]
    ) -> None:
        """Sanity: spec_adversary phase is locatable."""
        start, end = _find_phase_bounds(yaml_lines, "spec_adversary")
        assert start > 0 and end > start, (
            f"spec_adversary phase bounds not findable: ({start}, {end})"
        )

    def test_spec_adversary_no_named_import_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC_ADVERSARY must not restate the SPEC_ADVERSARY-version CONSUME
        prose ('Cross-check: the import is concrete (named import; not a
        re-namespacing).' is uniquely from line 484)."""
        start, end = _find_phase_bounds(yaml_lines, "spec_adversary")
        block = "\n".join(yaml_lines[start - 1 : end])
        # The "Cross-check" qualifier on the CONSUME line is the SPEC_ADVERSARY
        # restatement's identifying signature. Removing it confirms the
        # restated bullet block is gone.
        assert "import is concrete (named import" not in block, (
            "SPEC_ADVERSARY must not contain the 'named import; not a "
            "re-namespacing' cross-check — that prose comes from the §5 "
            "CONSUME restatement. The restatement should have been replaced."
        )

    def test_spec_adversary_no_extend_parameterization_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC_ADVERSARY must not restate the EXTEND-with-non-empty-Files-to-Create
        cross-check verbatim — that prose comes from the §5 restatement."""
        start, end = _find_phase_bounds(yaml_lines, "spec_adversary")
        block = "\n".join(yaml_lines[start - 1 : end])
        assert "EXTEND-with-non-empty-Files-to-Create" not in block, (
            "SPEC_ADVERSARY must not contain 'EXTEND-with-non-empty-Files-to-Create' "
            "verbatim — that prose comes from the restated EXTEND bullet."
        )

    def test_spec_adversary_no_new_ok_none_found_restatement(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC_ADVERSARY must not restate the drifted NEW-OK prose
        ('(none found) with rationale'). That wording exists ONLY in the
        SPEC_ADVERSARY restatement and is the most-drifted of the three."""
        start, end = _find_phase_bounds(yaml_lines, "spec_adversary")
        block = "\n".join(yaml_lines[start - 1 : end])
        assert "(none found)" not in block, (
            "SPEC_ADVERSARY must not contain the drifted NEW-OK '(none found)' "
            "wording — that diverges from §5's canonical 'grep returned zero "
            "plausibly-related symbols' phrasing."
        )

    def test_spec_adversary_contains_section_5_indirection(
        self, yaml_lines: list[str]
    ) -> None:
        """SPEC_ADVERSARY must explicitly indirect at §5 for the verdict
        definitions."""
        start, end = _find_phase_bounds(yaml_lines, "spec_adversary")
        block = "\n".join(yaml_lines[start - 1 : end])
        assert "existing_symbols.md" in block, (
            "SPEC_ADVERSARY phase must still reference existing_symbols.md."
        )
        assert "§5" in block, (
            "SPEC_ADVERSARY phase must contain a §5 reference."
        )


# ---------------------------------------------------------------------------
# Section C: Whole-YAML invariants (single-source-of-truth measurement)
# ---------------------------------------------------------------------------


class TestSingleSourceOfTruthInvariants:
    """Whole-YAML grep-style invariants that lock in the single source-of-truth
    state."""

    def test_consume_preferred_appears_exactly_once(
        self, yaml_text: str
    ) -> None:
        """The phrase 'CONSUME (preferred)' is unique to §5's CONSUME bullet.
        After the PR, exactly ONE occurrence must exist in the YAML."""
        count = yaml_text.count("CONSUME** (preferred)")
        assert count == 1, (
            f"expected exactly 1 'CONSUME** (preferred)' occurrence (canonical "
            f"§5 location); found {count}. Multiple occurrences indicate a "
            f"restatement was not removed."
        )

    def test_byte_identical_appears_exactly_once(
        self, yaml_text: str
    ) -> None:
        """The CONSUME 'byte-identical' signature phrase must appear exactly
        once after the PR — only in §5."""
        # Allow only one occurrence of "import the existing symbol byte-identical"
        canonical = yaml_text.count("import the existing symbol byte-identical")
        assert canonical == 1, (
            f"expected exactly 1 'import the existing symbol byte-identical' "
            f"occurrence (§5 CONSUME); found {canonical}."
        )

    def test_parameterize_existing_symbol_appears_exactly_once(
        self, yaml_text: str
    ) -> None:
        """The EXTEND 'parameterize the existing symbol' clause must appear
        exactly once after the PR — only in §5."""
        count = yaml_text.count("parameterize the existing symbol")
        assert count == 1, (
            f"expected exactly 1 'parameterize the existing symbol' occurrence "
            f"(§5 EXTEND); found {count}."
        )

    def test_genuinely_new_phrase_appears_in_section_5_only(
        self, yaml_lines: list[str]
    ) -> None:
        """The §5 NEW-OK 'genuinely new' wording (canonical) must appear
        only in §5 — the drifted SPEC/SPEC_ADVERSARY restatements are gone.

        The §5 ``existing_symbols_inventory`` phase contains the phrase three
        times by design: twice on the NEW-OK bullet line ('genuinely new; ...
        (none — genuinely new)') and once on the §6 example placeholder. The
        assertion here is precisely that NO occurrence appears OUTSIDE the
        ``existing_symbols_inventory`` phase — equivalently: SPEC,
        SPEC_ADVERSARY, and every other phase must be free of the phrase.
        """
        inv_start, inv_end = _find_phase_bounds(
            yaml_lines, "existing_symbols_inventory"
        )
        outside_inventory = "\n".join(
            yaml_lines[: inv_start - 1] + yaml_lines[inv_end:]
        )
        assert "genuinely new" not in outside_inventory, (
            "'genuinely new' must appear only inside the "
            "existing_symbols_inventory phase (§5 + §6 example). Any "
            "occurrence elsewhere indicates a restatement is still present."
        )
