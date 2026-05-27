"""Regression tests for #836 — single source-of-truth for _VERDICT_KEYWORDS.

Before this consolidation, `verdict_parser._VERDICT_KEYWORDS` (lowercase set)
and `transitions._VERDICT_KEYWORDS` (uppercase tuple) were two independent
definitions of the same semantic set. Drift between them was a latent
risk: if a refactor dropped the `.upper()` call at the sole consume site
(sequencer.py), or if a new verdict were added to only one definition,
silent verdict-extraction failures would result.

After #836 there is exactly ONE definition; `transitions._VERDICT_KEYWORDS`
re-exports the canonical lowercase set.
"""

from __future__ import annotations


class TestSingleSourceOfTruth:
    def test_only_one_definition_in_src(self):
        """A grep across src/ must return exactly one `_VERDICT_KEYWORDS = …` line."""
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src" / "orchestration_engine"
        matches: list[str] = []
        for py in src.rglob("*.py"):
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"\s*_VERDICT_KEYWORDS\s*=\s*[\{\(\[\"']", line):
                    matches.append(f"{py.relative_to(src)}:{lineno}: {line.strip()}")
        assert len(matches) == 1, (
            f"expected exactly one _VERDICT_KEYWORDS definition; "
            f"found {len(matches)}: {matches}"
        )
        # The one definition must live in verdict_parser.py
        assert matches[0].startswith("verdict_parser.py:"), (
            f"canonical definition must be in verdict_parser.py; "
            f"got {matches[0]!r}"
        )

    def test_transitions_re_exports_canonical(self):
        """`from .transitions import _VERDICT_KEYWORDS` returns the same object
        as `from .verdict_parser import _VERDICT_KEYWORDS`."""
        from orchestration_engine import transitions, verdict_parser
        assert transitions._VERDICT_KEYWORDS is verdict_parser._VERDICT_KEYWORDS, (
            "transitions._VERDICT_KEYWORDS must be the SAME object as "
            "verdict_parser._VERDICT_KEYWORDS (re-export, not a copy)"
        )

    def test_sequencer_binds_canonical_via_transitions_re_export(self):
        """Two-hop identity check: the actual consume site (sequencer.py:3363)
        binds the canonical set object — not a copy or rebind. If a future
        refactor of transitions.py replaced the re-export with a defensive
        copy (`_VERDICT_KEYWORDS = set(verdict_parser._VERDICT_KEYWORDS)`),
        single-source-of-truth would silently break at the consume site
        while the one-hop test_transitions_re_exports_canonical still passed."""
        from orchestration_engine import sequencer, verdict_parser
        assert sequencer._VERDICT_KEYWORDS is verdict_parser._VERDICT_KEYWORDS, (
            "sequencer._VERDICT_KEYWORDS must be the SAME object as "
            "verdict_parser._VERDICT_KEYWORDS — single-source invariant "
            "must hold at the actual consume site, not just one hop in."
        )

    def test_canonical_set_contents(self):
        """The canonical set is lowercase and matches `extract_verdict()`'s
        documented output contract."""
        from orchestration_engine.verdict_parser import _VERDICT_KEYWORDS
        assert _VERDICT_KEYWORDS == {"approve", "request_changes", "abort"}
        # Every element must be lowercase
        for keyword in _VERDICT_KEYWORDS:
            assert keyword == keyword.lower()


