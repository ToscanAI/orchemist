"""Unit tests for the sequencer's generic adversary dispatch (Issue #702).

Covers issue AC #9: generic-path dispatch, legacy-fallback dispatch, escalation
via ``escalation_partner``, and reward persistence with a config-driven filename.

These are the implementer's own unit tests — distinct from the sealed acceptance
suite. They drive the dispatch logic both directly (via ``_record_adversary_outcome``)
and end-to-end through ``StateMachineSequencer.execute`` for the escalation paths.
"""

import json
import logging
from unittest import mock

from orchestration_engine.adversary_parser import AdversaryConfig
from orchestration_engine.schemas import TaskResult, TaskState
from orchestration_engine.sequencer import PhaseSequencer, StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


def _make_runner_stub(phase_text):
    """A minimal MagicMock runner whose single executor returns a SUCCESS result
    carrying ``phase_text`` under ``result["text"]`` (the channel _extract_phase_text reads).
    """
    runner = mock.MagicMock()
    store = {}

    def _submit(spec, **kwargs):  # noqa: ARG001
        store[spec.id] = spec
        return spec.id

    def _get(task_id, **kwargs):  # noqa: ARG001
        return store[task_id]

    runner.queue.submit_task.side_effect = _submit
    runner.queue.get_task.side_effect = _get

    executor = mock.MagicMock()
    executor.can_handle.return_value = True

    def _execute(spec, **kwargs):  # noqa: ARG001
        return TaskResult(
            task_id=spec.id,
            task_type=spec.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": phase_text},
        )

    executor.execute.side_effect = _execute
    runner.executors = [executor]
    return runner


def _result(text):
    """A phase-result dict shaped like the sequencer's completed-phase result."""
    return {"result": {"text": text}}


# ---------------------------------------------------------------------------
# Generic dispatch
# ---------------------------------------------------------------------------


def test_generic_dispatch_uses_adversary_parser_not_spec_adversary(tmp_path):
    """A phase with adversary_config (reward_enabled=True) writes its config-named reward
    file via the generic parser; the legacy spec_adversary module is not consulted."""
    config = AdversaryConfig(
        valid_categories=["custom_cat"],
        reward_enabled=True,
        reward_filename="custom_reward.json",
    )
    phase = PhaseDefinition(id="my_reviewer", name="rev", adversary_config=config)
    runner = _make_runner_stub("")
    seq = PhaseSequencer(
        PipelineTemplate(id="t", name="T", phases=[phase]),
        runner,
        output_dir=str(tmp_path),
    )

    with mock.patch("orchestration_engine.spec_adversary.parse_adversary_output") as legacy_mock:
        seq._record_adversary_outcome(phase, _result("VERDICT: REQUEST_CHANGES\n[custom_cat] a"))

    legacy_mock.assert_not_called()
    assert (tmp_path / "custom_reward.json").exists()
    payload = json.loads((tmp_path / "custom_reward.json").read_text())
    assert payload["verdict"] == "REQUEST_CHANGES"
    assert isinstance(payload["reward_score"], float)


def test_legacy_fallback_dispatch_uses_spec_adversary_module(tmp_path):
    """A phase with adversary_config=None and id='spec_adversary' uses the legacy module
    (writes adversary_reward.json) and does NOT invoke the generic compute_reward."""
    phase = PhaseDefinition(id="spec_adversary", name="adv")
    assert phase.adversary_config is None
    runner = _make_runner_stub("")
    seq = PhaseSequencer(
        PipelineTemplate(id="t", name="T", phases=[phase]),
        runner,
        output_dir=str(tmp_path),
    )

    with mock.patch("orchestration_engine.adversary_parser.compute_reward") as generic_mock:
        seq._record_adversary_outcome(phase, _result("VERDICT: REQUEST_CHANGES\n[trivial] a"))

    generic_mock.assert_not_called()
    assert (tmp_path / "adversary_reward.json").exists()
    payload = json.loads((tmp_path / "adversary_reward.json").read_text())
    assert isinstance(payload["reward_score"], int)


def test_reward_enabled_false_skips_persist(tmp_path):
    """A generic phase with reward_enabled=False writes no reward file."""
    config = AdversaryConfig(
        valid_categories=["custom_cat"],
        reward_enabled=False,
        reward_filename="custom_reward.json",
    )
    phase = PhaseDefinition(id="rev", name="rev", adversary_config=config)
    runner = _make_runner_stub("")
    seq = PhaseSequencer(
        PipelineTemplate(id="t", name="T", phases=[phase]),
        runner,
        output_dir=str(tmp_path),
    )

    seq._record_adversary_outcome(phase, _result("VERDICT: REQUEST_CHANGES\n[custom_cat] a"))

    assert not (tmp_path / "custom_reward.json").exists()
    assert not (tmp_path / "adversary_reward.json").exists()


