"""Acceptance tests — #969 Per-phase provider targeting (mixed-provider pipelines).

Round 2 — revised per test-adversary F1 (c7b hermeticity seal).

Sealed-tester output. Derived ONLY from behavioral.md (§0 harness map, §VALUES
byte-quoted ground truth, contracts 1-8, §F scope). No implementation seen.

RUN WITH:  PYTHONPATH=src python3 -m pytest <thisfile> -x -q
(The dominant suite convention is the bare ``from orchestration_engine...`` form;
the worktree editable .pth / PYTHONPATH=src resolves it.)

================================================================================
EXPECTED-TODAY LEDGER (derived from the [NEW]/[CHANGED]/[UNCHANGED] markers in
behavioral.md §0.2 + reachability §0.9). RED = must fail pre-impl; GREEN = shield
that passes today on today-real surfaces.
================================================================================
  C1  test_c1_* ............ RED   PhaseDefinition.provider is [NEW] ABSENT today
                                    ('provider' not in __dataclass_fields__) — the
                                    field, the YAML parse, the default-None, and
                                    the allowlist-survival all fail today.
  C2  test_c2_* ............ RED   unknown-provider-in-ERRORS is [NEW]. The
                                    model_tier contrast half is [UNCHANGED] (warn-
                                    only, GREEN today), but the test asserts BOTH
                                    halves so it is RED today (provider half fails;
                                    today an unknown provider field would be parsed
                                    /ignored, never landing in errors).
  C3  test_c3_* ............ RED   PipelineRunner.from_providers is [NEW] classmethod
                                    ABSENT today → AttributeError on every case
                                    (a/b/c/d/e/f).
  C4  test_c4_* ............ RED   provider-aware selection branch is net-new
                                    ([CHANGED] loop). Today phase B (provider:
                                    openrouter) is served by executors[0] via the
                                    first-can_handle loop, so the openrouter fake
                                    never records "b" → assertion fails.
  C5  test_c5_* ............ GREEN first-can_handle ordering is [UNCHANGED]: with no
                                    provider declared, only executors[0] runs today.
                                    standalone()==1-executor is [UNCHANGED]. Pure
                                    backward-compat shield on today-real surfaces.
  C6  test_c6_* ............ GREEN _resolve_dialogue_executor EXISTS today and the
                                    three pinned results are the live-probed baseline
                                    in §VALUES (substring + all-dry-run fallback +
                                    no-match None). [CHANGED]-to-shim must preserve
                                    them; they hold on today-real surfaces → GREEN.
  C7  test_c7_* ............ RED   orch run auto-upgrade routes through from_providers
                                    ([NEW]); spy/build fails today. (b) needs the
                                    eager build raise that does not exist yet.
  C8  test_c8_* ............ GREEN exercises ONLY today-real OpenRouter surfaces:
                                    custom model_map + disable_tools single-shot +
                                    urlopen body capture. Mirrors the existing
                                    test_custom_model_map_overrides_defaults. The
                                    per-phase framing is conceptual; the SUT (tier
                                    resolves through the TARGET executor's own map)
                                    is true today → GREEN shield.

SUBTLE CALLS spelled out:
  * C2 is RED-as-a-whole even though its model_tier half is a today-true shield —
    behavioral.md mandates both halves in one test to prove the deliberate F.2
    inversion; the provider half cannot pass pre-impl.
  * C5/C6/C8 are intentional GREEN shields: they touch no [NEW] symbol at module
    import or in-body, only today-real behaviour, so they guard against regression
    rather than driving the feature. C5 even includes the corroborating
    standalone()==1 sanity assert (§Contract 5) without duplicating
    test_pipeline_runner.py.
  * Module-level imports are TODAY-REAL ONLY. [NEW] symbols (PhaseDefinition.provider,
    from_providers, _resolve_executor_by_name, provider_name) are touched in-body so
    import never errors; their absence surfaces as the test failing, as intended.
================================================================================
"""

import json

import pytest
import yaml
from click.testing import CliRunner
from unittest.mock import patch, MagicMock

