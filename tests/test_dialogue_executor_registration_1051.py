"""Focused tests for runtime dialogue-participant executor registration (#1051).

Two independent, surgical fixes are pinned here:

**Fix A — runtime registration at the sequencer dialogue chokepoint.** A
dialogue phase names its drafter/reviewer executors under
``dialogue_config.{drafter,reviewer}.executor`` — NOT the per-phase ``provider:``
field — so no mode factory (standalone/openrouter/...) ever builds the
``GeminiCliExecutor`` a ``gemini_cli`` participant requires. Pre-fix, a real
(non-dry-run) run of ``spec-review-dialogue.yaml`` fails at dispatch with
``dialogue_executor_unresolved`` before any model call, for EVERY entrypoint
(CLI + daemon + web + eval + programmatic). The fix registers the executor lazily
at ``PhaseSequencer._resolve_dialogue_executor`` — the single chokepoint all
dialogue lookups funnel through — guarded against dry-run and idempotent.

**Fix B — ``disable_tools`` default for dialogue participants.**
``DialogueParticipant`` gains a declared ``disable_tools: bool = True`` field, and
``DialogueRunner._invoke`` threads it into the executor payload so the OpenRouter
drafter runs text-in/text-out by default (not as an agentic tool-using agent),
with explicit per-participant opt-out.

No live ``gemini`` binary, no API keys, no Click. The GeminiCliExecutor is
side-effect-free at construction (``shutil.which`` only at execute time), so the
registration assertions never touch a subprocess; the Fix-B assertions use a
scripted executor double that records the payload, mirroring
``tests/test_dialogue_phase.py``'s ``_ScriptedExecutor``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List

from orchestration_engine.dialogue_phase import (
    DialogueParticipant,
    DialoguePhaseConfig,
    run_dialogue,
)
from orchestration_engine.pipeline_runner import PipelineRunner
from orchestration_engine.schemas import TaskResult, TaskState, TaskType
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import TemplateEngine

# Stable test fixture (mirrors the bundled spec-review-dialogue.yaml) lives in
# examples/, not templates/, per the #632 lint rule that forbids tests from
# referencing the production templates dir by hardcoded path.
TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "examples" / "spec-review-dialogue-fixture.yaml"
)


# ---------------------------------------------------------------------------
# Executor doubles
# ---------------------------------------------------------------------------


class _StubExecutor:
    """Duck-typed executor exposing a ``provider_name`` and a recording
    ``execute(...)``. Mirrors ``_ScriptedExecutor`` (test_dialogue_phase.py) but
    returns a single canned result and stores every ``task_payload`` seen."""

    def __init__(self, provider_name: str, output: str = "APPROVED\nok"):
        self.provider_name = provider_name
        self._output = output
        self.calls: List[dict] = []

    def can_handle(self, task_type: TaskType) -> bool:  # noqa: ARG002
        return True

    def execute(self, task, worker_id=None, model_tier=None, thinking_level=None):
        self.calls.append(
            {
                "worker_id": worker_id,
                "model_tier": model_tier,
                "thinking_level": thinking_level,
                "task_payload": dict(task.payload),
            }
        )
        return TaskResult(
            task_id="stub-task",
            task_type=TaskType.REVIEW,
            state=TaskState.SUCCESS,
            confidence=0.8,
            result={"output": self._output},
            errors=[],
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            model_used="stub-model",
            tokens_consumed=10,
            execution_time_seconds=0.01,
            cost_usd=Decimal("0.0"),
        )


class _DryRunLikeExecutor(_StubExecutor):
    """A stub whose *class name* contains ``DryRun`` so the all-dry-run guard and
    the all-dry-run fallback both treat it as a dry-run executor — without
    importing the real DryRunExecutor (which would carry no ``provider_name``)."""

    def __init__(self):
        super().__init__(provider_name="")


def _load_template():
    return TemplateEngine().load_template(TEMPLATE_PATH)


def _gemini_count(executors) -> int:
    return sum(1 for e in executors if type(e).__name__ == "GeminiCliExecutor")


# ===========================================================================
# Assertion 1 — registration via the helper resolves end-to-end (helper path)
# ===========================================================================


def test_build_gemini_executor_constructs_real_executor_no_subprocess():
    """``_build_gemini_executor`` returns a real GeminiCliExecutor with
    ``provider_name == "gemini"`` and the supplied default_model — and runs NO
    subprocess at construction (side-effect-free)."""
    ge = PipelineRunner._build_gemini_executor(default_model="gemini-3.1-pro-preview")
    assert type(ge).__name__ == "GeminiCliExecutor"
    assert ge.provider_name == "gemini"
    assert ge.default_model == "gemini-3.1-pro-preview"


def test_append_dialogue_executors_registers_and_resolves_end_to_end():
    """The helper scans the real template, appends a GeminiCliExecutor, and the
    sequencer resolves ``gemini_cli`` to THAT exact instance (identity)."""
    template = _load_template()
    runner = PipelineRunner(
        executors=[_StubExecutor(provider_name="openrouter")], db_path=":memory:"
    )

    PipelineRunner._append_dialogue_executors(runner.executors, template)

    # SECONDARY: a GeminiCliExecutor is now present.
    assert _gemini_count(runner.executors) == 1
    appended = next(e for e in runner.executors if type(e).__name__ == "GeminiCliExecutor")

    # PRIMARY: end-to-end resolution returns the appended instance by IDENTITY,
    # pinning the gemini_cli -> gemini -> provider_name resolution.
    seq = PhaseSequencer(template, runner)
    resolved = seq._resolve_executor_by_name("gemini_cli")
    assert resolved is appended
    assert resolved is not None


def test_append_dialogue_executors_is_idempotent():
    """A second ``_append_dialogue_executors`` call adds no duplicate."""
    template = _load_template()
    runner = PipelineRunner(
        executors=[_StubExecutor(provider_name="openrouter")], db_path=":memory:"
    )
    PipelineRunner._append_dialogue_executors(runner.executors, template)
    PipelineRunner._append_dialogue_executors(runner.executors, template)
    assert _gemini_count(runner.executors) == 1


# ===========================================================================
# Assertion 1b — registration fires at the SEQUENCER chokepoint with NO manual
# helper call (the daemon-style path — the real acceptance gate for ALL
# entrypoints). RED on main without the fix; GREEN with it.
# ===========================================================================


def test_chokepoint_registers_gemini_with_no_manual_append():
    """Build a PhaseSequencer exactly as the daemon does — a non-dry-run runner
    with gemini ABSENT and NO manual ``_append_dialogue_executors`` — then assert
    the reviewer resolves to a real GeminiCliExecutor through the REAL dispatch
    resolver. Proves the lazy ``_register_dialogue_executors_once`` pre-pass
    registered the executor at the chokepoint the daemon/web actually hit."""
    template = _load_template()
    openrouter_stub = _StubExecutor(provider_name="openrouter")
    runner = PipelineRunner(executors=[openrouter_stub], db_path=":memory:")

    # Daemon-style: NO manual append. Gemini is absent before the first resolve.
    assert _gemini_count(runner.executors) == 0

    seq = PhaseSequencer(template, runner)

    reviewer = seq._resolve_dialogue_executor("gemini_cli")
    assert reviewer is not None
    assert type(reviewer).__name__ == "GeminiCliExecutor"
    assert reviewer.provider_name == "gemini"

    # The drafter still resolves to the openrouter stub (executors[0] preserved).
    drafter = seq._resolve_dialogue_executor("openrouter")
    assert drafter is openrouter_stub


def test_chokepoint_idempotent_across_repeated_resolves():
    """The resolver is called twice per dialogue phase (drafter + reviewer); a
    second ``gemini_cli`` resolve returns the SAME object and the runner still
    holds exactly one GeminiCliExecutor."""
    template = _load_template()
    runner = PipelineRunner(
        executors=[_StubExecutor(provider_name="openrouter")], db_path=":memory:"
    )
    seq = PhaseSequencer(template, runner)

    first = seq._resolve_dialogue_executor("gemini_cli")
    second = seq._resolve_dialogue_executor("gemini_cli")
    assert first is second
    assert _gemini_count(runner.executors) == 1


# ===========================================================================
# Assertion 1c — dry-run chokepoint guard: no append when ALL executors are
# DryRunExecutors (the all-dry-run fallback / template-validation is preserved).
# ===========================================================================


def test_chokepoint_dry_run_guard_skips_append():
    """An all-dry-run runner does NOT get a gemini append; the resolve returns
    the dry-run executor via the all-dry-run fallback, byte-preserving the
    dry-run template-validation behaviour."""
    template = _load_template()
    dry_stub = _DryRunLikeExecutor()
    runner = PipelineRunner(executors=[dry_stub], db_path=":memory:")
    seq = PhaseSequencer(template, runner)

    resolved = seq._resolve_dialogue_executor("gemini_cli")
    assert resolved is dry_stub
    # No GeminiCliExecutor was appended.
    assert _gemini_count(runner.executors) == 0
    assert len(runner.executors) == 1


# ===========================================================================
# Assertion 2 — DialogueRunner._invoke puts disable_tools=True in the drafter
# payload by default + the per-participant opt-out threads through.
# ===========================================================================


def _approve_first_round_config(*, drafter: DialogueParticipant) -> DialoguePhaseConfig:
    return DialoguePhaseConfig(
        drafter=drafter,
        reviewer=DialogueParticipant(executor="gemini_cli", model="gemini-3.1-pro"),
        max_rounds=4,
        convergence_signal="APPROVED",
    )


def test_invoke_defaults_disable_tools_true(tmp_path):
    """A drafter that omits ``disable_tools`` yields a payload with
    ``disable_tools is True`` (the declared field default)."""
    drafter_exec = _StubExecutor(provider_name="openrouter", output="# Draft v1")
    reviewer_exec = _StubExecutor(provider_name="gemini", output="APPROVED\nok")
    config = _approve_first_round_config(
        drafter=DialogueParticipant(executor="openrouter", model_tier="opus")
    )

    run_dialogue(
        phase_config=config,
        drafter_executor=drafter_exec,
        reviewer_executor=reviewer_exec,
        initial_input="rough spec",
        output_dir=tmp_path,
    )

    assert drafter_exec.calls, "drafter executor was never invoked"
    assert drafter_exec.calls[0]["task_payload"]["disable_tools"] is True


def test_invoke_opt_out_disable_tools_false(tmp_path):
    """A drafter with ``disable_tools=False`` yields a payload with
    ``disable_tools is False`` — proving the field threads, not a hardcode."""
    drafter_exec = _StubExecutor(provider_name="openrouter", output="# Draft v1")
    reviewer_exec = _StubExecutor(provider_name="gemini", output="APPROVED\nok")
    config = _approve_first_round_config(
        drafter=DialogueParticipant(executor="openrouter", model_tier="opus", disable_tools=False)
    )

    run_dialogue(
        phase_config=config,
        drafter_executor=drafter_exec,
        reviewer_executor=reviewer_exec,
        initial_input="rough spec",
        output_dir=tmp_path,
    )

    assert drafter_exec.calls, "drafter executor was never invoked"
    assert drafter_exec.calls[0]["task_payload"]["disable_tools"] is False


def test_disable_tools_field_default_on_participant():
    """The declared field defaults to True and is overridable via YAML/extra."""
    assert DialogueParticipant(executor="openrouter").disable_tools is True
    assert DialogueParticipant(executor="openrouter", disable_tools=False).disable_tools is False


# ===========================================================================
# Assertion 3 — an unknown executor name still fast-fails (no silent dry-run
# substitution outside dry-run); the scan does not over-register.
# ===========================================================================


def test_unknown_executor_name_resolves_to_none_non_dry_run():
    """A genuinely-unknown name returns None in a non-dry-run runner — no silent
    substitution."""
    template = _load_template()
    runner = PipelineRunner(
        executors=[_StubExecutor(provider_name="openrouter")], db_path=":memory:"
    )
    seq = PhaseSequencer(template, runner)
    assert seq._resolve_dialogue_executor("totally_unknown_executor") is None


def test_scan_does_not_over_register_for_builtin_only_participants():
    """``_append_dialogue_executors`` appends nothing for a dialogue template
    whose participants name only built-in providers (anthropic/openrouter)."""

    class _Participant:
        def __init__(self, executor, model=None):
            self.executor = executor
            self.model = model

    class _DC:
        def __init__(self):
            self.drafter = _Participant("openrouter")
            self.reviewer = _Participant("anthropic")

    class _Phase:
        dialogue_config = _DC()

    class _Template:
        phases = [_Phase()]

    executors = [_StubExecutor(provider_name="openrouter")]
    before = len(executors)
    PipelineRunner._append_dialogue_executors(executors, _Template())
    assert len(executors) == before
    assert _gemini_count(executors) == 0
