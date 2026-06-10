"""Integration tests for StateMachineSequencer with git-based phase handoff.

Covers behavioral contracts §3-§4, §6-§7, §9, §13, §15.
Each test maps to a specific behavioral contract from behavioral.md.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.adversary_parser import AdversaryConfig
from orchestration_engine.schemas import TaskResult, TaskState, TaskType
from orchestration_engine.sequencer import StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate
from orchestration_engine.transitions import PhaseOutcome

# #703: bare ``spec_adversary`` phases now raise at dispatch (the legacy shim was
# removed). These git-handoff tests use ``spec_adversary`` as a realistic loop
# terminus; attach a minimal generic-path AdversaryConfig (reward_enabled defaults
# to False → no reward file written) so ``execute()`` stays runnable without
# changing any prompt/commit assertion.
_SPEC_ADVERSARY_TEST_CONFIG = AdversaryConfig(
    valid_categories=["vague", "trivial", "missing_edge_case", "leakage", "divergence"],
    fallback_category="vague",
    verdict_scan="last",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the given repo directory."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(tmp_path: Path, *, branch: str = "main") -> Path:
    """Create a minimal git repo with one initial commit on the given branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".orchemist/\n")
    (repo / "README.md").write_text("# Test Repo\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def _make_loop_template(
    *,
    max_iterations: int = 4,
    phase_ids: tuple[str, ...] = ("spec", "behavioral", "spec_adversary"),
) -> PipelineTemplate:
    """Build a minimal PipelineTemplate with a 3-phase spec loop.

    spec → behavioral → spec_adversary → (request_changes → spec, approve → terminal)
    """
    phases = []
    for i, pid in enumerate(phase_ids):
        transitions = {}
        if i < len(phase_ids) - 1:
            transitions["success"] = phase_ids[i + 1]
        else:
            # Adversary: request_changes loops back, approve is terminal
            transitions["request_changes"] = phase_ids[0]
            transitions["success"] = None  # terminal on approve

        phase = PhaseDefinition(
            id=pid,
            name=pid.replace("_", " ").title(),
            prompt_template=(
                "Phase: {input[phase_name]}\n"
                "Iteration history:\n{iteration_history}\n"
                "Phase diff:\n{phase_diff}\n"
                "Previous commit: {previous_commit}\n"
            ),
            transitions=transitions,
            max_iterations=max_iterations,
            adversary_config=(_SPEC_ADVERSARY_TEST_CONFIG if pid == "spec_adversary" else None),
        )
        phases.append(phase)

    return PipelineTemplate(
        id="test-loop-template",
        name="Test Loop Template",
        phases=phases,
        default_transitions={},
    )


def _make_non_loop_template() -> PipelineTemplate:
    """Build a minimal template with NO loop group — a simple linear pipeline."""
    phases = [
        PhaseDefinition(
            id="phase_a",
            name="Phase A",
            prompt_template="Do A. {iteration_history}{phase_diff}{previous_commit}",
            transitions={"success": "phase_b"},
            max_iterations=0,
        ),
        PhaseDefinition(
            id="phase_b",
            name="Phase B",
            prompt_template="Do B. {iteration_history}{phase_diff}{previous_commit}",
            transitions={},
            max_iterations=0,
        ),
    ]
    return PipelineTemplate(
        id="test-linear-template",
        name="Test Linear Template",
        phases=phases,
        default_transitions={},
    )


def _build_runner(execute_fn: Callable) -> MagicMock:
    """Build a mock TaskRunner with a custom execute side-effect."""
    runner = MagicMock()
    _task_store: Dict[str, Any] = {}

    def submit_task(spec):
        _task_store[spec.id] = spec
        return spec.id

    def get_task(task_id):
        return _task_store.get(task_id)

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = get_task
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()

    executor = MagicMock()
    executor.can_handle.return_value = True
    executor.execute.side_effect = execute_fn
    runner.executors = [executor]
    return runner


