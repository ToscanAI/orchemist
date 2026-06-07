"""Tests for GeminiCliExecutor, focused on the #927 ``started_at`` fix.

Before #927, ``GeminiCliExecutor.execute`` captured ``start_time = time.time()``
(a float) but set ``started_at=datetime.now()`` at result-construction time —
i.e. AFTER the subprocess finished. These tests pin the corrected behaviour:
``started_at`` is a ``datetime`` captured at execute() ENTRY, and all
``elapsed``/duration math (including the ``TimeoutExpired`` handler that the
adversary flagged at line ~139) computes without a ``float - datetime``
``TypeError``.
"""

import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from orchestration_engine.executors.gemini_cli_executor import (
    GeminiCliExecutor,
    GeminiCliError,
)
from orchestration_engine.schemas import TaskSpec, TaskType, TaskState


@pytest.fixture
def executor():
    return GeminiCliExecutor()


@pytest.fixture
def task():
    return TaskSpec(type=TaskType.REVIEW, payload={"prompt": "say hi"})


def _fake_completed(stdout="hello from gemini", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestStartedAtCapturedAtEntry:
    """T1 / AC6: started_at reflects entry time, not subprocess-exit time."""

    def test_success_started_at_is_at_entry(self, executor, task, monkeypatch):
        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.shutil.which",
            lambda x: "/usr/bin/gemini",
        )
        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.subprocess.run",
            lambda *a, **k: _fake_completed(),
        )

        time_before = datetime.now()
        result = executor.execute(task, worker_id="w1")
        time_after = datetime.now()

        assert result.state == TaskState.SUCCESS
        assert isinstance(result.started_at, datetime)
        # started_at must fall in [time_before, time_after] — i.e. captured at
        # entry, before the (instant, mocked) subprocess "ran".
        assert time_before <= result.started_at <= time_after
        # And it must precede or equal completion.
        assert result.started_at <= result.completed_at

    def test_started_at_precedes_completed_at_with_delay(self, executor, task, monkeypatch):
        # Simulate a subprocess that takes measurable wall-clock time; started_at
        # must still be the (earlier) entry time, strictly before completed_at.
        import time as _t

        def _slow_run(*a, **k):
            _t.sleep(0.05)
            return _fake_completed()

        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.shutil.which",
            lambda x: "/usr/bin/gemini",
        )
        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.subprocess.run",
            _slow_run,
        )

        result = executor.execute(task, worker_id="w1")
        assert result.started_at < result.completed_at
        assert result.execution_time_seconds >= 0.04

    def test_failed_result_started_at_is_at_entry(self, executor, monkeypatch):
        # Empty-prompt early-exit path goes through _failed_result; its started_at
        # must also be the entry datetime (the helper now takes a datetime).
        empty_task = TaskSpec(type=TaskType.REVIEW, payload={"prompt": ""})

        time_before = datetime.now()
        result = executor.execute(empty_task, worker_id="w1")
        time_after = datetime.now()

        assert result.state == TaskState.FAILED
        assert result.errors[0].code == "empty_prompt"
        assert isinstance(result.started_at, datetime)
        assert time_before <= result.started_at <= time_after
        assert result.started_at <= result.completed_at


class TestTimeoutHandlerNoTypeError:
    """Adversary line-139 guard: the TimeoutExpired handler must not raise
    TypeError (float - datetime) before re-raising TimeoutError."""

    def test_timeout_raises_timeouterror_not_typeerror(self, executor, task, monkeypatch):
        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.shutil.which",
            lambda x: "/usr/bin/gemini",
        )

        def _raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="gemini", timeout=1, output="partial", stderr="boom")

        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.subprocess.run",
            _raise_timeout,
        )

        # Must surface as TimeoutError (the elapsed computation on the now-datetime
        # start_time must not blow up with a TypeError first).
        with pytest.raises(TimeoutError):
            executor.execute(task, worker_id="w1")


class TestNonZeroExitStillRaises:
    """Behaviour-preserving: non-zero exit still raises GeminiCliError."""

    def test_nonzero_exit_raises(self, executor, task, monkeypatch):
        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.shutil.which",
            lambda x: "/usr/bin/gemini",
        )
        monkeypatch.setattr(
            "orchestration_engine.executors.gemini_cli_executor.subprocess.run",
            lambda *a, **k: _fake_completed(stdout="", stderr="kaboom", returncode=2),
        )
        with pytest.raises(GeminiCliError):
            executor.execute(task, worker_id="w1")
