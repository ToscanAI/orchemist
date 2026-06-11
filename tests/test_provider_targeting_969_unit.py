"""Implementer-authored unit tests for #969 per-phase provider targeting.

Distinct from the sealed acceptance suite (tests/test_per_phase_provider_969.py):
this file isolates internals the sealed suite intentionally pins only by CONTRACT
(it deliberately gives its fakes class names carrying the provider token so it
passes via attr-match OR substring fallback). Here we prove the EXPLICIT
``provider_name`` attr-match path on its own, the ``KNOWN_PROVIDERS`` gate,
the validate ERROR channel, and from_providers ordering — with zero network.

RUN WITH:  PYTHONPATH=src python3 -m pytest tests/test_provider_targeting_969_unit.py -q
"""

import pytest

from orchestration_engine.pipeline_runner import PipelineRunner
from orchestration_engine.runner import DryRunExecutor, TaskExecutor
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import (
    TemplateEngine,
    PhaseDefinition,
    PipelineTemplate,
)
from orchestration_engine.schemas import TaskType, TaskState, TaskResult
from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
from orchestration_engine.executors.openrouter_executor import OpenRouterExecutor
from orchestration_engine.executors.claudecode_executor import ClaudeCodeExecutor
from orchestration_engine.executors.gemini_cli_executor import GeminiCliExecutor
from orchestration_engine.openclaw_executor import OpenClawExecutor


# ---------------------------------------------------------------------------
# A neutral-named recording fake. Its class name carries NO provider token, so
# routing to it can ONLY succeed via the explicit provider_name attr (the
# substring fallback cannot rescue it). This is the distinction from the sealed
# suite's dual-identity fakes.
# ---------------------------------------------------------------------------
class _AttrOnlyExec(TaskExecutor):
    def __init__(self, provider_name):
        self.provider_name = provider_name
        self.served = []

    def execute(self, task, worker_id="w", model_tier=None, thinking_level=None, **kwargs):
        pid = (getattr(task, "payload", None) or {}).get("phase_id", getattr(task, "id", "x"))
        self.served.append(pid)
        return TaskResult(
            task_id=getattr(task, "id", "x"),
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": "APPROVE\n\nok", "output": "ok"},
        )

    def can_handle(self, task_type, **kwargs):
        return True

    def estimate_cost(self, task, **kwargs):
        return 0.0


def _bare_template():
    return PipelineTemplate(
        id="d", name="D", version="1.0.0",
        phases=[PhaseDefinition(id="p", name="P", depends_on=[], prompt_template="hi")],
    )


# ---------------------------------------------------------------------------
# provider_name class attrs on the REAL executors (no construction-network).
# ---------------------------------------------------------------------------
def test_real_executors_expose_provider_name():
    assert AnthropicExecutor.provider_name == "anthropic"
    assert OpenRouterExecutor.provider_name == "openrouter"
    assert ClaudeCodeExecutor.provider_name == "claudecode"
    assert GeminiCliExecutor.provider_name == "gemini"
    assert OpenClawExecutor.provider_name == "openclaw"
    assert DryRunExecutor.provider_name == "dryrun"
    # The ABC default is the empty string (unknown/abstract).
    assert TaskExecutor.provider_name == ""


def test_known_providers_membership():
    assert set(TemplateEngine.KNOWN_PROVIDERS) == {"anthropic", "openrouter"}


# ---------------------------------------------------------------------------
# _resolve_executor_by_name: the EXPLICIT attr-match path, proven with fakes
# whose class names do NOT contain the provider token.
# ---------------------------------------------------------------------------
def test_resolver_matches_by_provider_name_attr_only():
    a = _AttrOnlyExec("anthropic")
    o = _AttrOnlyExec("openrouter")
    runner = PipelineRunner(executors=[a, o])
    try:
        seq = PhaseSequencer(_bare_template(), runner)
        # Class names carry no token -> only attr-match can resolve these.
        assert seq._resolve_executor_by_name("openrouter") is o
        assert seq._resolve_executor_by_name("anthropic") is a
        # Shim delegates identically.
        assert seq._resolve_dialogue_executor("openrouter") is o
    finally:
        runner.close()


def test_resolver_gemini_alias_via_attr():
    """The gemini_cli alias normalises to 'gemini' and matches the attr."""
    g = _AttrOnlyExec("gemini")
    runner = PipelineRunner(executors=[g])
    try:
        seq = PhaseSequencer(_bare_template(), runner)
        assert seq._resolve_executor_by_name("gemini_cli") is g
        assert seq._resolve_executor_by_name("gemini") is g
    finally:
        runner.close()


