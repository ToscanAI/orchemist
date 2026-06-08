"""Regression tests for #676 — web path applies config_schema defaults.

`web/app.py:_execute_pipeline` is the single funnel both web launch routes
(`/api/run` style endpoints at app.py:261 and :317) use to start a pipeline.
Before the fix it constructed ``PhaseSequencer(config=initial_input)`` WITHOUT
first applying the template's ``config_schema`` property defaults — so a
web-launched run that omitted a newly-added optional field would render the
literal ``<MISSING:field>`` into phase prompts (the same class of regression
#835 fixed for the CLI/daemon paths).

These tests prove the helper is invoked before the sequencer reads the config,
and that a caller-supplied value is not overwritten by a default.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict

import pytest

from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


def _make_template(config_schema: Dict[str, Any]) -> PipelineTemplate:
    """Build a one-phase template carrying the given config_schema."""
    phase = PhaseDefinition(
        id="p0",
        name="Phase 0",
        prompt_template="Use {config[tool]}",
    )
    return PipelineTemplate(
        id="web-defaults-fixture",
        name="Web Defaults Fixture",
        phases=[phase],
        config_schema=config_schema,
    )


def _make_active_runs(run_id: str, initial_input: Dict[str, Any]) -> Dict[str, Any]:
    """Build the minimal active_runs entry that _execute_pipeline reads."""
    return {
        run_id: {
            "run_id": run_id,
            "template": "web-defaults-fixture",
            "mode": "dry-run",
            "input": initial_input,
            "status": "starting",
            "phases_completed": [],
            "phases_failed": [],
            "events": [],
            "event_queue": asyncio.Queue(),
            "done": False,
            "error": None,
            "pause_after": [],
            "resume_event": threading.Event(),
            "paused_at_phase": None,
        }
    }


def _run_execute_pipeline(monkeypatch, initial_input: Dict[str, Any], config_schema):
    """Drive _execute_pipeline with PhaseSequencer stubbed; return captured config.

    Patches ``PhaseSequencer`` in the sequencer module (where _execute_pipeline
    lazily imports it) so the constructor records the ``config`` kwarg and
    ``execute`` returns a benign result. Returns the captured config dict (the
    object the sequencer would actually read for prompt rendering).
    """
    from orchestration_engine.web import app as web_app
    from orchestration_engine import sequencer as sequencer_mod

    captured: Dict[str, Any] = {}

    class _FakeSequencer:
        def __init__(self, template, runner, config=None, **kwargs):
            captured["config"] = config

        def execute(self, initial_input):
            return {"phase_outputs": {}, "final_output": {}}

    monkeypatch.setattr(sequencer_mod, "PhaseSequencer", _FakeSequencer)

    run_id = "run-test"
    active_runs = _make_active_runs(run_id, initial_input)
    template = _make_template(config_schema)

    asyncio.run(
        web_app._execute_pipeline(
            run_id, template, "dry-run", initial_input, active_runs
        )
    )
    return captured.get("config")


class TestWebPathAppliesDefaults:
    def test_web_path_fills_defaults_before_sequencer(self, monkeypatch):
        """A defaulted key absent from the input is present in the config the
        sequencer sees (the previously-missing #676 behaviour)."""
        config_schema = {"properties": {"tool": {"default": "pytest"}}}
        initial_input: Dict[str, Any] = {}

        captured_config = _run_execute_pipeline(
            monkeypatch, initial_input, config_schema
        )

        assert captured_config is not None
        assert captured_config.get("tool") == "pytest"

    def test_web_path_does_not_overwrite_caller_supplied_value(self, monkeypatch):
        """A caller-supplied value wins over the schema default."""
        config_schema = {"properties": {"tool": {"default": "pytest"}}}
        initial_input: Dict[str, Any] = {"tool": "mypy"}

        captured_config = _run_execute_pipeline(
            monkeypatch, initial_input, config_schema
        )

        assert captured_config is not None
        assert captured_config.get("tool") == "mypy"
