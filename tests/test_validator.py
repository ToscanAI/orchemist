"""Unit tests for orchestrator-side ExternalValidator lifecycle (validator.py).

Tests focus on the lifecycle methods (spawn, validate, shutdown) without
running a real subprocess wherever possible.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.errors import OrchestratorError
from orchestration_engine.validator import (
    ExternalValidator,
    ValidationRequest,
    ValidatorError,
)


class TestValidatorErrorInheritance:
    """ValidatorError must subclass OrchestratorError."""

    def test_is_orchestrator_error(self):
        assert issubclass(ValidatorError, OrchestratorError)

    def test_can_be_raised_and_caught_as_orchestrator_error(self):
        with pytest.raises(OrchestratorError):
            raise ValidatorError("test error")


class TestValidateBeforeSpawn:
    """Calling validate() before spawn() must raise ValidatorError('not spawned')."""

    def test_validate_raises_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            validator = ExternalValidator(test_store_path=tmp)
            request = ValidationRequest(
                run_id="test-run",
                repo_path=tmp,
                test_store_path=tmp,
                test_file_hash="dummy",
            )
            with pytest.raises(ValidatorError) as exc_info:
                validator.validate(request)
            assert "not spawned" in str(exc_info.value).lower()


class TestSpawnFailure:
    """spawn() must raise ValidatorError('failed to spawn') on subprocess failure."""

    def test_spawn_raises_on_file_not_found(self, monkeypatch):
        def bad_popen(*args, **kwargs):
            raise FileNotFoundError("no such binary")

        monkeypatch.setattr(subprocess, "Popen", bad_popen)

        with tempfile.TemporaryDirectory() as tmp:
            validator = ExternalValidator(test_store_path=tmp)
            with pytest.raises(ValidatorError) as exc_info:
                validator.spawn()
            assert "failed to spawn" in str(exc_info.value).lower()

    def test_spawn_raises_on_os_error(self, monkeypatch):
        def bad_popen(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(subprocess, "Popen", bad_popen)

        with tempfile.TemporaryDirectory() as tmp:
            validator = ExternalValidator(test_store_path=tmp)
            with pytest.raises(ValidatorError) as exc_info:
                validator.spawn()
            assert "failed to spawn" in str(exc_info.value).lower()


class TestShutdownIdempotent:
    """shutdown() must be safe to call multiple times and on un-spawned validators."""

    def test_shutdown_before_spawn_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            validator = ExternalValidator(test_store_path=tmp)
            # Should not raise
            validator.shutdown()
            validator.shutdown()


class TestValidationRequest:
    """ValidationRequest dataclass contract."""

    def test_default_timeout_is_300(self):
        request = ValidationRequest(
            run_id="run",
            repo_path="/tmp",
            test_store_path="/tmp",
            test_file_hash="abc",
        )
        assert request.timeout_seconds == 300

    def test_custom_timeout(self):
        request = ValidationRequest(
            run_id="run",
            repo_path="/tmp",
            test_store_path="/tmp",
            test_file_hash="abc",
            timeout_seconds=60,
        )
        assert request.timeout_seconds == 60

    def test_all_required_fields(self):
        request = ValidationRequest(
            run_id="run-123",
            repo_path="/some/path",
            test_store_path="/store/path",
            test_file_hash="deadbeef" * 8,
        )
        assert request.run_id == "run-123"
        assert request.repo_path == "/some/path"
        assert request.test_store_path == "/store/path"
        assert request.test_file_hash == "deadbeef" * 8
