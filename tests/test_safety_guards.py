"""Tests for SafetyGuard — loop prevention + exclusions.

Covers:
  - TestSafetyGuardMaxAttempts      (5 tests)
  - TestSafetyGuardExcludedTypes    (9 tests)
  - TestSafetyGuardFlakyDetection   (12 tests)
  - TestSafetyGuardReturnType       (5 tests)
  - TestSafetyGuardCheckOrdering    (2 tests)
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from dataclasses import dataclass, field
from typing import Optional

from orchestration_engine.regression import SafetyGuard, RegressionStatus


# ---------------------------------------------------------------------------
# Helper: mock Regression
# ---------------------------------------------------------------------------

@dataclass
class MockRegression:
    """Minimal stand-in for Regression, parameterised per test."""
    id: str = "reg-001"
    commit_sha: str = "abc123"
    ci_run_url: str = "https://ci.example.com/run/1"
    failure_type: str = "test_failure"
    fix_attempt_count: int = 0
    diagnosis: Optional[str] = None
    fix_run_id: Optional[str] = None


def make_regression(
    *,
    id: str = "reg-001",
    commit_sha: str = "abc123",
    failure_type: str = "test_failure",
    fix_attempt_count: int = 0,
    diagnosis: Optional[str] = None,
    fix_run_id: Optional[str] = None,
) -> MockRegression:
    """Factory for mock Regression objects."""
    return MockRegression(
        id=id,
        commit_sha=commit_sha,
        failure_type=failure_type,
        fix_attempt_count=fix_attempt_count,
        diagnosis=diagnosis,
        fix_run_id=fix_run_id,
    )


def make_db_with_records(records=None):
    """Return a MagicMock db whose list_regressions yields records."""
    db = MagicMock()
    db.list_regressions.return_value = records or []
    return db


# ---------------------------------------------------------------------------
# TestSafetyGuardMaxAttempts
# ---------------------------------------------------------------------------

class TestSafetyGuardMaxAttempts(unittest.TestCase):

    def setUp(self):
        self.guard = SafetyGuard()

    def test_blocks_at_exactly_max_attempts(self):
        """Exactly at MAX_FIX_ATTEMPTS (3) → blocked."""
        reg = make_regression(fix_attempt_count=3)
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("max fix attempts reached", reason)

    def test_blocks_above_max_attempts(self):
        """Above MAX_FIX_ATTEMPTS (e.g. 10) → blocked."""
        reg = make_regression(fix_attempt_count=10)
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("max fix attempts reached", reason)

    def test_allows_zero_attempts(self):
        """0 attempts → allowed (below threshold)."""
        reg = make_regression(fix_attempt_count=0)
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertTrue(allowed)

    def test_allows_two_attempts(self):
        """2 attempts → still below limit of 3, allowed."""
        reg = make_regression(fix_attempt_count=2)
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertTrue(allowed)

    def test_custom_max_attempts(self):
        """Custom max_attempts=1: blocks at 1, allows 0."""
        guard = SafetyGuard(max_attempts=1)
        reg_blocked = make_regression(fix_attempt_count=1)
        reg_allowed = make_regression(fix_attempt_count=0)
        blocked, reason = guard.should_attempt_fix(reg_blocked, db=None)
        allowed, _ = guard.should_attempt_fix(reg_allowed, db=None)
        self.assertFalse(blocked)
        self.assertIn("1", reason)
        self.assertTrue(allowed)


# ---------------------------------------------------------------------------
# TestSafetyGuardExcludedTypes
# ---------------------------------------------------------------------------

class TestSafetyGuardExcludedTypes(unittest.TestCase):

    def setUp(self):
        self.guard = SafetyGuard()

    def _check_excluded(self, failure_type: str):
        reg = make_regression(failure_type=failure_type)
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed, f"Expected {failure_type!r} to be blocked")
        self.assertIn("excluded failure type", reason)

    def test_dependency_failure_blocked(self):
        self._check_excluded("dependency_failure")

    def test_infra_failure_blocked(self):
        self._check_excluded("infra_failure")

    def test_infrastructure_failure_blocked(self):
        self._check_excluded("infrastructure_failure")

    def test_network_timeout_blocked(self):
        self._check_excluded("network_timeout")

    def test_oom_kill_blocked(self):
        self._check_excluded("oom_kill")

    def test_out_of_memory_blocked(self):
        self._check_excluded("out_of_memory")

    def test_secret_missing_blocked(self):
        self._check_excluded("secret_missing")

    def test_env_misconfiguration_blocked(self):
        self._check_excluded("env_misconfiguration")

    def test_third_party_outage_blocked(self):
        self._check_excluded("third_party_outage")

    def test_test_failure_allowed(self):
        """test_failure is not excluded → allowed."""
        reg = make_regression(failure_type="test_failure")
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertTrue(allowed)

    def test_build_error_allowed(self):
        """build_error is not excluded → allowed."""
        reg = make_regression(failure_type="build_error")
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertTrue(allowed)

    def test_case_insensitive_match(self):
        """Exclusion check is case-insensitive (OOM_KILL should be blocked)."""
        reg = make_regression(failure_type="OOM_KILL")
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("excluded failure type", reason)

    def test_custom_excluded_types(self):
        """Custom excluded_failure_types override defaults."""
        guard = SafetyGuard(excluded_failure_types=frozenset({"custom_infra"}))
        blocked_reg = make_regression(failure_type="custom_infra")
        allowed_reg = make_regression(failure_type="dependency_failure")  # not in custom set
        b_allowed, b_reason = guard.should_attempt_fix(blocked_reg, db=None)
        a_allowed, _ = guard.should_attempt_fix(allowed_reg, db=None)
        self.assertFalse(b_allowed)
        self.assertIn("excluded failure type", b_reason)
        self.assertTrue(a_allowed)


# ---------------------------------------------------------------------------
# TestSafetyGuardFlakyDetection
# ---------------------------------------------------------------------------

class TestSafetyGuardFlakyDetection(unittest.TestCase):

    def setUp(self):
        self.guard = SafetyGuard()

    def test_flaky_keyword_in_failure_type(self):
        """'flaky' in failure_type → blocked."""
        reg = make_regression(failure_type="flaky_test")
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("flaky", reason)

    def test_intermittent_keyword_in_failure_type(self):
        """'intermittent' in failure_type → blocked."""
        reg = make_regression(failure_type="intermittent_failure")
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("intermittent", reason)

    def test_flaky_keyword_in_diagnosis(self):
        """'flaky' keyword in diagnosis field → blocked."""
        reg = make_regression(failure_type="test_failure", diagnosis="This is a flaky test.")
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("flaky", reason)

    def test_race_condition_keyword(self):
        """'race condition' in diagnosis → blocked."""
        reg = make_regression(failure_type="test_failure", diagnosis="looks like a race condition here")
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("race condition", reason)

    def test_case_insensitive_flaky_keyword(self):
        """Keyword match is case-insensitive (FLAKY → blocked)."""
        reg = make_regression(failure_type="FLAKY_TEST")
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("flaky", reason.lower())

    def test_no_keywords_allows(self):
        """No flaky keywords present → rule 3 passes."""
        reg = make_regression(failure_type="test_failure", diagnosis="NullPointerException in Foo.bar")
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertTrue(allowed)

    def test_db_oscillation_blocks(self):
        """DB record: same commit, different id, status=FIXED, no fix_run_id → flaky."""
        records = [{
            "commit_sha": "abc123",
            "id": "reg-OTHER",
            "status": RegressionStatus.FIXED.value,
            "fix_run_id": None,
        }]
        db = make_db_with_records(records)
        reg = make_regression(id="reg-001", commit_sha="abc123")
        allowed, reason = self.guard.should_attempt_fix(reg, db=db)
        self.assertFalse(allowed)
        self.assertIn("self-healed", reason)

    def test_same_id_ignored_in_db(self):
        """DB record with same regression id → not counted as oscillation."""
        records = [{
            "commit_sha": "abc123",
            "id": "reg-001",   # same id as current
            "status": RegressionStatus.FIXED.value,
            "fix_run_id": None,
        }]
        db = make_db_with_records(records)
        reg = make_regression(id="reg-001", commit_sha="abc123")
        allowed, _ = self.guard.should_attempt_fix(reg, db=db)
        self.assertTrue(allowed)

    def test_fixed_with_fix_run_id_not_flaky(self):
        """FIXED record that has a fix_run_id (was actually fixed) → not oscillation."""
        records = [{
            "commit_sha": "abc123",
            "id": "reg-OTHER",
            "status": RegressionStatus.FIXED.value,
            "fix_run_id": "run-xyz",   # real fix applied
        }]
        db = make_db_with_records(records)
        reg = make_regression(id="reg-001", commit_sha="abc123")
        allowed, _ = self.guard.should_attempt_fix(reg, db=db)
        self.assertTrue(allowed)

    def test_db_none_skips_oscillation_check(self):
        """db=None skips DB oscillation check entirely."""
        reg = make_regression(failure_type="test_failure")
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertTrue(allowed)

    def test_db_exception_does_not_block(self):
        """DB query raising an exception → guard logs warning but does not block."""
        db = MagicMock()
        db.list_regressions.side_effect = RuntimeError("DB is down")
        reg = make_regression(failure_type="test_failure")
        # Should not raise; should allow the fix (fail-safe open)
        allowed, _ = self.guard.should_attempt_fix(reg, db=db)
        self.assertTrue(allowed)

    def test_custom_flaky_keywords(self):
        """Custom flaky_keywords override default set."""
        guard = SafetyGuard(flaky_keywords=frozenset({"unreliable"}))
        blocked_reg = make_regression(failure_type="unreliable_test")
        allowed_reg = make_regression(failure_type="flaky_test")  # 'flaky' NOT in custom set
        b_allowed, b_reason = guard.should_attempt_fix(blocked_reg, db=None)
        a_allowed, _ = guard.should_attempt_fix(allowed_reg, db=None)
        self.assertFalse(b_allowed)
        self.assertIn("unreliable", b_reason)
        self.assertTrue(a_allowed)


# ---------------------------------------------------------------------------
# TestSafetyGuardReturnType
# ---------------------------------------------------------------------------

class TestSafetyGuardReturnType(unittest.TestCase):

    def setUp(self):
        self.guard = SafetyGuard()

    def test_returns_tuple(self):
        """should_attempt_fix always returns a 2-tuple."""
        reg = make_regression()
        result = self.guard.should_attempt_fix(reg, db=None)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_first_element_bool_true(self):
        """First element is bool True when allowed."""
        reg = make_regression(fix_attempt_count=0, failure_type="test_failure")
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertIs(type(allowed), bool)
        self.assertTrue(allowed)

    def test_first_element_bool_false(self):
        """First element is bool False when blocked."""
        reg = make_regression(fix_attempt_count=3)
        allowed, _ = self.guard.should_attempt_fix(reg, db=None)
        self.assertIs(type(allowed), bool)
        self.assertFalse(allowed)

    def test_second_element_is_string(self):
        """Second element (reason) is always a str."""
        reg = make_regression()
        _, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertIsInstance(reason, str)

    def test_allow_reason_text(self):
        """When allowed, reason contains 'safe to attempt fix'."""
        reg = make_regression()
        _, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertIn("safe to attempt fix", reason)


# ---------------------------------------------------------------------------
# TestSafetyGuardCheckOrdering
# ---------------------------------------------------------------------------

class TestSafetyGuardCheckOrdering(unittest.TestCase):

    def setUp(self):
        self.guard = SafetyGuard()

    def test_max_attempts_checked_before_exclusion(self):
        """Max-attempts rule fires even when failure_type is excluded."""
        # Both rules would block: pick the one that comes first (max attempts)
        reg = make_regression(
            fix_attempt_count=3,          # triggers rule 1
            failure_type="infra_failure", # triggers rule 2
        )
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("max fix attempts reached", reason)

    def test_exclusion_checked_before_flaky(self):
        """Exclusion rule fires before flaky detection."""
        # failure_type excluded AND contains a flaky keyword
        reg = make_regression(
            fix_attempt_count=0,
            failure_type="infra_failure",
            diagnosis="intermittent network error",  # would trigger flaky
        )
        allowed, reason = self.guard.should_attempt_fix(reg, db=None)
        self.assertFalse(allowed)
        self.assertIn("excluded failure type", reason)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
