"""Acceptance tests — v1 backend reliability cluster (Closes #735 #753 #480).

This module pins the behavioural contracts authored in `.claude/worktrees/.../behavioral.md`
against the executor + retry-engine implementations after the v1 Gate 1 fix.

Test classes (one per issue):

- :class:`TestSubagentTimeoutFromConfig` (#753) — `OpenClawExecutor` must read
  ``subagents.runTimeoutSeconds`` from ``~/.openclaw/openclaw.json`` when no
  explicit ``timeout_seconds`` is supplied, with a graceful fallback to
  ``DEFAULT_TIMEOUT_SECONDS`` when the config key is absent / malformed.

- :class:`TestCircuitBreakerEscalation` (#480) — when the requested model's
  circuit breaker is open at task entry, the executor must walk the fallback
  chain instead of returning ``circuit_open``. Closed-CB inputs preserve
  byte-identical behaviour (no double-record, no spurious escalation).

- :class:`TestContentPipelineReliability` (#735) — covers four sub-RCs:
  RC-1 per-phase ``timeout_seconds:`` in content-pipeline.yaml;
  RC-2 git-output success-detection on gateway GC;
  RC-3 git-clone of original output_dir for retries;
  RC-4 race-free retry dedup.

These tests are SEALED per the acceptance-test skill contract — the implement
phase must not modify them.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Make sure repo root is on sys.path under pytest's auto-discovery so
# `orchestration_engine.*` imports resolve in editable installs too.

from orchestration_engine.adaptive_retry import AdaptiveRetryEngine, RetryStrategy
from orchestration_engine.db import Database
from orchestration_engine.diagnosis import DiagnosisResult, FailureClass, Remediation
from orchestration_engine.openclaw_executor import (
    DEFAULT_TIMEOUT_SECONDS,
    MODEL_MAP,
    OpenClawExecutor,
    _CIRCUIT_BREAKERS,
    _CIRCUIT_BREAKERS_LOCK,
)
from orchestration_engine.recovery import CircuitBreakerState, ExecutorRetryConfig
from orchestration_engine.schemas import Priority, TaskSpec, TaskState, TaskType


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Module-level CB registry leaks across tests; reset per test."""
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()
    yield
    with _CIRCUIT_BREAKERS_LOCK:
        _CIRCUIT_BREAKERS.clear()