def test_resolver_empty_name_returns_none():
    a = _AttrOnlyExec("anthropic")
    runner = PipelineRunner(executors=[a])
    try:
        seq = PhaseSequencer(_bare_template(), runner)
        assert seq._resolve_executor_by_name("") is None
        assert seq._resolve_executor_by_name(None) is None
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# Selection defensive branch: provider set, no matching executor -> RuntimeError
# naming the provider (the §B.2 defensive raise). Driven through a full run.
# ---------------------------------------------------------------------------
def test_selection_defensive_raise_names_provider():
    # Runner holds only an anthropic-attr fake; phase declares openrouter, which
    # cannot be resolved -> the §B.2 defensive selection branch raises a
    # RuntimeError naming the provider + the registered executors + the
    # credential hint (a hard config error, raised before the retry loop).
    a = _AttrOnlyExec("anthropic")
    runner = PipelineRunner(executors=[a])
    try:
        phase = PhaseDefinition(
            id="x", name="X", depends_on=[], prompt_template="run {input}",
            provider="openrouter",
        )
        tpl = PipelineTemplate(id="t", name="T", version="1.0.0", phases=[phase])
        seq = PhaseSequencer(tpl, runner)
        with pytest.raises(RuntimeError) as ei:
            seq.execute({"input": "go"})
        msg = str(ei.value)
        assert "openrouter" in msg
        assert "no executor for provider" in msg
        assert "OPENROUTER_API_KEY" in msg
    finally:
        runner.close()


# ---------------------------------------------------------------------------
# validate_template_extended: the ERROR channel directly (no YAML round-trip),
# plus the difflib "did you mean" hint for a near-miss provider.
# ---------------------------------------------------------------------------
def test_validate_extended_provider_error_direct():
    engine = TemplateEngine()
    phase = PhaseDefinition(id="draft", name="Draft", provider="anthropics")  # typo
    tpl = PipelineTemplate(id="v", name="V", version="1.0.0", phases=[phase])
    errors, warnings = engine.validate_template_extended(tpl, {})
    prov_errs = [e for e in errors if "provider" in e]
    assert prov_errs, "unknown provider must be an ERROR"
    assert any("anthropics" in e for e in prov_errs)
    assert any("draft" in e for e in prov_errs)
    # Near-miss suggestion present.
    assert any("did you mean 'anthropic'" in e for e in prov_errs)
    # Inversion: NOT a warning.
    assert [w for w in warnings if "provider" in w] == []


@pytest.mark.parametrize("good", ["anthropic", "openrouter"])
def test_validate_extended_known_provider_no_error_direct(good):
    engine = TemplateEngine()
    phase = PhaseDefinition(id="p", name="P", provider=good)
    tpl = PipelineTemplate(id="v", name="V", version="1.0.0", phases=[phase])
    errors, _ = engine.validate_template_extended(tpl, {})
    assert [e for e in errors if "provider" in e] == []


# ---------------------------------------------------------------------------
# from_providers: explicit key kwargs only (no env reliance) — proves the
# shared builders + default-first ordering + the runtime unknown-provider guard.
# ---------------------------------------------------------------------------
def test_from_providers_default_openrouter_first(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = PhaseDefinition(id="a", name="A", depends_on=[], prompt_template="hi",
                        provider="anthropic")
    b = PhaseDefinition(id="b", name="B", depends_on=["a"], prompt_template="hi",
                        provider="openrouter")
    tpl = PipelineTemplate(id="m", name="M", version="1.0.0", phases=[a, b])
    runner = PipelineRunner.from_providers(
        tpl, anthropic_api_key="sk-ant-x", openrouter_api_key="sk-or-x",
        default_provider="openrouter",  # inverse default -> openrouter index 0
    )
    try:
        assert len(runner.executors) == 2
        assert runner.executors[0].provider_name == "openrouter"
        # Both providers present regardless of order.
        assert {e.provider_name for e in runner.executors} == {"anthropic", "openrouter"}
    finally:
        runner.close()


def test_from_providers_custom_model_map_forwarded(monkeypatch):
    """Run-level openrouter model_map reaches the built openrouter executor."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    b = PhaseDefinition(id="b", name="B", depends_on=[], prompt_template="hi",
                        provider="openrouter")
    tpl = PipelineTemplate(id="m", name="M", version="1.0.0", phases=[b])
    runner = PipelineRunner.from_providers(
        tpl, anthropic_api_key="sk-ant-x", openrouter_api_key="sk-or-x",
        openrouter_model_map={"sonnet": "openai/gpt-4o"},
        default_provider="anthropic",
    )
    try:
        ors = [e for e in runner.executors if e.provider_name == "openrouter"]
        assert ors and ors[0].model_map["sonnet"] == "openai/gpt-4o"
    finally:
        runner.close()


def test_from_providers_unknown_default_provider_raises(monkeypatch):
    """An unknown default_provider is also caught by the runtime guard."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tpl = PipelineTemplate(
        id="x", name="X", version="1.0.0",
        phases=[PhaseDefinition(id="p", name="P", depends_on=[], prompt_template="hi")],
    )
    with pytest.raises(ValueError) as ei:
        PipelineRunner.from_providers(
            tpl, anthropic_api_key="sk-ant-x", default_provider="ollama"
        )
    msg = str(ei.value).lower()
    assert "ollama" in msg
    assert "anthropic" in msg and "openrouter" in msg


# success