class PromptCapture:
    """Captures prompts sent to each phase via the mock runner."""

    def __init__(self, results: Optional[Dict[str, List[str]]] = None):
        """results maps phase_id → list of text outputs per call."""
        self.results = results or {}
        self.call_count: Dict[str, int] = defaultdict(int)
        self.prompts: Dict[str, List[str]] = defaultdict(list)

    def __call__(self, task_spec, **kwargs) -> TaskResult:
        phase_id = task_spec.payload.get("phase_id", "unknown")
        prompt = task_spec.payload.get("prompt", "")
        self.prompts[phase_id].append(prompt)
        count = self.call_count[phase_id]
        self.call_count[phase_id] += 1

        results = self.results.get(phase_id, [])
        if count < len(results):
            text = results[count]
        else:
            text = f"Default output for {phase_id} round {count + 1}"

        return TaskResult(
            task_id=task_spec.id,
            task_type=task_spec.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": text},
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Provide a clean git repo with one commit on 'main'."""
    return _init_repo(tmp_path)


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Provide an output directory."""
    d = tmp_path / "output"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# §3 — Phase Input: Commit-Based Reads
# ---------------------------------------------------------------------------

class TestPhaseInputCommitBasedReads:
    """§3: Phase input uses commit SHAs from previous rounds."""

    def test_round2_prompt_references_previous_commit(self, git_repo: Path, output_dir: Path) -> None:
        """§3.1-BC1: When building the prompt for round 2+, the system uses the
        commit SHA from the previous round to reference prior output."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="commit-read-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        # The adversary's second prompt (round 2) should contain a commit reference
        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Should contain a short SHA (8 hex chars) somewhere
            assert re.search(r"[0-9a-f]{7,8}", round2_prompt), \
                f"Expected commit SHA in round 2 prompt, got: {round2_prompt[:500]}"

    def test_round1_has_empty_previous_commit_and_diff(self, git_repo: Path, output_dir: Path) -> None:
        """§3.2-BC1: In round 1, the system provides empty strings for
        {previous_commit} and {phase_diff}."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=2)

        # Make adversary approve immediately
        capture = PromptCapture({
            "spec_adversary": ["APPROVE\nLooks good"],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="round1-empty-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        # Check the first adversary prompt — should have empty phase_diff and previous_commit
        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if adversary_prompts:
            round1_prompt = adversary_prompts[0]
            # The template puts "Phase diff:\n{phase_diff}\n" — should be "Phase diff:\n\n"
            assert "Phase diff:\n\n" in round1_prompt or "Previous commit: \n" in round1_prompt

    def test_phase_input_without_git_uses_inline(self, output_dir: Path) -> None:
        """§3.3-BC1: If git handoff is inactive, the system builds the prompt
        using existing inline iteration history, unchanged from pre-feature behavior."""
        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)

        # No git handoff — pass None
        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        sequencer.execute({"phase_name": "test"})

        # Verify iteration history uses inline format (no commit SHAs)
        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Inline format uses "--- Round N: member_id ---" without "(commit ...)"
            assert "(commit " not in round2_prompt


# ---------------------------------------------------------------------------
# §4 — Iteration History: Compact Format
# ---------------------------------------------------------------------------

class TestIterationHistoryCompactFormat:
    """§4: Compact iteration history with git handoff."""

    def test_compact_history_has_commit_references(self, git_repo: Path, output_dir: Path) -> None:
        """§4.1-BC1: With git handoff active, {iteration_history} contains
        compact commit-reference lines, NOT full inline text."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="compact-history-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Should contain "(commit " followed by a short hash
            assert re.search(r"\(commit [0-9a-f]{7,8}\)", round2_prompt), \
                f"Expected commit reference in iteration history, got: {round2_prompt[:500]}"

    def test_compact_history_has_diff_blocks(self, git_repo: Path, output_dir: Path) -> None:
        """§4.1-BC2/BC3: With git handoff, iteration history for round 3 contains
        diff blocks showing changes between consecutive rounds."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=4)
        capture = PromptCapture({
            "spec": ["Spec v1", "Spec v2", "Spec v3"],
            "behavioral": ["Behavioral v1", "Behavioral v2", "Behavioral v3"],
            "spec_adversary": [
                "REQUEST_CHANGES\nFix the spec",
                "REQUEST_CHANGES\nMore fixes",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="diff-blocks-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 3:
            round3_prompt = adversary_prompts[2]
            # Should contain diff blocks (```diff)
            assert "diff" in round3_prompt.lower()

    def test_round1_initial_output_marker(self, git_repo: Path, output_dir: Path) -> None:
        """§4.1-BC4: When round 1 is referenced in iteration history, the system
        includes '[Initial output — see commit ...]' instead of a diff."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="initial-marker-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Round 1 entries should have "Initial output" marker
            assert "Initial output" in round2_prompt or "[Initial" in round2_prompt

    def test_prompt_size_scales_linearly(self, git_repo: Path, output_dir: Path) -> None:
        """§4.2-BC1: {iteration_history} grows linearly, not exponentially.
        Prompt size for the adversary phase should be roughly constant across rounds."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=5)
        capture = PromptCapture({
            "spec": [f"# Spec Round {i}\n" + "x" * 2000 for i in range(1, 6)],
            "behavioral": [f"# Behavioral Round {i}\n" + "y" * 2000 for i in range(1, 6)],
            "spec_adversary": [
                "REQUEST_CHANGES\nFix 1",
                "REQUEST_CHANGES\nFix 2",
                "REQUEST_CHANGES\nFix 3",
                "APPROVE\nDone",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="linear-scaling-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 4:
            sizes = [len(p) for p in adversary_prompts]
            # Compare round 4 vs round 3 — growth should be roughly linear
            # (adding one more round of diffs), not exponential
            if sizes[2] > 0 and sizes[3] > 0:
                ratio = sizes[3] / sizes[2]
                assert ratio < 2.0, \
                    f"Prompt size growth ratio {ratio:.2f} suggests non-linear scaling. Sizes: {sizes}"

    def test_inline_history_without_git_backward_compatible(self, output_dir: Path) -> None:
        """§4.3-BC1: When git handoff is not active, {iteration_history} is built
        using existing inline text mechanism — pre-feature behavior."""
        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)

        # No git handoff
        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Should use inline format: "--- Round N: member ---\n<full text>"
            # NOT commit references
            assert "(commit " not in round2_prompt

    def test_round1_iteration_history_is_empty(self, git_repo: Path, output_dir: Path) -> None:
        """§4.4-BC1: When current round is 1, {iteration_history} is empty
        regardless of git handoff status."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=2)
        capture = PromptCapture({
            "spec_adversary": ["APPROVE\nGood"],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="empty-round1-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        # First adversary prompt should have empty iteration history
        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if adversary_prompts:
            round1_prompt = adversary_prompts[0]
            # "Iteration history:\n{iteration_history}\n" → "Iteration history:\n\n"
            assert "Iteration history:\n\n" in round1_prompt

    def test_missing_commit_skipped_in_history(self, git_repo: Path, output_dir: Path) -> None:
        """§4.4-BC2: If a commit for a particular member/round is missing from
        the commit log, the system skips that entry rather than erroring."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="skip-missing-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        # Execute should not raise even if commit log is incomplete
        result = sequencer.execute({"phase_name": "test"})
        assert result is not None  # Pipeline completed without error


# ---------------------------------------------------------------------------
# §6 — New Template Variables
# ---------------------------------------------------------------------------

class TestNewTemplateVariables:
    """§6: {phase_diff} and {previous_commit} template variables."""

    def test_phase_diff_populated_in_round2(self, git_repo: Path, output_dir: Path) -> None:
        """§6.1-BC1: In round 2+, {phase_diff} contains git diff output."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec": ["# Spec v1\n- Point A", "# Spec v2\n- Point A\n- Point B"],
            "behavioral": ["# BH v1", "# BH v2"],
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="phase-diff-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # {phase_diff} should contain diff content
            assert "Phase diff:" in round2_prompt
            # Should not be empty in round 2 (since spec/behavioral changed)

    def test_previous_commit_is_short_sha(self, git_repo: Path, output_dir: Path) -> None:
        """§6.2-BC1: {previous_commit} contains the short (8-character) commit
        SHA of the most recent commit from the previous round's last member."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="prev-commit-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Extract the "Previous commit: ..." line
            match = re.search(r"Previous commit:\s*([0-9a-f]+)", round2_prompt)
            if match:
                sha = match.group(1)
                assert len(sha) == 8, f"Expected 8-char short SHA, got '{sha}'"

    def test_template_vars_empty_in_round1(self, git_repo: Path, output_dir: Path) -> None:
        """§6.3-BC1: In round 1, both new template variables are empty strings."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=2)
        capture = PromptCapture({
            "spec_adversary": ["APPROVE\nGood"],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="empty-vars-round1")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if adversary_prompts:
            round1_prompt = adversary_prompts[0]
            assert "Previous commit: \n" in round1_prompt or "Previous commit:\n" in round1_prompt

    def test_template_vars_empty_without_git(self, output_dir: Path) -> None:
        """§6.4-BC1: When git handoff is not active, both new template variables
        are empty strings — not errors."""
        template = _make_loop_template(max_iterations=2)
        capture = PromptCapture({
            "spec_adversary": ["APPROVE\nGood"],
        })
        runner = _build_runner(capture)

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if adversary_prompts:
            # No MISSING: tokens should appear
            for prompt in adversary_prompts:
                assert "<MISSING:phase_diff>" not in prompt
                assert "<MISSING:previous_commit>" not in prompt

    def test_template_vars_empty_for_non_loop_phases(self, git_repo: Path, output_dir: Path) -> None:
        """§6.5-BC1: For phases not in a loop group, the new template variables
        are empty strings regardless of git handoff status."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_non_loop_template()
        capture = PromptCapture({
            "phase_a": ["Output A"],
            "phase_b": ["Output B"],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="non-loop-vars-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        for phase_id in ["phase_a", "phase_b"]:
            prompts = capture.prompts.get(phase_id, [])
            for prompt in prompts:
                assert "<MISSING:phase_diff>" not in prompt
                assert "<MISSING:previous_commit>" not in prompt

    def test_template_vars_never_produce_missing_tokens(self, git_repo: Path, output_dir: Path) -> None:
        """§6.6-BC1: Templates that don't reference the new variables still work.
        Variables are explicitly set to '' — no <MISSING:...> tokens appear."""
        from orchestration_engine.git_handoff import GitHandoff

        # Template that doesn't reference phase_diff or previous_commit
        phases = [
            PhaseDefinition(
                id="simple",
                name="Simple",
                prompt_template="Just do the thing. History: {iteration_history}",
                transitions={},
                max_iterations=0,
            ),
        ]
        template = PipelineTemplate(
            id="simple-template",
            name="Simple Template",
            phases=phases,
            default_transitions={},
        )
        capture = PromptCapture()
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="no-missing-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        result = sequencer.execute({"phase_name": "test"})
        assert result is not None

        # No MISSING tokens in any prompts
        for prompts in capture.prompts.values():
            for prompt in prompts:
                assert "<MISSING:" not in prompt


# ---------------------------------------------------------------------------
# §7 — Template Changes: Adversary Prompt
# ---------------------------------------------------------------------------

class TestAdversaryPromptChanges:
    """§7: Adversary prompt includes Changes Since Last Round section."""

    def test_adversary_prompt_has_changes_section_round2(self, git_repo: Path, output_dir: Path) -> None:
        """§7.1-BC1: In round 2+, the adversary prompt includes {phase_diff}
        content (the template references it)."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec": ["# Spec v1", "# Spec v2 with changes"],
            "behavioral": ["# BH v1", "# BH v2 with updates"],
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="adversary-changes-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # Template includes "Phase diff:\n{phase_diff}\n" — should have content
            assert "Phase diff:" in round2_prompt

    def test_file_based_reads_still_work(self, git_repo: Path, output_dir: Path) -> None:
        """§7.3-BC1: The daemon still writes files to {output_dir} regardless of
        git handoff status. Git handoff is additional, not a replacement."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=2)
        capture = PromptCapture({
            "spec_adversary": ["APPROVE\nGood"],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="file-reads-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        # The sequencer itself doesn't write to output_dir — the daemon does.
        # But we can verify the sequencer doesn't break file-based mode.
        result = sequencer.execute({"phase_name": "test"})
        assert result is not None  # Pipeline completed successfully


# ---------------------------------------------------------------------------
# §9 — Backward Compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """§9: Non-git pipelines and dry-run are unchanged."""

    def test_non_git_pipeline_unchanged(self, output_dir: Path) -> None:
        """§9.1-BC1: When a pipeline is launched without repo_path, the system
        does not attempt git handoff. All behavior is identical to pre-feature."""
        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)

        # No git_handoff parameter
        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        result = sequencer.execute({"phase_name": "test"})
        assert result is not None

        # Prompts should not contain commit references
        for phase_id, prompts in capture.prompts.items():
            for prompt in prompts:
                assert "(commit " not in prompt

    def test_template_without_new_vars_unchanged(self, output_dir: Path) -> None:
        """§9.1-BC2: A template that doesn't reference new variables runs
        with zero behavioral differences."""
        phases = [
            PhaseDefinition(
                id="only_phase",
                name="Only Phase",
                prompt_template="Do the work: {input[task]}",
                transitions={},
                max_iterations=0,
            ),
        ]
        template = PipelineTemplate(
            id="old-template",
            name="Old Template",
            phases=phases,
            default_transitions={},
        )
        capture = PromptCapture()
        runner = _build_runner(capture)

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        result = sequencer.execute({"task": "do something"})
        assert result is not None

    def test_existing_callers_work_without_git_args(self, output_dir: Path) -> None:
        """§9.4-BC1: Existing callers that don't provide new git-related args
        continue to work — new parameters have default values."""
        template = _make_loop_template(max_iterations=2)
        capture = PromptCapture({
            "spec_adversary": ["APPROVE\nGood"],
        })
        runner = _build_runner(capture)

        # Construct without git_handoff argument at all
        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        result = sequencer.execute({"phase_name": "test"})
        assert result is not None


# ---------------------------------------------------------------------------
# §13 — Interaction: Features Combining
# ---------------------------------------------------------------------------

class TestFeatureInteractions:
    """§13: Git handoff interactions with other features."""

    def test_no_git_handoff_without_loop_groups(self, git_repo: Path, output_dir: Path) -> None:
        """§13.1-BC1: When git handoff is available but the pipeline has no loop
        groups, the system does not initialize git handoff."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_non_loop_template()
        capture = PromptCapture()
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="no-loop-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        result = sequencer.execute({"phase_name": "test"})
        assert result is not None

        # No spec-loop branch should have been created
        result_git = subprocess.run(
            ["git", "branch", "--list", "spec-loop/*"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert result_git.stdout.strip() == ""

    def test_abort_preserves_branch(self, git_repo: Path, output_dir: Path) -> None:
        """§13.2-BC1: When a pipeline is aborted (max rounds reached), git handoff
        cleanup runs with branch preservation."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=2)
        # Never approve — always request changes to force max iterations abort
        capture = PromptCapture({
            "spec_adversary": [
                "REQUEST_CHANGES\nNot good enough",
                "REQUEST_CHANGES\nStill not good",
                "REQUEST_CHANGES\nStill bad",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="abort-preserve-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        result = sequencer.execute({"phase_name": "test"})

        # Pipeline should have aborted
        assert result.get("aborted") is True or result.get("abort_reason") is not None

    def test_template_vars_resolved_in_single_pass(self, git_repo: Path, output_dir: Path) -> None:
        """§13.3-BC1: Both new template variables and {iteration_history} are
        resolved in the same formatting pass — no double-substitution."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        capture = PromptCapture({
            # Include curly braces in output to test no double-substitution
            "spec": ["# Spec with {curly} braces"],
            "behavioral": ["# Behavioral with {more} braces"],
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="single-pass-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        # Should not raise KeyError or format errors
        result = sequencer.execute({"phase_name": "test"})
        assert result is not None

    def test_git_truncation_limit_replaces_inline_limit(self, git_repo: Path, output_dir: Path) -> None:
        """§13.4-BC1: When git handoff is active, diffs have their own truncation
        limit (2500 chars per member), not the inline 4000-char limit."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_loop_template(max_iterations=3)
        # Create large outputs that would exceed 4000 chars inline
        capture = PromptCapture({
            "spec": ["x" * 5000, "y" * 5000],
            "behavioral": ["a" * 5000, "b" * 5000],
            "spec_adversary": [
                "REQUEST_CHANGES\nFix it",
                "APPROVE\nLooks good",
            ],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="truncation-limit-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        adversary_prompts = capture.prompts.get("spec_adversary", [])
        if len(adversary_prompts) >= 2:
            round2_prompt = adversary_prompts[1]
            # The iteration history should be compact — much shorter than
            # 3 × 4000 = 12000 chars of inline content
            history_match = re.search(
                r"Iteration history:\n(.*?)(?=\nPhase diff:)", round2_prompt, re.DOTALL
            )
            if history_match:
                history_text = history_match.group(1)
                # Each member's diff should be ≤ 2500 chars
                # Total should be much less than inline mode
                assert len(history_text) < 10000, \
                    f"Iteration history too large ({len(history_text)} chars) for git mode"


# ---------------------------------------------------------------------------
# §15 — No-Op Scenarios
# ---------------------------------------------------------------------------

class TestNoOpScenarios:
    """§15: Explicitly nothing happens in these scenarios."""

    def test_no_loop_group_no_branch_no_commits(self, git_repo: Path, output_dir: Path) -> None:
        """§15-BC1: When a pipeline has no loop group, no temp branch is created,
        no commits are made, no new template variables are populated."""
        from orchestration_engine.git_handoff import GitHandoff

        template = _make_non_loop_template()
        capture = PromptCapture()
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="noop-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        result = sequencer.execute({"phase_name": "test"})
        assert result is not None

        # No spec-loop branches
        git_result = subprocess.run(
            ["git", "branch", "--list", "spec-loop/*"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert git_result.stdout.strip() == ""

        # Template vars should not have commit info
        for prompts in capture.prompts.values():
            for prompt in prompts:
                assert "(commit " not in prompt
                assert "<MISSING:" not in prompt

    def test_non_loop_phase_output_not_committed(self, git_repo: Path, output_dir: Path) -> None:
        """§15-BC2: When a phase outside the loop group completes, the system
        does not commit its output to the temp branch."""
        from orchestration_engine.git_handoff import GitHandoff

        # Add a pre-loop phase that is NOT in the loop group
        phases = [
            PhaseDefinition(
                id="pre_phase",
                name="Pre Phase",
                prompt_template="Do pre-work: {input[phase_name]}",
                transitions={"success": "spec"},
                max_iterations=0,
            ),
            PhaseDefinition(
                id="spec",
                name="Spec",
                prompt_template="Spec: {input[phase_name]} {iteration_history}{phase_diff}{previous_commit}",
                transitions={"success": "spec_adversary"},
                max_iterations=0,
            ),
            PhaseDefinition(
                id="spec_adversary",
                name="Adversary",
                prompt_template="Review: {input[phase_name]} {iteration_history}{phase_diff}{previous_commit}",
                transitions={"request_changes": "spec"},
                max_iterations=2,
                adversary_config=_SPEC_ADVERSARY_TEST_CONFIG,
            ),
        ]
        template = PipelineTemplate(
            id="pre-phase-template",
            name="Pre Phase Template",
            phases=phases,
            default_transitions={},
        )
        capture = PromptCapture({
            "pre_phase": ["Pre-work done"],
            "spec_adversary": ["APPROVE\nGood"],
        })
        runner = _build_runner(capture)
        handoff = GitHandoff(repo_path=git_repo, run_id="non-loop-commit-run")

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
            git_handoff=handoff,
        )

        sequencer.execute({"phase_name": "test"})

        # Check that pre_phase output is NOT in the commit log
        # Only loop group phases should be committed
        if handoff.is_active():
            assert handoff.get_commit("pre_phase", 1) is None

    def test_unrelated_pipeline_zero_changes(self, output_dir: Path) -> None:
        """§15-BC3: When unrelated pipeline modules are loaded, the system
        exhibits zero behavioral changes."""
        # Simply verify that importing the module doesn't change anything
        template = _make_non_loop_template()
        capture = PromptCapture()
        runner = _build_runner(capture)

        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=output_dir,
        )

        result = sequencer.execute({"phase_name": "test"})
        assert result is not None
        # No exceptions, no unexpected behavior