# --- TODAY-REAL imports only (verified importable at HEAD per §0.1) ----------
from orchestration_engine.pipeline_runner import PipelineRunner
from orchestration_engine.runner import DryRunExecutor, TaskExecutor
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import (
    TemplateEngine,
    PhaseDefinition,
    PipelineTemplate,
)
from orchestration_engine.schemas import TaskSpec, TaskType, TaskState, TaskResult
from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
from orchestration_engine.executors.openrouter_executor import (
    OpenRouterExecutor,
    DEFAULT_MODEL_MAP,
)
from orchestration_engine.cli import main  # the click group ("orch")


# ============================================================================
# §0.4 recording-fake recipe (dual identity: provider_name attr AND a class
# name carrying the provider token, so routing succeeds via attr-match OR the
# legacy substring fallback — we pin the CONTRACT, not the mechanism). Every
# method takes **kwargs so future executor kwargs never break the fakes.
# ============================================================================
class _RecordingExecutor(TaskExecutor):
    """Records which phase_ids it served.

    provider_name set per-fake so the NEW resolver matches it by attribute;
    can_handle is unconditionally True (like every real executor).
    """

    def __init__(self, provider_name):
        self.provider_name = provider_name
        self.served = []  # phase_ids this executor's execute() saw

    def execute(self, task, worker_id="w", model_tier=None,
                thinking_level=None, **kwargs):
        pid = (getattr(task, "payload", None) or {}).get(
            "phase_id", getattr(task, "id", "x")
        )
        self.served.append(pid)
        return TaskResult(
            task_id=getattr(task, "id", "x"),
            task_type=task.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": "APPROVE\n\ndone (test)", "output": "done",
                    "served_by": self.provider_name},
        )

    def can_handle(self, task_type, **kwargs):
        return True

    def estimate_cost(self, task, **kwargs):
        return 0.0


class FakeAnthropicExec(_RecordingExecutor):
    """type(ex).__name__ contains 'anthropic' (substring fallback safety)."""


class FakeOpenRouterExec(_RecordingExecutor):
    """type(ex).__name__ contains 'openrouter' (substring fallback safety)."""


# ============================================================================
# YAML / template helpers
# ============================================================================
_MIN_TPL = """\
id: prov-tpl
name: "Provider Test"
version: "1.0.0"
description: "x"
author: "QA"
phases:
  - id: draft
    name: Draft
{provider_line}{tier_line}    depends_on: []
    prompt_template: "Hello {{input}}"
"""


def _write_tpl(tmp_path, provider=None, model_tier=None, fname="tpl.yaml"):
    """Write a minimal valid one-phase template, optionally carrying provider/
    model_tier on the phase. Returns the Path."""
    provider_line = f"    provider: {provider}\n" if provider is not None else ""
    tier_line = f"    model_tier: {model_tier}\n" if model_tier is not None else ""
    p = tmp_path / fname
    p.write_text(_MIN_TPL.format(provider_line=provider_line, tier_line=tier_line))
    return p


def _two_phase_template(provider_b="openrouter"):
    """Build a 2-phase linear PipelineTemplate directly (no YAML): phase 'a'
    has NO provider; phase 'b' depends on 'a' and declares provider_b. Trivial
    prompt_template so prompt-building doesn't abort."""
    phase_a = PhaseDefinition(
        id="a", name="A", depends_on=[], prompt_template="run {input}"
    )
    phase_b = PhaseDefinition(
        id="b", name="B", depends_on=["a"], prompt_template="run {input}",
        provider=provider_b,
    )
    return PipelineTemplate(
        id="rt", name="Routing", version="1.0.0", phases=[phase_a, phase_b]
    )


def _no_provider_template():
    """A two-phase linear template with NO provider on any phase (backward-compat
    shield)."""
    phase_a = PhaseDefinition(
        id="a", name="A", depends_on=[], prompt_template="run {input}"
    )
    phase_b = PhaseDefinition(
        id="b", name="B", depends_on=["a"], prompt_template="run {input}"
    )
    return PipelineTemplate(
        id="np", name="NoProv", version="1.0.0", phases=[phase_a, phase_b]
    )


