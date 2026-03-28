"""Integration tests for validator_runner.py subprocess.

Tests run the validator_runner as a real subprocess and verify end-to-end
JSON-RPC communication.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from orchestration_engine.ipc import (
    HealthRequest,
    HealthResult,
    ValidationRequest,
    ValidationResult,
    deserialize_response,
    serialize_request,
)
from orchestration_engine.test_store import TestStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_runner() -> subprocess.Popen:
    """Start the validator_runner subprocess."""
    return subprocess.Popen(
        [sys.executable, "-m", "orchestration_engine.validator_runner"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _rpc(proc: subprocess.Popen, request) -> dict:
    """Send a request and read one response line."""
    line = serialize_request(request)
    proc.stdin.write(line.encode("utf-8"))
    proc.stdin.flush()
    raw = proc.stdout.readline()
    return json.loads(raw.decode("utf-8"))


def _seal(store_root: Path, run_id: str, content: str) -> str:
    """Create a test file and seal it; return its SHA-256 hash."""
    test_file = store_root.parent / f"{run_id}_test.py"
    test_file.write_text(content)
    ts = TestStore(store_root=store_root)
    manifest = ts.seal_tests(
        run_id=run_id,
        test_file_path=str(test_file),
        spec_hash="dummy",
    )
    return manifest.test_file_hash


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """validator_runner responds to health pings."""

    def test_health_returns_ok(self):
        proc = _start_runner()
        try:
            resp = _rpc(proc, HealthRequest())
            assert resp["result"]["status"] == "ok"
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


class TestValidationPassingTests:
    """Happy path: all tests pass → verdict PASS."""

    def test_all_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            store_root.mkdir()

            content = "def test_always_passes():\n    assert True\n"
            file_hash = _seal(store_root, "run-pass", content)

            proc = _start_runner()
            try:
                request = ValidationRequest(
                    run_id="run-pass",
                    test_store_path=str(store_root),
                    repo_path=str(Path("/home/toscan/orchestration-engine")),
                    branch="",
                    test_manifest_hash=file_hash,
                )
                resp = _rpc(proc, request)
                result = resp["result"]
                assert result["verdict"] == "PASS"
                assert result["pass_rate"] == 1.0
                assert result["retry_recommended"] is False
            finally:
                proc.stdin.close()
                proc.wait(timeout=30)


class TestValidationFailingTests:
    """Failing tests → verdict FAIL."""

    def test_some_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            store_root.mkdir()

            content = textwrap.dedent("""\
                def test_passes():
                    assert True

                def test_fails():
                    assert False, "intentional"
            """)
            file_hash = _seal(store_root, "run-fail", content)

            proc = _start_runner()
            try:
                request = ValidationRequest(
                    run_id="run-fail",
                    test_store_path=str(store_root),
                    repo_path=str(Path("/home/toscan/orchestration-engine")),
                    branch="",
                    test_manifest_hash=file_hash,
                )
                resp = _rpc(proc, request)
                result = resp["result"]
                assert result["verdict"] == "FAIL"
                assert result["retry_recommended"] is True
            finally:
                proc.stdin.close()
                proc.wait(timeout=30)


class TestValidationHashMismatch:
    """Hash mismatch → verdict ERROR with integrity message."""

    def test_wrong_hash_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            store_root.mkdir()

            content = "def test_pass():\n    assert True\n"
            _seal(store_root, "run-hash", content)
            wrong_hash = "0" * 64

            proc = _start_runner()
            try:
                request = ValidationRequest(
                    run_id="run-hash",
                    test_store_path=str(store_root),
                    repo_path=str(Path("/home/toscan/orchestration-engine")),
                    branch="",
                    test_manifest_hash=wrong_hash,
                )
                resp = _rpc(proc, request)
                result = resp["result"]
                assert result["verdict"] == "ERROR"
                # Check details for integrity message
                details_text = json.dumps(result.get("details", [])).lower()
                assert any(
                    kw in details_text or kw in result.get("retry_reason", "").lower()
                    for kw in ["integrity", "hash", "mismatch"]
                )
            finally:
                proc.stdin.close()
                proc.wait(timeout=30)


class TestEOFShutdown:
    """Closing stdin causes the subprocess to exit cleanly."""

    def test_eof_causes_exit(self):
        proc = _start_runner()
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("validator_runner did not exit after stdin EOF")


class TestForbiddenImports:
    """validator_runner.py must not import forbidden modules."""

    def test_no_forbidden_imports(self):
        import ast

        runner_path = Path("/home/toscan/orchestration-engine/src/orchestration_engine/validator_runner.py")
        assert runner_path.exists(), "validator_runner.py must exist"

        source = runner_path.read_text()
        tree = ast.parse(source)

        forbidden = {"validator", "sequencer", "daemon", "errors"}
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                for part in parts:
                    if part in forbidden:
                        violations.append(node.module)
                        break
        assert not violations, f"Found forbidden imports: {violations}"
