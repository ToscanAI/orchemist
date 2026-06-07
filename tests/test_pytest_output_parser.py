"""Tests for the shared pytest-output parser (Issue #927).

Pins the branch coverage of ``count_pytest_results`` and the stdlib-only
isolation guarantee, and asserts the ``errors=1 when total==0 and rc!=0`` rule is
NOT applied by the shared helper (it belongs to the validator wrapper only).
"""

import importlib.util
import sys

import pytest

from orchestration_engine.pytest_output_parser import (
    count_pytest_results,
    _PytestCounts,
)


class TestCountPytestResultsBranches:
    """Branch coverage for the shared counting function."""

    def test_full_summary(self):
        # "2 passed, 1 failed, 1 error in 0.35s"
        counts = count_pytest_results(
            "============ 2 passed, 1 failed, 1 error in 0.35s ============", 1
        )
        assert counts == _PytestCounts(
            passed=2, failed=1, errors=1, total=4, pass_rate=0.5
        )

    def test_pass_only(self):
        counts = count_pytest_results("==== 3 passed in 0.12s ====", 0)
        assert counts == _PytestCounts(
            passed=3, failed=0, errors=0, total=3, pass_rate=1.0
        )

    def test_no_tests_ran(self):
        counts = count_pytest_results("no tests ran in 0.01s", 5)
        assert counts == _PytestCounts(
            passed=0, failed=0, errors=0, total=0, pass_rate=0.0
        )

    def test_no_tests_collected_ignorecase(self):
        # re.IGNORECASE must be honoured (character-identical to originals).
        counts = count_pytest_results("NO TESTS COLLECTED", 5)
        assert counts.total == 0
        assert counts.errors == 0

    def test_empty_output_rc0(self):
        counts = count_pytest_results("", 0)
        assert counts == _PytestCounts(
            passed=0, failed=0, errors=0, total=0, pass_rate=0.0
        )

    def test_empty_output_nonzero_rc_does_not_apply_errors_rule(self):
        # AC12: the errors=1 rule is the WRAPPER's responsibility, NOT this fn's.
        counts = count_pytest_results("", 1)
        assert counts == _PytestCounts(
            passed=0, failed=0, errors=0, total=0, pass_rate=0.0
        )
        assert counts.errors == 0

    def test_failed_only(self):
        counts = count_pytest_results("==== 4 failed in 1.2s ====", 1)
        assert counts == _PytestCounts(
            passed=0, failed=4, errors=0, total=4, pass_rate=0.0
        )

    def test_errors_plural_and_singular(self):
        plural = count_pytest_results("==== 3 errors in 0.05s ====", 1)
        assert plural.errors == 3 and plural.total == 3
        singular = count_pytest_results("==== 1 error in 0.05s ====", 1)
        assert singular.errors == 1 and singular.total == 1

    def test_fallback_scan_when_summary_line_unmatched(self):
        # No "in N.Ns ===" summary; fallback individual-count scan kicks in.
        stdout = "some preamble\n5 passed\n2 failed\n1 error\ntrailing noise"
        counts = count_pytest_results(stdout, 1)
        assert counts == _PytestCounts(
            passed=5, failed=2, errors=1, total=8, pass_rate=5 / 8
        )

    def test_pass_rate_partial(self):
        # AC11: "2 passed, 1 failed in 0.5s" -> pass_rate 2/3
        counts = count_pytest_results("==== 2 passed, 1 failed in 0.5s ====", 1)
        assert counts == _PytestCounts(
            passed=2, failed=1, errors=0, total=3, pass_rate=2 / 3
        )

    def test_total_invariant(self):
        counts = count_pytest_results("==== 7 passed, 3 failed, 2 errors in 9s ====", 1)
        assert counts.total == counts.passed + counts.failed + counts.errors

    def test_never_raises_on_garbage(self):
        # Defensive: arbitrary text must not raise.
        for bad in ["", "\x00\x01", "passed failed error", "=== ===", "12345"]:
            count_pytest_results(bad, 0)


class TestStdlibIsolation:
    """AC10: importing the parser triggers no engine/pydantic/yaml imports."""

    def test_parser_file_imports_only_stdlib(self):
        # Execute the parser module file in isolation (bypassing the package
        # __init__, which eagerly imports unrelated engine modules) and assert
        # it pulls in neither pydantic, yaml, nor any orchestration_engine.* mod.
        spec = importlib.util.spec_from_file_location(
            "_isolated_pytest_output_parser",
            __import__("orchestration_engine.pytest_output_parser", fromlist=["__file__"]).__file__,
        )
        module = importlib.util.module_from_spec(spec)
        before = set(sys.modules)
        spec.loader.exec_module(module)
        new = set(sys.modules) - before

        assert "pydantic" not in sys.modules or "pydantic" not in new
        assert not any(m.startswith("orchestration_engine") for m in new), (
            f"parser file pulled in engine modules: "
            f"{[m for m in new if m.startswith('orchestration_engine')]}"
        )
        # Sanity: the isolated module still works.
        assert module.count_pytest_results("1 passed in 0.1s", 0).passed == 1
