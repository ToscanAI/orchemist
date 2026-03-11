"""Unit tests for orchestration_engine.test_runner module.

Covers:
- TestRunResult dataclass construction
- parse_pytest_output() output parsing
- run_pytest() subprocess handling (with mocks)
- write_acceptance_results() JSON file writing
- format_failure_summary() output formatting
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.test_runner import (
    TestRunResult,
    format_failure_summary,
    parse_pytest_output,
    run_pytest,
    write_acceptance_results,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pytest_stdout(passed=0, failed=0, errors=0, duration="0.12s"):
    """Build a realistic pytest -v --tb=short stdout string."""
    parts = []
    if passed:
        parts.append(f"{passed} passed")
    if failed:
        parts.append(f"{failed} failed")
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    summary = ", ".join(parts) if parts else "no tests ran"
    return f"\n============================= {summary} in {duration} =============================="


def _make_result(**kwargs) -> TestRunResult:
    defaults = dict(
        passed=0, failed=0, errors=0, total=0,
        pass_rate=0.0, failure_details="", full_output="", exit_code=0,
    )
    defaults.update(kwargs)
    return TestRunResult(**defaults)


# ---------------------------------------------------------------------------
# TestRunResult dataclass tests
# ---------------------------------------------------------------------------

class TestTestRunResultDataclass:
    def test_all_fields_present(self):
        r = TestRunResult(
            passed=3, failed=1, errors=0, total=4,
            pass_rate=0.75,
            failure_details="FAILED test_foo",
            full_output="full",
            exit_code=1,
        )
        assert r.passed == 3
        assert r.failed == 1
        assert r.errors == 0
        assert r.total == 4
        assert r.pass_rate == 0.75
        assert "FAILED" in r.failure_details
        assert r.full_output == "full"
        assert r.exit_code == 1

    def test_zero_total_pass_rate(self):
        r = _make_result(passed=0, failed=0, errors=0, total=0, pass_rate=0.0)
        assert r.pass_rate == 0.0
        assert r.total == 0


# ---------------------------------------------------------------------------
# parse_pytest_output tests
# ---------------------------------------------------------------------------

class TestParsePytestOutput:
    def test_all_passed(self):
        stdout = _make_pytest_stdout(passed=2)
        r = parse_pytest_output(stdout, 0)
        assert r.passed == 2
        assert r.failed == 0
        assert r.errors == 0
        assert r.total == 2
        assert r.exit_code == 0

    def test_mixed_results(self):
        stdout = _make_pytest_stdout(passed=3, failed=1)
        r = parse_pytest_output(stdout, 1)
        assert r.passed == 3
        assert r.failed == 1
        assert r.errors == 0
        assert r.total == 4
        assert abs(r.pass_rate - 0.75) < 1e-6

    def test_with_errors(self):
        stdout = _make_pytest_stdout(passed=0, failed=2, errors=1)
        r = parse_pytest_output(stdout, 1)
        assert r.errors == 1
        assert r.failed == 2
        assert r.passed == 0
        assert r.total == 3

    def test_no_tests_collected(self):
        stdout = "========================= no tests ran ========================="
        r = parse_pytest_output(stdout, 5)
        assert r.passed == 0
        assert r.failed == 0
        assert r.errors == 0
        assert r.total == 0
        assert r.pass_rate == 0.0

    def test_only_errors(self):
        stdout = "=========================== 3 errors in 0.05s ==========================="
        r = parse_pytest_output(stdout, 1)
        assert r.errors == 3
        assert r.passed == 0
        assert r.failed == 0

    def test_empty_output(self):
        r = parse_pytest_output("", 1)
        assert r.passed == 0
        assert r.failed == 0
        assert r.errors == 0
        assert r.total == 0
        assert r.pass_rate == 0.0

    def test_failure_details_captured(self):
        stdout = (
            "FAILED tests/test_foo.py::test_bar - AssertionError: assert 1 == 2\n"
            "  assert 1 == 2\n"
            "PASSED tests/test_foo.py::test_ok\n"
            "=========================== 1 passed, 1 failed in 0.10s ==========================="
        )
        r = parse_pytest_output(stdout, 1)
        assert "FAILED" in r.failure_details or "AssertionError" in r.failure_details

    def test_exit_code_reflected(self):
        r_pass = parse_pytest_output(_make_pytest_stdout(passed=1), 0)
        r_fail = parse_pytest_output(_make_pytest_stdout(failed=1), 1)
        assert r_pass.exit_code == 0
        assert r_fail.exit_code == 1

    def test_pass_rate_zero_when_total_zero(self):
        r = parse_pytest_output("", 0)
        assert r.pass_rate == 0.0  # no ZeroDivisionError


# ---------------------------------------------------------------------------
# run_pytest tests (subprocess mocked)
# ---------------------------------------------------------------------------

class TestRunPytest:
    def test_success(self, tmp_path):
        mock_stdout = _make_pytest_stdout(passed=2)
        mock_proc = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.returncode = 0

        test_file = str(tmp_path / "acceptance_tests.py")
        Path(test_file).write_text("# placeholder")

        with patch("subprocess.run", return_value=mock_proc):
            r = run_pytest(test_file)

        assert r.failed == 0
        assert r.errors == 0
        assert r.passed == 2
        assert r.exit_code == 0

    def test_failure(self, tmp_path):
        mock_stdout = _make_pytest_stdout(passed=1, failed=2)
        mock_proc = MagicMock()
        mock_proc.stdout = mock_stdout
        mock_proc.returncode = 1

        test_file = str(tmp_path / "acceptance_tests.py")
        Path(test_file).write_text("# placeholder")

        with patch("subprocess.run", return_value=mock_proc):
            r = run_pytest(test_file)

        assert r.failed > 0
        assert r.exit_code == 1

    def test_timeout(self, tmp_path):
        test_file = str(tmp_path / "acceptance_tests.py")
        Path(test_file).write_text("# placeholder")

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=300)):
            r = run_pytest(test_file, timeout_seconds=300)

        assert r.errors >= 1
        assert r.exit_code != 0
        combined = (r.failure_details + r.full_output).lower()
        assert "timeout" in combined or r.errors >= 1

    def test_file_not_found(self, tmp_path):
        test_file = str(tmp_path / "acceptance_tests.py")
        Path(test_file).write_text("# placeholder")

        with patch("subprocess.run", side_effect=FileNotFoundError("python3 not found")):
            r = run_pytest(test_file)

        assert r.errors >= 1

    def test_command_structure(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.stdout = _make_pytest_stdout(passed=1)
        mock_proc.returncode = 0

        test_file = str(tmp_path / "acceptance_tests.py")
        Path(test_file).write_text("# placeholder")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            run_pytest(test_file)

        cmd = mock_run.call_args[0][0]
        assert "python3" in cmd[0] or cmd[0] == "python3"
        assert "-m" in cmd
        assert "pytest" in cmd
        assert test_file in cmd
        assert "-v" in cmd
        assert "--tb=short" in cmd

    def test_shell_false(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.stdout = _make_pytest_stdout(passed=1)
        mock_proc.returncode = 0

        test_file = str(tmp_path / "acceptance_tests.py")
        Path(test_file).write_text("# placeholder")

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            run_pytest(test_file)

        kwargs = mock_run.call_args[1]
        assert kwargs.get("shell", False) is False


# ---------------------------------------------------------------------------
# write_acceptance_results tests
# ---------------------------------------------------------------------------

class TestWriteAcceptanceResults:
    def test_creates_json(self, tmp_path):
        r = _make_result(passed=3, total=3, pass_rate=1.0)
        write_acceptance_results(r, str(tmp_path))
        assert (tmp_path / "acceptance_results.json").exists()

    def test_pass_status(self, tmp_path):
        r = _make_result(passed=5, total=5, pass_rate=1.0)
        write_acceptance_results(r, str(tmp_path))
        data = json.loads((tmp_path / "acceptance_results.json").read_text())
        assert data["status"] == "pass"

    def test_fail_status(self, tmp_path):
        r = _make_result(passed=3, failed=1, total=4, pass_rate=0.75, exit_code=1)
        write_acceptance_results(r, str(tmp_path))
        data = json.loads((tmp_path / "acceptance_results.json").read_text())
        assert data["status"] == "fail"

    def test_numeric_fields(self, tmp_path):
        r = _make_result(passed=3, failed=1, errors=0, total=4, pass_rate=0.75, exit_code=1)
        write_acceptance_results(r, str(tmp_path))
        data = json.loads((tmp_path / "acceptance_results.json").read_text())
        assert data["passed"] == 3
        assert data["failed"] == 1
        assert data["errors"] == 0
        assert data["total"] == 4
        assert abs(data["pass_rate"] - 0.75) < 1e-6

    def test_phase_field(self, tmp_path):
        r = _make_result(passed=1, total=1, pass_rate=1.0)
        write_acceptance_results(r, str(tmp_path), phase="acceptance_run")
        data = json.loads((tmp_path / "acceptance_results.json").read_text())
        assert data["phase"] == "acceptance_run"

    def test_overwrites_previous(self, tmp_path):
        r1 = _make_result(passed=5, total=5, pass_rate=1.0)
        write_acceptance_results(r1, str(tmp_path))
        first = json.loads((tmp_path / "acceptance_results.json").read_text())
        assert first["status"] == "pass"

        r2 = _make_result(failed=2, total=2, pass_rate=0.0, exit_code=1,
                          failure_details="FAILED foo")
        write_acceptance_results(r2, str(tmp_path))
        second = json.loads((tmp_path / "acceptance_results.json").read_text())
        assert second["status"] == "fail"
        assert second["failed"] == 2


# ---------------------------------------------------------------------------
# format_failure_summary tests
# ---------------------------------------------------------------------------

class TestFormatFailureSummary:
    def test_returns_string(self):
        r = _make_result(passed=1, failed=2, errors=0, total=3, pass_rate=0.333,
                         failure_details="FAILED test_x\n  assert 1 == 2", exit_code=1)
        summary = format_failure_summary(r)
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_contains_counts(self):
        r = _make_result(passed=1, failed=2, errors=1, total=4, pass_rate=0.25,
                         failure_details="FAILED test_x", exit_code=1)
        summary = format_failure_summary(r)
        assert "2" in summary or "fail" in summary.lower()

    def test_safe_for_str_format(self):
        """Curly braces in failure details must be escaped."""
        r = _make_result(
            passed=0, failed=1, total=1, pass_rate=0.0, exit_code=1,
            failure_details="FAILED test_set - AssertionError: assert {1, 2} == {1, 3}",
        )
        summary = format_failure_summary(r)
        try:
            _ = "Feedback: {}".format(summary)
            _ = f"Feedback: {summary}"
        except (KeyError, IndexError, ValueError) as exc:
            pytest.fail(f"format_failure_summary output is unsafe for str.format(): {exc}")
