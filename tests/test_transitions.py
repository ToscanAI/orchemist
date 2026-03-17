"""Tests for extract_verdict() priority-ordering behavior (Issue #600)."""

import pytest
from orchestration_engine.transitions import extract_verdict


class TestExtractVerdictPriority:
    # -- Core priority tests -----------------------------------------------

    def test_request_changes_beats_approve_on_first_line(self):
        """The real-world bug: reasoning starts with APPROVE, verdict is REQUEST_CHANGES."""
        text = "APPROVE would be premature\n...\nREQUEST_CHANGES\n[BLOCKER] fix it"
        assert extract_verdict(text) == "request_changes"

    def test_approve_only(self):
        assert extract_verdict("APPROVE\nCode looks good") == "approve"

    def test_request_changes_only(self):
        assert extract_verdict("REQUEST_CHANGES\nFix the bug") == "request_changes"

    def test_abort_only(self):
        assert extract_verdict("ABORT\nCritical issue") == "abort"

    def test_all_three_returns_request_changes(self):
        text = "APPROVE on some things\nABORT maybe\nREQUEST_CHANGES is the right call"
        assert extract_verdict(text) == "request_changes"

    def test_abort_beats_approve(self):
        text = "APPROVE the good parts\nABORT due to blocker"
        assert extract_verdict(text) == "abort"

    def test_request_changes_first_and_approve_second(self):
        text = "REQUEST_CHANGES\nfix it\nAPPROVE later"
        assert extract_verdict(text) == "request_changes"

    # -- Case insensitivity ------------------------------------------------

    def test_lowercase_approve(self):
        assert extract_verdict("approve: looks good") == "approve"

    def test_lowercase_request_changes(self):
        assert extract_verdict("request_changes: fix auth") == "request_changes"

    def test_lowercase_abort(self):
        assert extract_verdict("abort: cannot proceed") == "abort"

    def test_mixed_case_request_changes_beats_uppercase_approve(self):
        text = "APPROVE would be premature\nRequest_Changes: fix it"
        assert extract_verdict(text) == "request_changes"

    # -- Edge cases -------------------------------------------------------

    def test_empty_returns_none(self):
        assert extract_verdict("") is None

    def test_whitespace_only_returns_none(self):
        assert extract_verdict("\n\n  \n") is None

    def test_no_verdict_returns_none(self):
        assert extract_verdict("No verdict here at all.") is None

    def test_mid_sentence_keyword_not_matched(self):
        """Keywords not at line start are ignored."""
        text = "This is not worth an APPROVE because REQUEST_CHANGES mid-sentence"
        assert extract_verdict(text) is None

    def test_spec_adversary_first_line_preserved(self):
        """Well-formatted output (verdict on line 1) still works correctly."""
        assert extract_verdict("REQUEST_CHANGES\n[BLOCKER] fix X") == "request_changes"
        assert extract_verdict("APPROVE\nLooks good") == "approve"
        assert extract_verdict("ABORT\nFatal issue") == "abort"