def _bare_template():
    """A single trivial template usable for constructing a sequencer where only
    the resolver surface is exercised (contract 6)."""
    return PipelineTemplate(
        id="d", name="D", version="1.0.0",
        phases=[PhaseDefinition(id="p", name="P", depends_on=[],
                                prompt_template="hi")],
    )


# ============================================================================
# §0.5 urlopen capture idiom (byte-exact, copied from test_openrouter_executor)
# ============================================================================
def _or_response(content="out", prompt_tokens=10, completion_tokens=20):
    return json.dumps({
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens},
    }).encode("utf-8")


def _mock_urlopen(response_bytes):
    m = MagicMock()
    m.read.return_value = response_bytes
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


# ============================================================================
# CONTRACT 1 — Field + parse [NEW]
# ============================================================================
def test_c1_provider_field_parses_and_defaults_none(tmp_path):
    """C1: 'a phase declares provider: openrouter ... THEN the resulting
    PhaseDefinition.provider == "openrouter"'; 'a phase with NO provider: key
    -> PhaseDefinition.provider is None (default)'; direct construction accepts
    the kwarg; provider is on the known-fields allowlist (survives parse, not
    dropped)."""
    engine = TemplateEngine()

    # YAML carrying provider: openrouter -> survives parse (allowlist).
    p = _write_tpl(tmp_path, provider="openrouter")
    tpl = engine.load_template(str(p))
    draft = next(ph for ph in tpl.phases if ph.id == "draft")
    assert draft.provider == "openrouter"

    # No provider key -> default None.
    p2 = _write_tpl(tmp_path, provider=None, fname="noprov.yaml")
    tpl2 = engine.load_template(str(p2))
    draft2 = next(ph for ph in tpl2.phases if ph.id == "draft")
    assert draft2.provider is None

    # Direct construction accepts the kwarg.
    pd = PhaseDefinition(id="x", name="X", provider="anthropic")
    assert pd.provider == "anthropic"


# ============================================================================
# CONTRACT 2 — Validation: unknown provider = ERROR (the F.2 inversion) [NEW]
# ============================================================================
@pytest.mark.parametrize("bad_provider", ["gemini", "ollama", "bogus"])
def test_c2_unknown_provider_is_error_not_warning(tmp_path, bad_provider):
    """C2: 'the unknown provider is reported in the errors list (not warnings)'
    — any("provider" in e) is True, the error names the bad value and the phase
    id; AND [w for w in warnings if "provider" in w] == []."""
    engine = TemplateEngine()
    p = _write_tpl(tmp_path, provider=bad_provider, fname=f"bad_{bad_provider}.yaml")
    raw = yaml.safe_load(p.read_text())
    tpl = engine.load_template(str(p))

    errors, warnings = engine.validate_template_extended(tpl, raw)

    provider_errors = [e for e in errors if "provider" in e]
    assert provider_errors, f"unknown provider {bad_provider!r} must be an ERROR"
    # The error string names the bad provider value AND the phase id ("draft").
    assert any(bad_provider in e for e in provider_errors)
    assert any("draft" in e for e in provider_errors)
    # It is NOT a warning (the inversion).
    assert [w for w in warnings if "provider" in w] == []


@pytest.mark.parametrize("good_provider", ["anthropic", "openrouter"])
def test_c2_known_provider_no_error(tmp_path, good_provider):
    """C2 (known providers produce NO provider error): for provider: anthropic
    and provider: openrouter, [e for e in errors if "provider" in e] == []."""
    engine = TemplateEngine()
    p = _write_tpl(tmp_path, provider=good_provider, fname=f"ok_{good_provider}.yaml")
    raw = yaml.safe_load(p.read_text())
    tpl = engine.load_template(str(p))

    errors, _warnings = engine.validate_template_extended(tpl, raw)
    assert [e for e in errors if "provider" in e] == []