class TestSequencerCallsiteUsesLowercase:
    """The sequencer's verdict-stripping loop must compare lowercase
    (the canonical set is lowercase). Before #836 the callsite used
    `.upper()` because the imported `_VERDICT_KEYWORDS` from transitions
    was uppercase — a latent contract drift waiting to bite."""

    def test_sequencer_source_uses_lowercase_compare(self):
        from tests.conftest import read_src

        seq = read_src("sequencer.py")
        # The canonical line stripping verdicts must do a `.lower()` compare
        # against the (now-lowercase) keyword set. The previous `.upper()` form
        # would silently miss because the canonical set is lowercase.
        assert "line.strip().lower() in _VERDICT_KEYWORDS" in seq, (
            "sequencer.py no longer uses .lower() when comparing line text "
            "against _VERDICT_KEYWORDS — the lowercase canonical set will "
            "never match an .upper()'d comparison"
        )
        assert "line.strip().upper() in _VERDICT_KEYWORDS" not in seq, (
            "sequencer.py still has the legacy .upper() comparison against "
            "the now-lowercase _VERDICT_KEYWORDS — silent miss bug"
        )

    def test_stripping_loop_recognises_each_verdict_case(self):
        """Functional proof: each canonical verdict — in any case — is treated
        as the leading verdict line and stripped from the body."""
        from orchestration_engine.verdict_parser import _VERDICT_KEYWORDS

        def strip_verdict_prefix(text: str) -> str:
            """Mirror the sequencer.py:3363 loop in isolation."""
            stripped = []
            past = False
            for line in text.split("\n"):
                if not past and line.strip().lower() in _VERDICT_KEYWORDS:
                    continue
                past = True
                stripped.append(line)
            return "\n".join(stripped)

        for verdict_canonical in ["approve", "request_changes", "abort"]:
            for casing in [
                verdict_canonical,
                verdict_canonical.upper(),
                verdict_canonical.title(),
            ]:
                body = f"{casing}\n[BLOCKER] some issue\n[NIT] another"
                stripped = strip_verdict_prefix(body)
                assert stripped == "[BLOCKER] some issue\n[NIT] another", (
                    f"verdict {casing!r} should have been stripped from prefix; "
                    f"got {stripped!r}"
                )

    def test_new_keyword_added_via_set_mutation_is_seen_at_every_hop(self):
        """The consolidation's value: adding a verdict keyword via in-place
        mutation of the canonical set is observed by every consumer (transitions
        re-export AND sequencer callsite) without ANY parallel edit. This is
        what single-source-of-truth means in practice.

        (Note: rebinding `vp._VERDICT_KEYWORDS = new_set` would NOT propagate
        through the `from … import` statements — those captured the original
        object. Set mutation is the right knob; the test enforces that mutation
        flows through both hops.)
        """
        import orchestration_engine.verdict_parser as vp
        from orchestration_engine import sequencer, transitions

        try:
            vp._VERDICT_KEYWORDS.add("deferred")
            assert "deferred" in transitions._VERDICT_KEYWORDS, (
                "transitions._VERDICT_KEYWORDS must observe in-place mutations "
                "of the canonical set (single-source invariant)"
            )
            assert "deferred" in sequencer._VERDICT_KEYWORDS, (
                "sequencer._VERDICT_KEYWORDS must observe in-place mutations "
                "of the canonical set (single-source invariant)"
            )
            # And the sequencer's strip-loop must now recognise the new keyword
            def strip_verdict_prefix(text: str) -> str:
                stripped = []
                past = False
                for line in text.split("\n"):
                    if not past and line.strip().lower() in sequencer._VERDICT_KEYWORDS:
                        continue
                    past = True
                    stripped.append(line)
                return "\n".join(stripped)

            assert strip_verdict_prefix("DEFERRED\nfinding 1") == "finding 1"
        finally:
            vp._VERDICT_KEYWORDS.discard("deferred")


class TestVerdictParserContractUnchanged:
    """The consolidation must not change the public verdict-extraction
    contract; existing callers continue to work."""

    def test_extract_verdict_returns_lowercase(self):
        from orchestration_engine.verdict_parser import extract_verdict
        assert extract_verdict("APPROVE\nLooks good.") == "approve"
        assert extract_verdict("REQUEST_CHANGES\n[BLOCKER] x") == "request_changes"
        assert extract_verdict("ABORT\nfatal") == "abort"

    def test_extract_verdict_importable_from_transitions(self):
        """transitions also re-exports extract_verdict for legacy callers."""
        from orchestration_engine.transitions import extract_verdict as via_transitions
        from orchestration_engine.verdict_parser import extract_verdict as canonical
        assert via_transitions is canonical
