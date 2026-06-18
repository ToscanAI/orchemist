"""Deprecation signals for the `openclaw` execution mode (EPIC #1033, Phase 1).

Phase 1 DEPRECATES the `openclaw` mode but keeps it fully functional — removal
is Phase 2 (tracked in #1036). These tests assert that:

* selecting the mode programmatically (``PipelineRunner.openclaw``) emits BOTH a
  ``DeprecationWarning`` and a ``logger.warning`` — and does so *before* any
  network call (the warning fires at mode selection, not gateway contact), and
* the non-deprecated factories (``standalone`` / ``openrouter``) stay silent, and
* the ``providers_info`` entry + the canonical message are wired consistently.

Hermetic: every ``OpenClawExecutor`` here is built via ``dry_run=True`` with an
explicit gateway URL/token, so no HTTP and no ``~/.openclaw/openclaw.json`` read
influences the result. The warning is intentionally NOT emitted from
``OpenClawExecutor.__init__`` (that constructor is exercised by ~25 unrelated
unit tests), so constructing the executor directly must stay silent.
"""

import logging
import warnings

import pytest

from orchestration_engine.openclaw_executor import OpenClawExecutor
from orchestration_engine.pipeline_runner import (
    OPENCLAW_DEPRECATION_MESSAGE,
    PipelineRunner,
)
from orchestration_engine.providers_info import PROVIDERS_INFO


def _openclaw_runner() -> PipelineRunner:
    """Construct an openclaw-mode runner the hermetic way (no HTTP, no config read)."""
    return PipelineRunner.openclaw(
        gateway_url="http://localhost:18789",
        gateway_token="test-token",
        dry_run=True,
    )


class TestPipelineRunnerOpenclawDeprecation:
    """`PipelineRunner.openclaw(...)` is the programmatic mode-selection site."""

    def test_openclaw_factory_emits_deprecation_warning(self):
        with pytest.warns(DeprecationWarning, match="openclaw"):
            runner = _openclaw_runner()
        # Still functional (Phase 1): a real runner with one executor is returned.
        assert runner is not None
        assert len(runner.executors) == 1
        runner.close()

    def test_openclaw_factory_warning_uses_canonical_message(self):
        with pytest.warns(DeprecationWarning) as record:
            runner = _openclaw_runner()
        runner.close()
        messages = [str(w.message) for w in record]
        assert OPENCLAW_DEPRECATION_MESSAGE in messages
        # The canonical message names the alternatives + the tracking issue.
        assert "standalone" in OPENCLAW_DEPRECATION_MESSAGE
        assert "openrouter" in OPENCLAW_DEPRECATION_MESSAGE
        assert "1036" in OPENCLAW_DEPRECATION_MESSAGE

    def test_openclaw_factory_also_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.pipeline_runner"):
            with pytest.warns(DeprecationWarning):
                runner = _openclaw_runner()
        runner.close()
        assert any(
            OPENCLAW_DEPRECATION_MESSAGE in rec.getMessage() for rec in caplog.records
        ), "expected a logger.warning with the deprecation message"

    def test_standalone_factory_does_not_warn(self):
        """Sibling factories must stay silent — only openclaw is deprecated."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            runner = PipelineRunner.standalone(api_key="sk-ant-test")
        runner.close()

    def test_openrouter_factory_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            runner = PipelineRunner.openrouter(api_key="or-test-key")
        runner.close()


class TestOpenClawExecutorConstructorSilent:
    """Constructing the executor directly must NOT warn (per-test-isolation guard)."""

    def test_direct_executor_construction_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            executor = OpenClawExecutor(
                gateway_url="http://localhost:18789",
                gateway_token="test-token",
                dry_run=True,
            )
        assert executor is not None


class TestProvidersInfoOpenclawDeprecated:
    """The frozen providers registry marks openclaw deprecated in human-readable text."""

    def test_openclaw_entry_notes_deprecation(self):
        openclaw = next(p for p in PROVIDERS_INFO if p.name == "openclaw")
        # name/mode keys are unchanged (Phase 1 keeps it selectable).
        assert openclaw.name == "openclaw"
        assert openclaw.mode == "openclaw"
        # The human note flags the deprecation + points to the alternatives/#1036.
        assert "DEPRECATED" in openclaw.notes
        assert "1036" in openclaw.notes