def test_c2_model_tier_contrast_shield_still_warning(tmp_path):
    """C2 (contrast shield — model_tier behaviour UNCHANGED): an unknown
    model_tier is STILL a warning, not an error. errors-for-model_tier is empty
    AND any("model_tier" in w for w in warnings) is True. Proves the
    provider/model_tier channels are deliberately inverted (mirror-inverse of
    TE-13). KNOWN_MODEL_TIERS stays warn-only (§0.2 [UNCHANGED])."""
    engine = TemplateEngine()
    p = _write_tpl(tmp_path, model_tier="invalid-tier", fname="badtier.yaml")
    raw = yaml.safe_load(p.read_text())
    tpl = engine.load_template(str(p))

    errors, warnings = engine.validate_template_extended(tpl, raw)
    assert [e for e in errors if "model_tier" in e] == []
    assert any("model_tier" in w for w in warnings)


# ============================================================================
# CONTRACT 3 — from_providers build matrix [NEW]
# ============================================================================
def test_c3a_both_providers_two_executors_default_first(tmp_path, monkeypatch):
    """C3(a): both providers referenced + both creds present -> 2 executors,
    default FIRST. len(runner.executors) == 2 AND executors[0] is the Anthropic
    (default) executor."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Two phases, one anthropic, one openrouter.
    a = PhaseDefinition(id="a", name="A", depends_on=[], prompt_template="hi",
                        provider="anthropic")
    b = PhaseDefinition(id="b", name="B", depends_on=["a"], prompt_template="hi",
                        provider="openrouter")
    tpl = PipelineTemplate(id="m", name="M", version="1.0.0", phases=[a, b])

    runner = PipelineRunner.from_providers(
        tpl, anthropic_api_key="sk-ant-x", openrouter_api_key="sk-or-x",
        default_provider="anthropic",
    )
    try:
        assert len(runner.executors) == 2
        first = runner.executors[0]
        assert (isinstance(first, AnthropicExecutor)
                or getattr(first, "provider_name", "") == "anthropic")
    finally:
        runner.close()


def test_c3b_default_first_under_reversed_declaration(tmp_path, monkeypatch):
    """C3(b): ORDER contract under reversed declaration. First phase declares
    provider: openrouter, later phase provider: anthropic, default anthropic ->
    executors[0] is STILL the Anthropic (default) executor. Declaration order
    does not change index 0; only default_provider does (backward-compat
    invariant: index 0 = no-provider fallback)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = PhaseDefinition(id="a", name="A", depends_on=[], prompt_template="hi",
                        provider="openrouter")  # openrouter declared FIRST
    b = PhaseDefinition(id="b", name="B", depends_on=["a"], prompt_template="hi",
                        provider="anthropic")
    tpl = PipelineTemplate(id="r", name="R", version="1.0.0", phases=[a, b])

    runner = PipelineRunner.from_providers(
        tpl, anthropic_api_key="sk-ant-x", openrouter_api_key="sk-or-x",
        default_provider="anthropic",
    )
    try:
        assert len(runner.executors) == 2
        first = runner.executors[0]
        assert (isinstance(first, AnthropicExecutor)
                or getattr(first, "provider_name", "") == "anthropic")
    finally:
        runner.close()


def test_c3c_openrouter_cred_missing_raises_named(tmp_path, monkeypatch):
    """C3(c): openrouter referenced, OPENROUTER_API_KEY MISSING -> eager
    ValueError whose message contains "openrouter" (ci) AND
    "OPENROUTER_API_KEY". (§VALUES assertion discipline: stable substrings, not
    equality.)"""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = PhaseDefinition(id="a", name="A", depends_on=[], prompt_template="hi",
                        provider="openrouter")
    tpl = PipelineTemplate(id="o", name="O", version="1.0.0", phases=[a])

    with pytest.raises(ValueError) as excinfo:
        PipelineRunner.from_providers(
            tpl, anthropic_api_key="sk-ant-x", default_provider="anthropic"
        )
    msg = str(excinfo.value)
    assert "openrouter" in msg.lower()
    assert "OPENROUTER_API_KEY" in msg