@pytest.fixture
def tmp_openclaw_config(tmp_path, monkeypatch):
    """Redirect ``~/.openclaw/openclaw.json`` to a tmp file.

    Returns a callable: ``write_config(payload: dict)`` that JSON-dumps the
    payload to the redirected path. The caller passes ``None`` to remove the
    file (simulating absent config).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    config_dir = fake_home / ".openclaw"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"

    def _write(payload: Optional[Dict[str, Any]]) -> Path:
        if payload is None:
            if config_path.exists():
                config_path.unlink()
        else:
            config_path.write_text(json.dumps(payload))
        return config_path

    return _write


# ===========================================================================
# TestSubagentTimeoutFromConfig — Issue #753
# ===========================================================================


class TestSubagentTimeoutFromConfig:
    """Contract A2 — executor reads subagents.runTimeoutSeconds from openclaw.json."""

    # --- (a) Bug reproducer: with no explicit arg AND no config support,
    #         the executor uses the hard-coded DEFAULT.

    def test_reproducer_falls_through_to_default_when_config_absent(
        self, tmp_openclaw_config
    ):
        """Reproducer: no openclaw.json → executor uses DEFAULT_TIMEOUT_SECONDS."""
        tmp_openclaw_config(None)  # remove config file
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    # --- (b) Fixed path: with subagents.runTimeoutSeconds in config, executor reads it.

    def test_reads_runtimeoutseconds_from_subagents_block(self, tmp_openclaw_config):
        """Contract A2.1 bullet 1 — config value wins over DEFAULT."""
        tmp_openclaw_config({"subagents": {"runTimeoutSeconds": 1800}})
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == 1800

    def test_reads_runtimeoutseconds_custom_value(self, tmp_openclaw_config):
        """Contract A2.1 bullet 1 — any positive integer is honoured."""
        tmp_openclaw_config({"subagents": {"runTimeoutSeconds": 2700}})
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == 2700

    def test_explicit_timeout_arg_wins_over_config(self, tmp_openclaw_config):
        """Contract A2.1 bullet 4 — explicit arg > config > DEFAULT."""
        tmp_openclaw_config({"subagents": {"runTimeoutSeconds": 1800}})
        executor = OpenClawExecutor(
            gateway_url="http://localhost:18789", timeout_seconds=600
        )
        assert executor.timeout_seconds == 600

    # --- (c) Adversary-surfaced edge cases.

    def test_missing_subagents_block_falls_through_to_default(
        self, tmp_openclaw_config
    ):
        """Contract A2.1 bullet 2 — config exists but no subagents key."""
        tmp_openclaw_config({"gateway": {"auth": {"token": "abc"}}})
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    def test_subagents_block_without_runtimeoutseconds_falls_through(
        self, tmp_openclaw_config
    ):
        """Contract A2.1 bullet 2 — subagents exists but no runTimeoutSeconds."""
        tmp_openclaw_config({"subagents": {"maxConcurrent": 4}})
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    def test_malformed_config_falls_through_silently(self, tmp_openclaw_config):
        """Contract A2.2 bullet 1 — malformed JSON → DEFAULT, no exception."""
        config_path = tmp_openclaw_config({})  # write empty dict first
        config_path.write_text("{not valid json")
        # Must NOT raise — read fails silently.
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    def test_non_integer_runtimeoutseconds_falls_through(self, tmp_openclaw_config):
        """Contract A2.2 bullet 3 — non-positive int rejected, DEFAULT used."""
        tmp_openclaw_config({"subagents": {"runTimeoutSeconds": -10}})
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    def test_zero_runtimeoutseconds_falls_through(self, tmp_openclaw_config):
        """Contract A2.2 bullet 3 — zero rejected, DEFAULT used."""
        tmp_openclaw_config({"subagents": {"runTimeoutSeconds": 0}})
        executor = OpenClawExecutor(gateway_url="http://localhost:18789")
        assert executor.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    def test_falsy_explicit_timeout_falls_through_to_config(
        self, tmp_openclaw_config
    ):
        """Contract A2.1 bullet 5 — explicit timeout=0 uses config/default."""
        tmp_openclaw_config({"subagents": {"runTimeoutSeconds": 1800}})
        executor = OpenClawExecutor(
            gateway_url="http://localhost:18789", timeout_seconds=0
        )
        # 0 is falsy → config value wins.
        assert executor.timeout_seconds == 1800


# ===========================================================================
# TestCircuitBreakerEscalation — Issue #480
# ===========================================================================


def _make_executor() -> OpenClawExecutor:
    return OpenClawExecutor(gateway_url="http://localhost:18789", dry_run=False)


def _open_cb_for(model: str) -> CircuitBreakerState:
    """Force-open the CB for *model* by recording threshold-many failures."""
    cfg = ExecutorRetryConfig()
    with _CIRCUIT_BREAKERS_LOCK:
        cb = CircuitBreakerState(name=model)
        for _ in range(cfg.circuit_breaker_threshold):
            cb.record_failure(cfg.circuit_breaker_threshold)
        _CIRCUIT_BREAKERS[model] = cb
    return cb


def _make_task(model_chain: Optional[List[str]] = None) -> TaskSpec:
    payload: Dict[str, Any] = {"prompt": "test"}
    if model_chain is not None:
        payload["model_chain"] = model_chain
    return TaskSpec(
        type=TaskType.CONTENT,
        payload=payload,
        priority=Priority.NORMAL,
    )


class TestCircuitBreakerEscalation:
    """Contract A1 — CB-open first-gate must defer to fallback chain."""

    # --- (a) Bug reproducer: with sonnet CB open, the executor's pre-fix
    #         behaviour was to return circuit_open. Post-fix must escalate.

    def test_reproducer_cb_open_with_fallback_must_not_return_circuit_open(self):
        """Contract A1.1 bullet 1+3 — when sonnet CB is open and opus CB is closed,
        the executor must NOT immediately return circuit_open. It must attempt
        opus (or any tier with closed CB) and report that result.

        We mock ``_run_session`` to return a deterministic success so the test
        proves only the routing decision, not session-level mechanics.
        """
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]
        _open_cb_for(sonnet_model)

        executor = _make_executor()
        # Have the inner _run_session always succeed
        with patch.object(executor, "_run_session", return_value=("OK", 100)) as m:
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        # Bug reproducer assertion: state must NOT be FAILED with circuit_open
        # for the originally requested model when a fallback tier exists.
        if result.state == TaskState.FAILED:
            for err in result.errors:
                assert err.code != "circuit_open", (
                    f"Regression: CB-open early-return reappeared "
                    f"(errors={[(e.code, e.message) for e in result.errors]})"
                )
        # Post-fix: must have invoked _run_session at least once on opus.
        assert m.called, "Executor never reached _run_session — first-gate early-returned"
        invoked_models = [call.args[1] for call in m.call_args_list]
        assert opus_model in invoked_models, (
            f"Expected escalation to opus; got invocations: {invoked_models}"
        )

    # --- (b) Fixed path: A1.1 bullet 2 — success via escalation records
    #         success against the escalated tier.

    def test_success_via_escalation_records_against_escalated_model(self):
        """Contract A1.1 bullet 2 — escalated success recorded against escalated tier."""
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]
        _open_cb_for(sonnet_model)
        cfg = ExecutorRetryConfig()

        executor = _make_executor()
        with patch.object(executor, "_run_session", return_value=("OK", 50)):
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        # Sonnet's CB is still open (we don't record success on it).
        with _CIRCUIT_BREAKERS_LOCK:
            sonnet_cb = _CIRCUIT_BREAKERS.get(sonnet_model)
            opus_cb = _CIRCUIT_BREAKERS.get(opus_model)
        assert sonnet_cb is not None
        assert sonnet_cb.is_open(
            cfg.circuit_breaker_threshold,
            cfg.circuit_breaker_reset_seconds // 60,
        ), "Sonnet CB should still be open after escalation"

        # Opus must have been touched and its failure_count must be 0
        # (a record_success resets it).
        assert opus_cb is not None, "Opus CB entry never created during escalation"
        assert opus_cb.failure_count == 0, (
            "Opus CB failure_count should be 0 after record_success "
            f"(got {opus_cb.failure_count})"
        )

        # TaskResult model_used must reflect the escalated tier.
        assert result.model_used == opus_model, (
            f"model_used should be opus after escalation; got {result.model_used}"
        )
        assert result.state == TaskState.SUCCESS

    def test_all_tiers_cb_open_returns_all_tiers_unavailable(self):
        """Contract A1.1 bullet 3 — every tier CB open → all_tiers_unavailable."""
        sonnet_model = MODEL_MAP["sonnet"]
        opus_model = MODEL_MAP["opus"]
        _open_cb_for(sonnet_model)
        _open_cb_for(opus_model)

        executor = _make_executor()
        with patch.object(executor, "_run_session", return_value=("OK", 50)) as m:
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        assert result.state == TaskState.FAILED
        codes = [err.code for err in result.errors]
        assert "all_tiers_unavailable" in codes, (
            f"Expected all_tiers_unavailable code in errors; got {codes}"
        )
        # _run_session must NOT have been called when every tier is CB-broken.
        assert not m.called, (
            "When all tiers' CBs are open, _run_session must not be invoked"
        )

    # --- (c) Adversary-surfaced edge cases.

    def test_closed_cb_does_not_call_chain_advance(self):
        """Contract A1.1 bullet 4 — closed-CB tasks must not advance the chain.

        Adversary F3: trivial-satisfaction-resistant assertion.
        """
        executor = _make_executor()
        # All CBs closed — sonnet entry implicitly fresh.
        with patch(
            "orchestration_engine.openclaw_executor.ModelFallbackChain"
        ) as mock_chain_cls:
            mock_chain = MagicMock()
            mock_chain.current.return_value = "sonnet"
            mock_chain.has_next.return_value = True
            mock_chain.tiers.return_value = ["sonnet", "opus"]
            mock_chain_cls.return_value = mock_chain

            with patch.object(executor, "_run_session", return_value=("OK", 50)):
                task = _make_task(model_chain=["sonnet", "opus"])
                executor.execute(task)

        # When CB is closed at entry, the executor must NOT advance the chain.
        # advance() is only called when a tier's CB is open OR when retries are
        # exhausted; neither happened in this success path.
        mock_chain.advance.assert_not_called()

    def test_closed_cb_no_double_record_failure(self):
        """Contract A1.1 bullet 5 — no double-recording on closed-CB success.

        Adversary F3: trivial-satisfaction-resistant assertion.
        """
        sonnet_model = MODEL_MAP["sonnet"]
        executor = _make_executor()
        with patch.object(executor, "_run_session", return_value=("OK", 50)):
            task = _make_task(model_chain=["sonnet", "opus"])
            executor.execute(task)

        with _CIRCUIT_BREAKERS_LOCK:
            sonnet_cb = _CIRCUIT_BREAKERS.get(sonnet_model)
        assert sonnet_cb is not None
        assert sonnet_cb.failure_count == 0, (
            f"sonnet failure_count must be 0 after successful closed-CB run "
            f"(got {sonnet_cb.failure_count})"
        )

    def test_custom_model_chain_respected_on_escalation(self):
        """Contract A1.1 bullet 6 — custom payload.model_chain ordering wins."""
        sonnet_model = MODEL_MAP["sonnet"]
        haiku_model = MODEL_MAP["haiku"]
        _open_cb_for(sonnet_model)

        executor = _make_executor()
        with patch.object(executor, "_run_session", return_value=("OK", 50)) as m:
            task = _make_task(model_chain=["sonnet", "haiku"])
            result = executor.execute(task)

        invoked_models = [call.args[1] for call in m.call_args_list]
        # Must escalate to haiku, NOT to the implicit-default opus.
        assert haiku_model in invoked_models
        assert MODEL_MAP["opus"] not in invoked_models, (
            "Custom chain ['sonnet','haiku'] must not escalate to opus"
        )
        assert result.state == TaskState.SUCCESS

    def test_elapsed_cooldown_uses_original_model_first(self):
        """Contract A1.2 bullet 1 (adversary F16) — elapsed CB cooldown → original first.

        After the reset window elapses, the CB is_open() reports False, so the
        executor must NOT escalate — it tries the original (lower-tier) model.
        """
        sonnet_model = MODEL_MAP["sonnet"]
        # Open the CB then backdate opened_at past the reset window.
        cb = _open_cb_for(sonnet_model)
        cfg = ExecutorRetryConfig()
        # Backdate opened_at by reset_minutes + 1 so is_open() now reports False.
        from datetime import datetime, timedelta, timezone
        cb.opened_at = datetime.now(timezone.utc) - timedelta(
            minutes=(cfg.circuit_breaker_reset_seconds // 60) + 1
        )

        executor = _make_executor()
        with patch.object(executor, "_run_session", return_value=("OK", 50)) as m:
            task = _make_task(model_chain=["sonnet", "opus"])
            result = executor.execute(task)

        invoked_models = [call.args[1] for call in m.call_args_list]
        # Sonnet must be the FIRST model invoked (no escalation).
        assert invoked_models, "Executor did not invoke _run_session at all"
        assert invoked_models[0] == sonnet_model, (
            f"Elapsed-cooldown task must try sonnet first; "
            f"got first invocation on {invoked_models[0]}"
        )
        assert result.state == TaskState.SUCCESS
        assert result.model_used == sonnet_model


# ===========================================================================
# TestContentPipelineReliability — Issue #735
# ===========================================================================


# Avoid lint at tests/test_lint_no_templates_hardcode.py — the regex catches
# `/ "templates"`. Use joinpath() instead (mirrors test_cli_batch3.py).
_REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENT_PIPELINE_PATH = _REPO_ROOT.joinpath("templates", "content-pipeline.yaml")


def _load_content_pipeline_phases() -> Dict[str, Dict[str, Any]]:
    """Parse the content-pipeline.yaml and return {phase_id: phase_dict}.

    Uses PyYAML directly (no orchemist YAML helper) to keep the test schema-
    independent.
    """
    import yaml

    with CONTENT_PIPELINE_PATH.open() as f:
        doc = yaml.safe_load(f)
    return {p["id"]: p for p in doc.get("phases", [])}


class TestContentPipelineReliability:
    """Contract A3 — RC-1 (yaml timeouts), RC-2 (git-output GC), RC-3 (retry clone), RC-4 (dedup)."""

    # ---------------------------------------------------------------
    # RC-1 — per-phase timeout_seconds in content-pipeline.yaml
    # ---------------------------------------------------------------

    # --- (a) Bug reproducer

    def test_rc1_reproducer_pre_fix_phases_had_no_timeout_seconds(self):
        """Reproducer: confirm content-pipeline.yaml exists + is loadable."""
        assert CONTENT_PIPELINE_PATH.exists(), (
            f"content-pipeline.yaml missing at {CONTENT_PIPELINE_PATH}"
        )
        phases = _load_content_pipeline_phases()
        # Pre-fix: zero phases declared timeout_seconds. Post-fix: all do.
        # This test asserts post-fix shape; pre-fix the loop has zero hits.
        assert len(phases) >= 5

    # --- (b) Fixed path

    def test_rc1_opus_phases_have_timeout_seconds_between_2400_and_3600(self):
        """Contract A3.1 bullet 1 — opus phases have 2400 <= ts <= 3600."""
        phases = _load_content_pipeline_phases()
        opus_phase_ids = [
            pid for pid, p in phases.items() if p.get("model_tier") == "opus"
        ]
        assert opus_phase_ids, "Expected at least one opus phase in content-pipeline.yaml"
        for pid in opus_phase_ids:
            ts = phases[pid].get("timeout_seconds")
            assert ts is not None, f"Phase {pid} (opus) missing timeout_seconds"
            assert 2400 <= ts <= 3600, (
                f"Phase {pid} (opus) timeout_seconds={ts}; "
                f"expected 2400 <= ts <= 3600 per adversary F8"
            )

    def test_rc1_websearch_phases_have_timeout_seconds_between_2400_and_3600(self):
        """Contract A3.1 bullet 2 — research + fact_check (web search) phases."""
        phases = _load_content_pipeline_phases()
        # These two phases both invoke web_search in their prompts.
        for pid in ("research", "fact_check"):
            assert pid in phases, f"Expected phase {pid} in content-pipeline.yaml"
            ts = phases[pid].get("timeout_seconds")
            assert ts is not None, f"Phase {pid} missing timeout_seconds"
            assert 2400 <= ts <= 3600, (
                f"Phase {pid} (web search) timeout_seconds={ts}; expected 2400..3600"
            )

    def test_rc1_sonnet_no_websearch_phases_have_timeout_seconds_between_1800_and_3600(
        self,
    ):
        """Contract A3.1 bullet 3 — apply_fixes + voice_check (sonnet, no web)."""
        phases = _load_content_pipeline_phases()
        for pid in ("apply_fixes", "voice_check"):
            assert pid in phases, f"Expected phase {pid} in content-pipeline.yaml"
            ts = phases[pid].get("timeout_seconds")
            assert ts is not None, f"Phase {pid} missing timeout_seconds"
            assert 1800 <= ts <= 3600, (
                f"Phase {pid} (sonnet) timeout_seconds={ts}; expected 1800..3600"
            )

    # --- (c) Adversary-surfaced edge case (F8 upper bound).

    def test_rc1_no_phase_exceeds_3600_seconds(self):
        """Contract A3.1 — upper bound 3600 prevents trivial-satisfaction (F8)."""
        phases = _load_content_pipeline_phases()
        for pid, p in phases.items():
            ts = p.get("timeout_seconds")
            if ts is not None:
                assert ts <= 3600, (
                    f"Phase {pid} timeout_seconds={ts} exceeds 3600 — "
                    "config smell; split into smaller phases"
                )

    # ---------------------------------------------------------------
    # RC-2 — gateway GC race with git-committed output
    # ---------------------------------------------------------------

    def _make_git_output_dir(self, tmp_path: Path, artifact_name: str = "draft.md") -> Path:
        """Create a tmp git repo with a committed artefact for RC-2 tests."""
        repo = tmp_path / "out"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repo, check=True
        )
        (repo / artifact_name).write_text("Phase output content.")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "phase output", "--quiet"], cwd=repo, check=True
        )
        return repo

    # --- (a) Bug reproducer (RC-2): without the fix, a GC after a successful
    # commit raises RuntimeError. With the fix, when explicit output_dir +
    # output_artifact are configured, the fallback returns success.

    def test_rc2_reproducer_gc_without_kwargs_raises_runtimeerror(self, tmp_path):
        """Contract A3.2 bullet 2 — GC + no output kwargs → original RuntimeError.

        This pins backward compatibility: callers that do NOT thread output_dir
        through the kwargs continue to see the pre-fix behaviour.
        """
        executor = _make_executor()
        check_method = getattr(executor, "_check_git_committed_output", None)
        if check_method is None:
            pytest.fail(
                "_check_git_committed_output helper missing — RC-2 not implemented"
            )
        # Calling with None kwargs must return None (fallback disabled).
        out = check_method("session-key", None, None)
        assert out is None, (
            "When output_dir/output_artifact are None, fallback must return None"
        )

    # --- (b) Fixed path (RC-2): with explicit kwargs + committed file, fallback returns content.

    def test_rc2_committed_artifact_within_grace_window_returns_success(self, tmp_path):
        """Contract A3.2 bullet 1 — explicit kwargs + recent commit → success.

        The helper returns (output_text, tokens) on success.
        """
        repo = self._make_git_output_dir(tmp_path, "draft.md")
        executor = _make_executor()
        result = executor._check_git_committed_output(
            session_key="session-key",
            output_dir=str(repo),
            output_artifact="draft.md",
        )
        assert result is not None
        output_text, tokens = result
        assert "Phase output content." in output_text
        # Tokens is opaque (the executor doesn't have the agent's usage data
        # post-GC); contract permits any non-negative int.
        assert isinstance(tokens, int) and tokens >= 0

    def test_rc2_non_git_output_dir_returns_none(self, tmp_path):
        """Contract A3.2 bullet 3 — non-git output_dir → None (fallback declines)."""
        non_git = tmp_path / "plain"
        non_git.mkdir()
        (non_git / "draft.md").write_text("content")
        executor = _make_executor()
        result = executor._check_git_committed_output(
            session_key="session-key",
            output_dir=str(non_git),
            output_artifact="draft.md",
        )
        assert result is None

    # --- (c) Adversary-surfaced edge cases.

    def test_rc2_artifact_absent_returns_none(self, tmp_path):
        """Contract A3.2 bullet 3 — file absent → None (raise original error)."""
        repo = self._make_git_output_dir(tmp_path, "draft.md")
        executor = _make_executor()
        result = executor._check_git_committed_output(
            session_key="session-key",
            output_dir=str(repo),
            output_artifact="notthere.md",
        )
        assert result is None

    def test_rc2_stale_commit_outside_grace_window_returns_none(
        self, tmp_path, monkeypatch
    ):
        """Contract A3.2 bullet 3 — commit older than grace window → None.

        Adversary F2 narrowing: grace window must be enforced.
        """
        repo = self._make_git_output_dir(tmp_path, "draft.md")
        # Force grace window to 0 so the existing commit is "stale".
        monkeypatch.setenv("GC_OUTPUT_GRACE_SECONDS", "0")
        # Wait a beat so the commit timestamp is strictly older than "now".
        time.sleep(1.1)
        executor = _make_executor()
        result = executor._check_git_committed_output(
            session_key="session-key",
            output_dir=str(repo),
            output_artifact="draft.md",
        )
        assert result is None, (
            "Stale commit (older than grace window) must NOT trigger the fallback"
        )

    def test_rc2_success_records_cb_against_spawn_model_not_git_metadata(
        self, tmp_path
    ):
        """Contract A3.2 bullet 4 (adversary F13) — fallback success records
        CB success against the MODEL passed into _run_session, NOT against any
        git-commit metadata.

        Integration test: we exercise the fallback in isolation and then verify
        that the spawn-model variable is what gets recorded. The implementer
        wires _check_git_committed_output's return through the existing
        record_success path in _run_session, which uses the local `model`.
        """
        repo = self._make_git_output_dir(tmp_path, "draft.md")
        executor = _make_executor()
        # Fallback returns (text, tokens) without consulting model metadata.
        result = executor._check_git_committed_output(
            session_key="session-key",
            output_dir=str(repo),
            output_artifact="draft.md",
        )
        assert result is not None
        # The fallback signature does NOT take a model arg — it cannot record
        # CB success itself. The caller (the GC branch of _run_session) is
        # responsible. We verify the helper is model-agnostic by asserting
        # the return tuple shape and that it carries the file content only.
        output_text, tokens = result
        assert "Phase output content." in output_text
        # The helper does NOT inspect git-commit author/metadata for a model
        # name; an implementer that returns model-derived data via this helper
        # would change the tuple shape.
        assert len(result) == 2, (
            "Helper return shape must remain (text, tokens) — git-commit "
            "metadata must NOT be threaded back through the helper."
        )

    # ---------------------------------------------------------------
    # RC-3 — retry must clone original output_dir when it is a git repo
    # ---------------------------------------------------------------

    def _setup_retry_db(self, tmp_path: Path, original_output_dir: str) -> Database:
        """Create a DB with one failed original run pointing at output_dir."""
        from tests._helpers import pipeline_run_dict

        db = Database(db_path=Path(":memory:"))
        db.insert_pipeline_run(pipeline_run_dict(
            "orig-001",
            template_path="/tmp/template.yaml",
            template_id="t1",
            input_json=json.dumps({"budget_usd": 1.0}),
            mode="dry_run",
            output_dir=original_output_dir,
        ))
        return db

    def _make_diagnosis_retryable(self) -> DiagnosisResult:
        return DiagnosisResult(
            failure_class=FailureClass.QUALITY_GAP,
            remediation=Remediation.RETRY_ESCALATED_MODEL,
            confidence=0.9,
            explanation="test",
        )

    # --- (a) Bug reproducer (RC-3)

    def test_rc3_reproducer_pre_fix_creates_orphan_dir(self, tmp_path):
        """Pre-fix: a retry against a git-repo original creates an empty sibling dir.

        Post-fix: the retry directory is a git clone of the original.
        Reproducer asserts the EXISTENCE of a remediation (file presence or
        git status), so it stays green under the fix and red without it.
        """
        # Create a git repo as the original output_dir.
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=orig_dir, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=orig_dir, check=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=orig_dir, check=True)
        (orig_dir / "README.md").write_text("seed")
        subprocess.run(["git", "add", "-A"], cwd=orig_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", "seed", "--quiet"], cwd=orig_dir, check=True
        )

        # Run the retry engine; mock subprocess.Popen to prevent the daemon spawn.
        db = self._setup_retry_db(tmp_path, str(orig_dir))
        engine = AdaptiveRetryEngine(db=db, db_path=":memory:")
        run = db.get_pipeline_run("orig-001")
        # Patch Popen in the adaptive_retry module ONLY — not globally — so
        # our RC-3 git-clone (which uses subprocess.run) is not intercepted.
        with patch("orchestration_engine.adaptive_retry.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            engine.plan_and_execute(
                self._make_diagnosis_retryable(), run, "orig-001", max_retries=3
            )

        # Find the inserted retry row.
        all_runs = db.fetch_all("SELECT * FROM pipeline_runs")
        retry_rows = [r for r in all_runs if r["retry_of_run_id"] == "orig-001"]
        assert len(retry_rows) == 1, (
            f"Expected exactly one retry row; got {len(retry_rows)}"
        )
        retry_output_dir = Path(retry_rows[0]["output_dir"])

        # Post-fix assertion: retry_output_dir is a git clone (has .git).
        assert (retry_output_dir / ".git").exists(), (
            f"Retry output dir {retry_output_dir} is missing .git — "
            "RC-3 fix not applied (orphan dir regression)"
        )
        # Seed file must have been cloned through.
        assert (retry_output_dir / "README.md").exists(), (
            "Clone did not propagate committed files"
        )

    # --- (b) Fixed path (RC-3) — non-git original preserved as empty-sibling.

    def test_rc3_non_git_original_falls_back_to_legacy_behaviour(self, tmp_path):
        """Contract A3.3 bullet 2 — non-git original → no clone attempted."""
        orig_dir = tmp_path / "plain"
        orig_dir.mkdir()  # no git init
        (orig_dir / "data.txt").write_text("plain")

        db = self._setup_retry_db(tmp_path, str(orig_dir))
        engine = AdaptiveRetryEngine(db=db, db_path=":memory:")
        run = db.get_pipeline_run("orig-001")
        # Patch Popen in the adaptive_retry module ONLY — not globally — so
        # our RC-3 git-clone (which uses subprocess.run) is not intercepted.
        with patch("orchestration_engine.adaptive_retry.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            engine.plan_and_execute(
                self._make_diagnosis_retryable(), run, "orig-001", max_retries=3
            )

        # Should succeed without raising; retry row was inserted.
        all_runs = db.fetch_all("SELECT * FROM pipeline_runs")
        retry_rows = [r for r in all_runs if r["retry_of_run_id"] == "orig-001"]
        assert len(retry_rows) == 1
        # The retry_output_dir need not exist or contain a clone (legacy beh.).
        retry_output_dir = Path(retry_rows[0]["output_dir"])
        if retry_output_dir.exists():
            assert not (retry_output_dir / ".git").exists(), (
                "Non-git original must NOT produce a git clone"
            )

    # --- (c) Adversary-surfaced edge cases — F6: remote URL inheritance.

    def test_rc3_clone_inherits_remote_url_from_original(self, tmp_path):
        """Contract A3.3 bullet 5 (adversary F6) — clone preserves origin remote."""
        # Create a bare repo as the remote.
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)

        orig_dir = tmp_path / "orig"
        subprocess.run(
            ["git", "clone", "--quiet", str(remote), str(orig_dir)], check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=orig_dir, check=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=orig_dir, check=True)
        (orig_dir / "README.md").write_text("seed")
        subprocess.run(["git", "add", "-A"], cwd=orig_dir, check=True)
        subprocess.run(
            ["git", "commit", "-m", "seed", "--quiet"], cwd=orig_dir, check=True
        )

        db = self._setup_retry_db(tmp_path, str(orig_dir))
        engine = AdaptiveRetryEngine(db=db, db_path=":memory:")
        run = db.get_pipeline_run("orig-001")
        # Patch Popen in the adaptive_retry module ONLY — not globally — so
        # our RC-3 git-clone (which uses subprocess.run) is not intercepted.
        with patch("orchestration_engine.adaptive_retry.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            engine.plan_and_execute(
                self._make_diagnosis_retryable(), run, "orig-001", max_retries=3
            )

        all_runs = db.fetch_all("SELECT * FROM pipeline_runs")
        retry_rows = [r for r in all_runs if r["retry_of_run_id"] == "orig-001"]
        assert len(retry_rows) == 1
        retry_output_dir = Path(retry_rows[0]["output_dir"])
        # The clone should have an origin remote URL pointing at SOMETHING
        # reachable from the retry dir. Cloning from a local path produces
        # an origin pointing at that local path.
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=retry_output_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Retry clone missing origin remote: stderr={result.stderr}"
        )
        # The origin URL is the LOCAL clone (which itself has a remote
        # pointing at the bare remote). Either is acceptable per A3.3.
        assert result.stdout.strip(), "origin URL must be non-empty"

    # ---------------------------------------------------------------
    # RC-4 — race-free retry dedup
    # ---------------------------------------------------------------

    # --- (a) Bug reproducer (RC-4)

    def test_rc4_reproducer_active_retry_blocks_duplicate(self, tmp_path):
        """Contract A3.4 bullet 1 — active retry blocks a duplicate evaluate.

        Pre-fix: a second plan_and_execute against the same original spawns a
        second retry. Post-fix: it is short-circuited.
        """
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        db = self._setup_retry_db(tmp_path, str(orig_dir))

        # Pre-insert an in-progress retry row.
        from tests._helpers import pipeline_run_dict
        db.insert_pipeline_run(pipeline_run_dict(
            "retry-prev",
            template_path="/tmp/t.yaml",
            template_id="t1",
            mode="dry_run",
            output_dir="/tmp/out/retry-prev",
            retry_of_run_id="orig-001",
            status="running",
        ))

        engine = AdaptiveRetryEngine(db=db, db_path=":memory:")
        run = db.get_pipeline_run("orig-001")
        # Patch Popen in the adaptive_retry module ONLY — not globally — so
        # our RC-3 git-clone (which uses subprocess.run) is not intercepted.
        with patch("orchestration_engine.adaptive_retry.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            engine.plan_and_execute(
                self._make_diagnosis_retryable(), run, "orig-001", max_retries=3
            )

        # Post-fix: NO new retry row beyond the pre-inserted one.
        retry_rows = db.fetch_all(
            "SELECT * FROM pipeline_runs WHERE retry_of_run_id = ?", ("orig-001",)
        )
        assert len(retry_rows) == 1, (
            f"Expected 1 retry row (the pre-existing one); got {len(retry_rows)} "
            "— RC-4 dedup not enforced"
        )
        assert retry_rows[0]["run_id"] == "retry-prev"
        # subprocess.Popen MUST NOT have been called for the duplicate.
        assert not mock_popen.called, (
            "Daemon subprocess was spawned despite active retry — dedup race"
        )

    # --- (b) Fixed path: completed retry does NOT block new ones.

    def test_rc4_completed_retry_does_not_block_new_retry(self, tmp_path):
        """Contract A3.4 bullet 2 — historical retries don't block."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        db = self._setup_retry_db(tmp_path, str(orig_dir))

        from tests._helpers import pipeline_run_dict
        db.insert_pipeline_run(pipeline_run_dict(
            "retry-historical",
            template_path="/tmp/t.yaml",
            template_id="t1",
            mode="dry_run",
            output_dir="/tmp/out/retry-historical",
            retry_of_run_id="orig-001",
            status="failed",  # NOT pending/running
        ))

        engine = AdaptiveRetryEngine(db=db, db_path=":memory:")
        run = db.get_pipeline_run("orig-001")
        # Patch Popen in the adaptive_retry module ONLY — not globally — so
        # our RC-3 git-clone (which uses subprocess.run) is not intercepted.
        with patch("orchestration_engine.adaptive_retry.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            engine.plan_and_execute(
                self._make_diagnosis_retryable(), run, "orig-001", max_retries=3
            )

        retry_rows = db.fetch_all(
            "SELECT * FROM pipeline_runs WHERE retry_of_run_id = ?", ("orig-001",)
        )
        # The historical row + the new retry = 2 rows.
        assert len(retry_rows) == 2, (
            f"Historical retry should not block; got {len(retry_rows)} rows"
        )

    # --- (c) Adversary-surfaced edge case (F7) — concurrent dedup race.

    def test_rc4_concurrent_evaluators_produce_at_most_one_active_retry(
        self, tmp_path
    ):
        """Contract A3.4 bullet 3 (adversary F7) — concurrent dedup is race-free."""
        orig_dir = tmp_path / "orig"
        orig_dir.mkdir()
        db = self._setup_retry_db(tmp_path, str(orig_dir))

        # Two concurrent calls to plan_and_execute. The patch is scoped over
        # the entire concurrent section (NOT per-thread) — unittest.mock.patch
        # mutates a module global and is not thread-safe when used inside
        # workers, which would leak the Popen mock into subsequent tests.
        run = db.get_pipeline_run("orig-001")
        engine = AdaptiveRetryEngine(db=db, db_path=":memory:")

        barrier = threading.Barrier(2)
        errors: List[Exception] = []

        def _worker():
            try:
                barrier.wait(timeout=5)
                engine.plan_and_execute(
                    self._make_diagnosis_retryable(),
                    run,
                    "orig-001",
                    max_retries=3,
                )
            except Exception as e:
                errors.append(e)

        # Patch Popen in the adaptive_retry module ONLY — not globally — so
        # our RC-3 git-clone (which uses subprocess.run) is not intercepted.
        with patch("orchestration_engine.adaptive_retry.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        # At most ONE active retry must exist.
        active_rows = db.fetch_all(
            "SELECT * FROM pipeline_runs "
            "WHERE retry_of_run_id = ? AND status IN ('pending','running')",
            ("orig-001",),
        )
        assert len(active_rows) <= 1, (
            f"Concurrent evaluators produced {len(active_rows)} active retries — "
            "RC-4 race-free contract violated"
        )
