"""Tests for the dialogue phase prototype (Track B / Issue #677).

Covers the nine acceptance test cases from the Track B spec:

1. ``test_converges_in_round_1``        — reviewer approves first draft
2. ``test_converges_in_round_3``        — reviewer requests changes twice then approves
3. ``test_max_rounds_hit``              — reviewer never approves; phase still completes
4. ``test_history_accumulates``         — by round 3 drafter sees rounds 1 & 2 reviews
5. ``test_drafter_timeout``             — drafter executor raises timeout cleanly
6. ``test_reviewer_timeout``            — reviewer executor raises timeout cleanly
7. ``test_round_files_written``         — round-N-draft.md and round-N-review.md on disk
8. ``test_drift_indicator_fires``       — two near-identical drafts → convergence_stall warning
9. ``test_cost_summed_per_round``       — total_cost equals sum(round.cost) — no 3× inflation

All tests use mocked executors via ``unittest.mock`` — no real API calls.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import pytest

from orchestration_engine.dialogue_phase import (
    DialogueParticipant,
    DialoguePhaseConfig,
    DialogueResult,
    DialogueRound,
    DialogueRunner,
    DRIFT_SIMILARITY_THRESHOLD,
    _jaccard_similarity,
    run_dialogue,
)
from orchestration_engine.schemas import (
    TaskError,
    TaskResult,
    TaskState,
    TaskType,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mk_result(
    text: str,
    *,
    cost: float = 0.01,
    tokens: int = 100,
    state: TaskState = TaskState.SUCCESS,
    errors: Optional[List[TaskError]] = None,
) -> TaskResult:
    """Build a TaskResult with a controlled ``result['output']`` string."""
    return TaskResult(
        task_id="mock-task",
        task_type=TaskType.REVIEW,
        state=state,
        confidence=0.0 if state == TaskState.FAILED else 0.8,
        result={"output": text},
        errors=errors or [],
        started_at=datetime.now(),
        completed_at=datetime.now(),
        model_used="mock-model",
        tokens_consumed=tokens,
        execution_time_seconds=0.01,
        cost_usd=Decimal(str(cost)),
    )


class _ScriptedExecutor:
    """Executor double that returns a scripted list of TaskResult objects.

    Each ``execute()`` call returns the next entry from ``script`` and records
    the prompt it was given in ``calls`` for later assertions.  When the
    script is exhausted, raises ``IndexError`` (loudly — exhausted scripts
    are usually a test bug).
    """

    def __init__(self, script: List):
        self.script: List = list(script)
        self.calls: List[dict] = []
        self.index = 0

    def execute(self, task, worker_id=None, model_tier=None, thinking_level=None):
        if self.index >= len(self.script):
            raise IndexError(
                f"_ScriptedExecutor: script exhausted at call #{self.index + 1}"
            )
        entry = self.script[self.index]
        self.index += 1
        self.calls.append({
            "prompt": task.payload.get("prompt", ""),
            "worker_id": worker_id,
            "model_tier": model_tier,
            "thinking_level": thinking_level,
            "task_payload": dict(task.payload),
        })
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(task, worker_id=worker_id, model_tier=model_tier, thinking_level=thinking_level)
        return entry


def _basic_config(max_rounds: int = 4) -> DialoguePhaseConfig:
    return DialoguePhaseConfig(
        drafter=DialogueParticipant(executor="openrouter", model_tier="opus"),
        reviewer=DialogueParticipant(executor="gemini_cli", model="gemini-3.1-pro"),
        max_rounds=max_rounds,
        convergence_signal="APPROVED",
    )


# ---------------------------------------------------------------------------
# 1. Converges in round 1
# ---------------------------------------------------------------------------


def test_converges_in_round_1(tmp_path):
    drafter = _ScriptedExecutor([_mk_result("# Draft v1\nA refined spec.")])
    reviewer = _ScriptedExecutor([_mk_result("APPROVED\nLooks great.")])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=4),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.converged is True
    assert len(result.rounds) == 1
    assert result.rounds[0].approved is True
    assert "Draft v1" in result.final_draft
    assert result.error is None


# ---------------------------------------------------------------------------
# 2. Converges in round 3
# ---------------------------------------------------------------------------


def test_converges_in_round_3(tmp_path):
    drafter = _ScriptedExecutor([
        _mk_result("# Draft v1\nA first attempt."),
        _mk_result("# Draft v2\nRefined after critique 1."),
        _mk_result("# Draft v3\nFinal polished version."),
    ])
    reviewer = _ScriptedExecutor([
        _mk_result("REQUEST_CHANGES\nMissing edge cases."),
        _mk_result("REQUEST_CHANGES\nStill vague in section 2."),
        _mk_result("APPROVED\nLooks good now."),
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=5),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.converged is True
    assert len(result.rounds) == 3
    assert result.rounds[0].approved is False
    assert result.rounds[1].approved is False
    assert result.rounds[2].approved is True
    assert "Draft v3" in result.final_draft


# ---------------------------------------------------------------------------
# 3. Max rounds hit without convergence
# ---------------------------------------------------------------------------


def test_max_rounds_hit(tmp_path):
    # 4 drafts × 4 reviews, never approves
    drafter = _ScriptedExecutor([
        _mk_result(f"# Draft v{i}\nContent {i}.") for i in range(1, 5)
    ])
    reviewer = _ScriptedExecutor([
        _mk_result(f"REQUEST_CHANGES\nMore work needed (round {i}).")
        for i in range(1, 5)
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=4),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.converged is False
    assert len(result.rounds) == 4
    # Phase still completes — final_draft is the round-4 draft
    assert "Draft v4" in result.final_draft
    assert result.error is None  # No fatal error — just unconverged


# ---------------------------------------------------------------------------
# 4. History accumulates — drafter sees rounds 1 & 2 reviews by round 3
# ---------------------------------------------------------------------------


def test_history_accumulates(tmp_path):
    drafter = _ScriptedExecutor([
        _mk_result("# Draft v1"),
        _mk_result("# Draft v2"),
        _mk_result("# Draft v3"),
    ])
    reviewer = _ScriptedExecutor([
        _mk_result("REQUEST_CHANGES\nFirst critique: missing X."),
        _mk_result("REQUEST_CHANGES\nSecond critique: missing Y."),
        _mk_result("APPROVED"),
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=5),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.converged is True
    assert len(result.rounds) == 3

    # By round 3, the drafter prompt must contain:
    # - round 1 draft text
    # - round 1 reviewer critique
    # - round 2 draft text
    # - round 2 reviewer critique
    round3_drafter_prompt = drafter.calls[2]["prompt"]
    assert "Draft v1" in round3_drafter_prompt
    assert "First critique: missing X." in round3_drafter_prompt
    assert "Draft v2" in round3_drafter_prompt
    assert "Second critique: missing Y." in round3_drafter_prompt
    # Sanity: round 1 drafter call should NOT contain any history
    round1_drafter_prompt = drafter.calls[0]["prompt"]
    assert "First critique" not in round1_drafter_prompt
    assert "Draft v2" not in round1_drafter_prompt


# ---------------------------------------------------------------------------
# 5. Drafter timeout → phase fails cleanly
# ---------------------------------------------------------------------------


def test_drafter_timeout(tmp_path):
    drafter = _ScriptedExecutor([
        TimeoutError("drafter call timed out after 600s"),
    ])
    reviewer = _ScriptedExecutor([
        _mk_result("APPROVED"),  # Won't be reached
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=4),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.error is not None
    assert "drafter_failed_round_1" in result.error
    assert "timed out" in result.error
    assert result.converged is False
    assert result.succeeded is False
    # Reviewer must NOT have been called
    assert reviewer.index == 0


# ---------------------------------------------------------------------------
# 6. Reviewer timeout → phase fails cleanly
# ---------------------------------------------------------------------------


def test_reviewer_timeout(tmp_path):
    drafter = _ScriptedExecutor([_mk_result("# Draft v1")])
    reviewer = _ScriptedExecutor([
        TimeoutError("reviewer call timed out after 600s"),
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=4),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.error is not None
    assert "reviewer_failed_round_1" in result.error
    assert "timed out" in result.error
    # Drafter ran successfully in round 1; the round is recorded but unconverged
    assert result.converged is False
    assert result.succeeded is False
    # The round 1 draft text should still be available (drafter ran successfully)
    assert "Draft v1" in result.final_draft
    # One round record (the failed reviewer side leaves a partial round)
    assert len(result.rounds) == 1


# ---------------------------------------------------------------------------
# 7. Per-round files written to disk
# ---------------------------------------------------------------------------


def test_round_files_written(tmp_path):
    drafter = _ScriptedExecutor([
        _mk_result("Draft One"),
        _mk_result("Draft Two"),
    ])
    reviewer = _ScriptedExecutor([
        _mk_result("REQUEST_CHANGES\nNot yet."),
        _mk_result("APPROVED"),
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=4),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    assert result.converged is True
    # Round 1: draft + review
    p1d = tmp_path / "round-1-draft.md"
    p1r = tmp_path / "round-1-review.md"
    p2d = tmp_path / "round-2-draft.md"
    p2r = tmp_path / "round-2-review.md"
    for p in (p1d, p1r, p2d, p2r):
        assert p.exists(), f"missing transcript file: {p}"
    assert "Draft One" in p1d.read_text()
    assert "REQUEST_CHANGES" in p1r.read_text()
    assert "Draft Two" in p2d.read_text()
    assert "APPROVED" in p2r.read_text()


# ---------------------------------------------------------------------------
# 8. Drift indicator fires on near-identical drafts
# ---------------------------------------------------------------------------


def test_drift_indicator_fires(tmp_path, caplog):
    # Three near-identical drafts → two consecutive Jaccard hits → warning.
    near_identical = " ".join(f"word{i}" for i in range(50))
    # Tiny perturbation so the Jaccard is high but not exactly 1.0.
    perturbed = near_identical + " extra"
    perturbed_2 = near_identical + " extra2"

    drafter = _ScriptedExecutor([
        _mk_result(near_identical),
        _mk_result(perturbed),
        _mk_result(perturbed_2),
        _mk_result("totally different content here goes nothing similar"),
    ])
    reviewer = _ScriptedExecutor([
        _mk_result("REQUEST_CHANGES\nKeep trying."),
        _mk_result("REQUEST_CHANGES\nNope."),
        _mk_result("REQUEST_CHANGES\nStill not it."),
        _mk_result("REQUEST_CHANGES\nGiving up."),
    ])

    with caplog.at_level(logging.WARNING, logger="orchestration_engine.dialogue_phase"):
        result = run_dialogue(
            phase_config=_basic_config(max_rounds=4),
            drafter_executor=drafter,
            reviewer_executor=reviewer,
            initial_input="Initial rough spec.",
            output_dir=tmp_path,
        )

    assert result.converged is False
    assert result.convergence_stall is True
    # The warning log should be present
    warned = any("convergence_stall" in rec.message for rec in caplog.records)
    assert warned, "expected a convergence_stall warning to be logged"

    # And the per-round drift_similarity field on round 2/3 should exceed threshold
    assert result.rounds[1].drift_similarity is not None
    assert result.rounds[1].drift_similarity > DRIFT_SIMILARITY_THRESHOLD
    assert result.rounds[2].drift_similarity is not None
    assert result.rounds[2].drift_similarity > DRIFT_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# 9. Per-round cost summed correctly (no 3× inflation)
# ---------------------------------------------------------------------------


def test_cost_summed_per_round(tmp_path):
    drafter = _ScriptedExecutor([
        _mk_result("Draft v1", cost=0.012, tokens=100),
        _mk_result("Draft v2", cost=0.018, tokens=150),
        _mk_result("Draft v3", cost=0.024, tokens=200),
    ])
    reviewer = _ScriptedExecutor([
        _mk_result("REQUEST_CHANGES\nNo.", cost=0.005, tokens=50),
        _mk_result("REQUEST_CHANGES\nNo.", cost=0.006, tokens=60),
        _mk_result("APPROVED", cost=0.007, tokens=70),
    ])

    result = run_dialogue(
        phase_config=_basic_config(max_rounds=5),
        drafter_executor=drafter,
        reviewer_executor=reviewer,
        initial_input="Initial rough spec.",
        output_dir=tmp_path,
    )

    expected_total = Decimal("0.012") + Decimal("0.005") + \
                     Decimal("0.018") + Decimal("0.006") + \
                     Decimal("0.024") + Decimal("0.007")

    assert result.converged is True
    assert len(result.rounds) == 3
    # Per-round cost equals drafter + reviewer
    assert result.rounds[0].cost == Decimal("0.012") + Decimal("0.005")
    assert result.rounds[1].cost == Decimal("0.018") + Decimal("0.006")
    assert result.rounds[2].cost == Decimal("0.024") + Decimal("0.007")
    # Total equals sum of per-round costs — no 3× inflation
    assert result.total_cost == expected_total
    # And the sum-by-round invariant holds
    assert result.total_cost == sum(
        (r.cost for r in result.rounds),
        Decimal("0"),
    )
    # Total tokens summed correctly too
    assert result.total_tokens == 100 + 150 + 200 + 50 + 60 + 70


# ---------------------------------------------------------------------------
# Bonus sanity check: Jaccard math
# ---------------------------------------------------------------------------


class TestJaccardSimilarity:
    """Sanity checks on the helper used for the drift indicator."""

    def test_identical(self):
        assert _jaccard_similarity("a b c", "a b c") == 1.0

    def test_disjoint(self):
        assert _jaccard_similarity("a b c", "d e f") == 0.0

    def test_partial(self):
        # tokens={a,b} vs {a,c} → intersection={a}, union={a,b,c} → 1/3
        sim = _jaccard_similarity("a b", "a c")
        assert 0.33 < sim < 0.34

    def test_empty_both(self):
        assert _jaccard_similarity("", "") == 1.0

    def test_empty_one(self):
        assert _jaccard_similarity("a b", "") == 0.0

    def test_case_insensitive(self):
        # Both must lowercase before set-comparing
        assert _jaccard_similarity("Hello World", "hello world") == 1.0


# ---------------------------------------------------------------------------
# Bonus: Pydantic config validation
# ---------------------------------------------------------------------------


class TestDialoguePhaseConfig:
    """Pydantic-level validation tests."""

    def test_valid_minimal_config(self):
        cfg = DialoguePhaseConfig(
            drafter=DialogueParticipant(executor="openrouter"),
            reviewer=DialogueParticipant(executor="gemini_cli"),
        )
        assert cfg.max_rounds == 4
        assert cfg.convergence_signal == "APPROVED"

    def test_rejects_empty_executor(self):
        with pytest.raises(Exception):
            DialogueParticipant(executor="")

    def test_rejects_zero_max_rounds(self):
        with pytest.raises(Exception):
            DialoguePhaseConfig(
                drafter=DialogueParticipant(executor="openrouter"),
                reviewer=DialogueParticipant(executor="gemini_cli"),
                max_rounds=0,
            )