def test_c3d_anthropic_cred_missing_raises_named(tmp_path, monkeypatch):
    """C3(d): anthropic credential MISSING -> eager ValueError naming
    "anthropic" (ci) AND "ANTHROPIC_API_KEY". Template references openrouter as a
    phase with default_provider=anthropic, so anthropic is referenced as the
    default; only openrouter_api_key supplied."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = PhaseDefinition(id="a", name="A", depends_on=[], prompt_template="hi",
                        provider="openrouter")
    tpl = PipelineTemplate(id="d", name="D", version="1.0.0", phases=[a])

    with pytest.raises(ValueError) as excinfo:
        PipelineRunner.from_providers(
            tpl, openrouter_api_key="sk-or-x", default_provider="anthropic"
        )
    msg = str(excinfo.value)
    assert "anthropic" in msg.lower()
    assert "ANTHROPIC_API_KEY" in msg


def test_c3e_unknown_provider_runtime_guard_raises(tmp_path, monkeypatch):
    """C3(e): unknown provider in template -> from_providers raises (runtime
    guard, INV-2). Message names "gemini" AND references the known set (contains
    both "anthropic" and "openrouter"). This is the run-time rejection orch run
    relies on. [Substrings, not equality.]"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    a = PhaseDefinition(id="a", name="A", depends_on=[], prompt_template="hi",
                        provider="gemini")
    tpl = PipelineTemplate(id="g", name="G", version="1.0.0", phases=[a])

    with pytest.raises(ValueError) as excinfo:
        PipelineRunner.from_providers(
            tpl, anthropic_api_key="sk-ant-x", default_provider="anthropic"
        )
    msg = str(excinfo.value)
    assert "gemini" in msg.lower()
    assert "anthropic" in msg.lower()
    assert "openrouter" in msg.lower()


def test_c3f_no_provider_builds_single_default_executor(tmp_path, monkeypatch):
    """C3(f): NO provider fields -> existing factory path / 1-executor build.
    len(runner.executors) == 1 (the default); auto-upgrade does not multiply
    executors when nothing is referenced beyond the default, and it does NOT
    error."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tpl = _no_provider_template()

    runner = PipelineRunner.from_providers(
        tpl, anthropic_api_key="sk-ant-x", default_provider="anthropic"
    )
    try:
        assert len(runner.executors) == 1
        first = runner.executors[0]
        assert (isinstance(first, AnthropicExecutor)
                or getattr(first, "provider_name", "") == "anthropic")
    finally:
        runner.close()


# ============================================================================
# CONTRACT 4 — Selection routing in one run [NEW] (the headline)
# ============================================================================
def test_c4_two_phases_route_to_distinct_executors(tmp_path):
    """C4: phase A (no provider) served by executors[0] (Anthropic/default fake)
    and phase B (provider: openrouter) served by the OpenRouter fake — in ONE
    run. 'a' in fake_anthropic.served AND 'a' not in fake_openrouter.served;
    'b' in fake_openrouter.served AND 'b' not in fake_anthropic.served.

    Full-run form (STRONGLY PREFERRED per §Contract 4). Recorder keys on
    task.payload['phase_id'] which the sequencer sets."""
    fake_anthropic = FakeAnthropicExec("anthropic")
    fake_openrouter = FakeOpenRouterExec("openrouter")
    runner = PipelineRunner(executors=[fake_anthropic, fake_openrouter])
    try:
        tpl = _two_phase_template(provider_b="openrouter")
        seq = PhaseSequencer(tpl, runner)
        seq.execute({"input": "go"})

        assert "a" in fake_anthropic.served
        assert "a" not in fake_openrouter.served
        assert "b" in fake_openrouter.served
        assert "b" not in fake_anthropic.served
    finally:
        runner.close()


# ============================================================================
# CONTRACT 5 — Backward-compat shield (no-provider template = today) [GREEN]
# ============================================================================
def test_c5_no_provider_template_only_first_executor_runs(tmp_path):
    """C5: with NO provider on any phase and multiple executors, every phase is
    served by executors[0] (first-can_handle default); fake_openrouter.served ==
    [] — the second executor is never selected. Proves the first-can_handle
    ordering contract survives. [GREEN shield: touches no [NEW] symbol.]"""
    fake_anthropic = FakeAnthropicExec("anthropic")
    fake_openrouter = FakeOpenRouterExec("openrouter")
    runner = PipelineRunner(executors=[fake_anthropic, fake_openrouter])
    try:
        tpl = _no_provider_template()
        seq = PhaseSequencer(tpl, runner)
        seq.execute({"input": "go"})

        assert fake_openrouter.served == []
        # Both phases ran on index 0.
        assert set(fake_anthropic.served) == {"a", "b"}
    finally:
        runner.close()


def test_c5_standalone_single_executor_sanity(monkeypatch):
    """C5 (corroborating sanity, not a duplication of test_pipeline_runner.py):
    PipelineRunner.standalone(api_key="sk-ant-x") yields
    len(runner.executors) == 1."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = PipelineRunner.standalone(api_key="sk-ant-x")
    try:
        assert len(runner.executors) == 1
    finally:
        runner.close()