def test_no_adversary_config_non_spec_phase_is_noop(tmp_path):
    """A phase with no adversary_config and id != 'spec_adversary' writes nothing,
    raises nothing."""
    phase = PhaseDefinition(id="some_other_phase", name="x")
    runner = _make_runner_stub("")
    seq = PhaseSequencer(
        PipelineTemplate(id="t", name="T", phases=[phase]),
        runner,
        output_dir=str(tmp_path),
    )

    seq._record_adversary_outcome(phase, _result("VERDICT: REQUEST_CHANGES\n[x] a"))

    assert list(tmp_path.iterdir()) == []


def test_reward_enabled_true_unwritable_output_dir_no_crash(tmp_path, caplog):
    """A generic phase whose output_dir does not exist warns and continues (no crash,
    no file)."""
    bad_dir = tmp_path / "does_not_exist"
    config = AdversaryConfig(
        valid_categories=["custom_cat"],
        reward_enabled=True,
        reward_filename="custom_reward.json",
    )
    phase = PhaseDefinition(id="rev", name="rev", adversary_config=config)
    runner = _make_runner_stub("")
    seq = PhaseSequencer(
        PipelineTemplate(id="t", name="T", phases=[phase]),
        runner,
        output_dir=str(bad_dir),
    )

    caplog.set_level(logging.WARNING, logger="orchestration_engine")
    seq._record_adversary_outcome(
        phase, _result("VERDICT: REQUEST_CHANGES\n[custom_cat] a")
    )  # must not raise

    assert not (bad_dir / "custom_reward.json").exists()
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


# ---------------------------------------------------------------------------
# Escalation via escalation_partner
# ---------------------------------------------------------------------------


def test_escalation_fires_via_escalation_partner(tmp_path):
    """An exhausted reviewed phase naming an adversary via escalation_partner, with the
    partner's REQUEST_CHANGES output seeded in phase_outputs, sets escalation_required."""
    reviewed = PhaseDefinition(
        id="review_phase",
        name="review_phase",
        max_iterations=2,
        transitions={"success": "review_phase"},
        escalation_partner="my_adversary",
    )
    template = PipelineTemplate(id="t", name="T", phases=[reviewed])
    runner = _make_runner_stub("phase work output")
    seq = StateMachineSequencer(template, runner, output_dir=str(tmp_path))
    seq.phase_outputs["my_adversary"] = _result(
        "VERDICT: REQUEST_CHANGES\n[trivial] one\n[vague] two"
    )

    result = seq.execute({})

    assert result["abort_reason"] == "MAX_ITERATIONS_EXCEEDED"
    assert result["escalation_required"] is True
    assert result["escalation_reason"] == "my_adversary_loop_exhausted"
    assert isinstance(result["adversary_findings"], list)
    assert len(result["adversary_findings"]) == 2
    for f in result["adversary_findings"]:
        assert set(f.keys()) == {"category", "description"}


def test_escalation_partner_missing_output_logs_warning_and_skips(tmp_path, caplog):
    """When escalation_partner names a phase absent from phase_outputs, the run aborts
    normally, sets no escalation_required key, and logs a warning."""
    reviewed = PhaseDefinition(
        id="spec",
        name="spec",
        max_iterations=2,
        transitions={"success": "spec"},
        escalation_partner="some_adversary",
    )
    template = PipelineTemplate(id="t", name="T", phases=[reviewed])
    runner = _make_runner_stub("phase work output")
    seq = StateMachineSequencer(template, runner, output_dir=str(tmp_path))
    # Deliberately do NOT seed "some_adversary".

    caplog.set_level(logging.WARNING, logger="orchestration_engine")
    result = seq.execute({})  # must not raise

    assert result["abort_reason"] == "MAX_ITERATIONS_EXCEEDED"
    assert "escalation_required" not in result
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_no_escalation_partner_no_escalation_check(tmp_path):
    """An exhausted phase with escalation_partner=None sets none of the escalation keys —
    even when a 'spec_adversary' REQUEST_CHANGES output is present in phase_outputs."""
    reviewed = PhaseDefinition(
        id="spec",
        name="spec",
        max_iterations=2,
        transitions={"success": "spec"},
        # escalation_partner intentionally omitted -> None
    )
    template = PipelineTemplate(id="t", name="T", phases=[reviewed])
    runner = _make_runner_stub("phase work output")
    seq = StateMachineSequencer(template, runner, output_dir=str(tmp_path))
    seq.phase_outputs["spec_adversary"] = _result("VERDICT: REQUEST_CHANGES\n[trivial] a finding")

    result = seq.execute({})

    assert result["abort_reason"] == "MAX_ITERATIONS_EXCEEDED"
    assert "escalation_required" not in result
    assert "escalation_reason" not in result
    assert "adversary_findings" not in result