# ============================================================================
# CONTRACT 6 — Dialogue regression shield ([CHANGED] resolver, behaviour pinned)
# ============================================================================
def test_c6_resolve_dialogue_executor_baseline(tmp_path):
    """C6 (NARROWED LOUDLY): the #677 dialogue tests construct DialogueRunner
    directly and BYPASS _resolve_dialogue_executor, so the full dialogue path is
    too heavy / off-point for a sealed routing test. Therefore pin the resolver's
    PUBLIC surface directly via a constructed PhaseSequencer (the shim dialogue
    delegates through after the refactor). All three results are the live-probed
    §VALUES baseline that MUST survive the [CHANGED]-to-shim refactor (result
    pinned, not the matching path). [GREEN shield on today-real behaviour.]"""
    tpl = _bare_template()

    # (1) by-name resolution preserved: picks the named Anthropic over the other.
    anthro = AnthropicExecutor(api_key="sk-ant-x")
    seq = PhaseSequencer(
        tpl, PipelineRunner(executors=[anthro, DryRunExecutor(delay_seconds=0)])
    )
    try:
        assert seq._resolve_dialogue_executor("anthropic") is anthro
        # (3) no-match returns None (mixed list, NOT all-dry-run, no openrouter).
        assert seq._resolve_dialogue_executor("openrouter") is None
    finally:
        seq.runner.close()

    # (2) all-dry-run fallback preserved (INV-6): every executor dry-run ->
    # returns the DryRunExecutor (makes --mode dry-run mixed validation work).
    dry = DryRunExecutor(delay_seconds=0)
    seq2 = PhaseSequencer(tpl, PipelineRunner(executors=[dry]))
    try:
        assert seq2._resolve_dialogue_executor("openrouter") is dry
    finally:
        seq2.runner.close()


# ============================================================================
# CONTRACT 7 — Auto-upgrade ergonomics via orch run [NEW] (CLI-observable)
# ============================================================================
def _cli_tpl(tmp_path):
    """Mixed-provider template for the CLI: one phase provider: openrouter +
    one default/no-provider phase. Prompts have NO placeholders to avoid the
    #535 unresolved-placeholder guard (§0.10 / §Contract 7)."""
    p = tmp_path / "mixed.yaml"
    p.write_text(
        "id: mixed-cli\n"
        'name: "Mixed CLI"\n'
        'version: "1.0.0"\n'
        'description: "x"\n'
        'author: "QA"\n'
        "phases:\n"
        "  - id: first\n"
        "    name: First\n"
        "    depends_on: []\n"
        '    prompt_template: "Hello"\n'
        "  - id: second\n"
        "    name: Second\n"
        "    provider: openrouter\n"
        "    depends_on: [first]\n"
        '    prompt_template: "Hello"\n'
    )
    return p


def test_c7a_orch_run_both_creds_builds_two_executors(tmp_path, monkeypatch):
    """C7(a): both creds present -> orch run succeeds (exit_code == 0) and the
    auto-upgrade builds 2 executors, default FIRST. Spy on from_providers to make
    'builds 2 executors' CLI-observable. LLM calls mocked offline (AnthropicExecutor
    ._call_api + OpenRouterExecutor._do_post)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tpl = _cli_tpl(tmp_path)
    out_dir = tmp_path / "out"

    real = PipelineRunner.from_providers.__func__  # unwrap classmethod
    captured = {}

    def spy(template, *a, **kw):
        r = real(PipelineRunner, template, *a, **kw)
        captured["n"] = len(r.executors)
        captured["first"] = type(r.executors[0]).__name__
        return r

    anthropic_dict = {
        "content": [{"type": "text", "text": "APPROVE\n\nok"}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    openrouter_dict = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    with patch.object(PipelineRunner, "from_providers", staticmethod(spy)), \
            patch.object(AnthropicExecutor, "_call_api",
                         return_value=anthropic_dict), \
            patch.object(OpenRouterExecutor, "_do_post",
                         return_value=openrouter_dict):
        result = CliRunner().invoke(
            main,
            ["run", str(tpl), "--mode", "standalone", "--api-key", "sk-ant-x",
             "--output-dir", str(out_dir)],
            env={"OPENROUTER_API_KEY": "sk-or-x"},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, result.output
    assert captured.get("n") == 2          # auto-upgrade built TWO executors
    assert captured.get("first") == "AnthropicExecutor"  # default first


def test_c7b_orch_run_missing_openrouter_cred_nonzero_named(tmp_path, monkeypatch):
    """C7(b): openrouter cred MISSING -> from_providers raises eagerly, caught by
    the CLI's except ValueError -> echo + sys.exit(1). result.exit_code != 0 AND
    result.output contains "openrouter" (ci) AND "OPENROUTER_API_KEY". The delenv
    is MANDATORY (§0.6: env= cannot remove an inherited OPENROUTER_API_KEY).

    Hermeticity backstop (§0.5 unconditional no-network mandate): transport sealed
    via the two class-level patches (AnthropicExecutor._call_api +
    OpenRouterExecutor._do_post); at HEAD the run would otherwise execute live —
    the eager raise only exists post-impl — so the seal keeps the test
    RED-by-assertion (never RED-by-network), and post-impl the eager raise fires
    before the mocks are reached."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # MANDATORY
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tpl = _cli_tpl(tmp_path)
    out_dir = tmp_path / "out"

    with patch.object(AnthropicExecutor, "_call_api", return_value={
        "content": [{"type": "text", "text": "x"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }), patch.object(OpenRouterExecutor, "_do_post", return_value={
        "choices": [{"message": {"content": "x"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }):
        result = CliRunner().invoke(
            main,
            ["run", str(tpl), "--mode", "standalone", "--api-key", "sk-ant-x",
             "--output-dir", str(out_dir)],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "openrouter" in result.output.lower()
    assert "OPENROUTER_API_KEY" in result.output


# ============================================================================
# CONTRACT 8 — Tier flow through the TARGET executor's model_map [GREEN]
# ============================================================================
def test_c8_tier_resolves_through_target_executor_model_map():
    """C8: OpenRouterExecutor with custom model_map={"sonnet": "openai/gpt-4o"};
    execute(task, model_tier="sonnet") with disable_tools=True (single-shot
    urlopen path) -> sent request body's "model" == "openai/gpt-4o". Proves the
    phase's model_tier resolved through the TARGET executor's own model_map, NOT
    Anthropic's registry ('zero new tier plumbing'). Mirrors
    test_custom_model_map_overrides_defaults. [GREEN shield: today-real surfaces;
    sanity-check the custom map is genuinely distinct from DEFAULT_MODEL_MAP.]"""
    # Sanity: the custom value is unambiguously distinct from the default.
    assert DEFAULT_MODEL_MAP["sonnet"] != "openai/gpt-4o"

    executor = OpenRouterExecutor(
        api_key="sk-or-x", model_map={"sonnet": "openai/gpt-4o"}
    )
    task = TaskSpec(
        type=TaskType.CODE,
        payload={"prompt": "hi", "disable_tools": True},
    )
    try:
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = _mock_urlopen(_or_response())
            executor.execute(task, worker_id="w", model_tier="sonnet")
            sent_body = json.loads(mock_url.call_args[0][0].data)
            assert sent_body["model"] == "openai/gpt-4o"
    finally:
        if hasattr(executor, "close"):
            executor.close()


# success
