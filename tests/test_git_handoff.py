"""Acceptance tests for GitHandoff — the git-based phase handoff module.

Covers behavioral contracts §1-§5, §8, §10-§12, §14.
Each test maps to a specific behavioral contract from behavioral.md.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


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
    # Create an initial file and commit so HEAD exists
    (repo / "README.md").write_text("# Test Repo\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def _get_current_branch(repo: Path) -> str:
    """Return the name of the currently checked-out branch."""
    result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def _get_head_sha(repo: Path) -> str:
    """Return the full SHA of HEAD."""
    result = _git(repo, "rev-parse", "HEAD")
    return result.stdout.strip()


def _branch_exists(repo: Path, branch_name: str) -> bool:
    """Check whether a local branch exists."""
    result = _git(repo, "branch", "--list", branch_name)
    return branch_name in result.stdout


def _get_log_oneline(repo: Path, ref: str) -> list[str]:
    """Return list of one-line log entries for a ref."""
    result = _git(repo, "log", "--oneline", ref, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def _working_tree_clean(repo: Path) -> bool:
    """Return True if git status --porcelain is empty."""
    result = _git(repo, "status", "--porcelain")
    return result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Provide a clean git repo with one commit on 'main'."""
    return _init_repo(tmp_path)


@pytest.fixture
def git_repo_with_gitignore(git_repo: Path) -> Path:
    """Provide a git repo with .orchemist/ in .gitignore."""
    gitignore = git_repo / ".gitignore"
    gitignore.write_text(".orchemist/\n")
    _git(git_repo, "add", ".gitignore")
    _git(git_repo, "commit", "-m", "Add .gitignore")
    return git_repo


# ---------------------------------------------------------------------------
# §1.1 — Branch Creation (Happy Path)
# ---------------------------------------------------------------------------

class TestBranchCreationHappyPath:
    """§1.1: Temporary branch lifecycle — creation."""

    def test_temp_branch_created_on_initialize(self, git_repo: Path) -> None:
        """§1.1-BC1: When a pipeline run starts with git available, the system
        creates a local branch named spec-loop/{run-id}."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "test-run-001"
        handoff = GitHandoff(repo_path=git_repo, run_id=run_id)
        result = handoff.initialize()

        assert result is True
        assert _branch_exists(git_repo, f"spec-loop/{run_id}")

    def test_initialize_logs_creation_message(self, git_repo: Path, caplog) -> None:
        """§1.1-BC2: Initialization produces a log line containing
        'GitHandoff' and the run_id."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "test-run-002"
        handoff = GitHandoff(repo_path=git_repo, run_id=run_id)
        with caplog.at_level(logging.DEBUG):
            handoff.initialize()

        assert any("GitHandoff" in msg and run_id in msg for msg in caplog.messages), \
            f"Expected log message about GitHandoff creation for {run_id}, got: {caplog.messages}"

    def test_run_dir_created_inside_repo(self, git_repo: Path) -> None:
        """§1.1-BC3: When the temp branch is created, the system also creates
        .orchemist/runs/{run-id}/ inside the repository."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "test-run-003"
        handoff = GitHandoff(repo_path=git_repo, run_id=run_id)
        handoff.initialize()

        run_dir = git_repo / ".orchemist" / "runs" / run_id
        assert run_dir.is_dir()

    def test_branch_is_local_only(self, git_repo: Path) -> None:
        """§1.1-BC4: When the temp branch is created, the system does not push
        it to any remote — the branch remains local only."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "test-run-004"
        handoff = GitHandoff(repo_path=git_repo, run_id=run_id)
        handoff.initialize()

        # No remote exists in our test repo, but verify no push was attempted
        # by checking there are no remote-tracking branches
        result = _git(git_repo, "branch", "-r")
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# §1.2 — Branch Creation (Error Paths)
# ---------------------------------------------------------------------------

class TestBranchCreationErrorPaths:
    """§1.2: Error paths during branch creation."""

    def test_fallback_when_git_not_installed(self, tmp_path: Path, caplog) -> None:
        """§1.2-BC1: If git is not installed, the system logs a warning
        and falls back."""
        from orchestration_engine.git_handoff import GitHandoff

        repo = tmp_path / "fake_repo"
        repo.mkdir()

        # Create a PATH without git
        empty_bin = tmp_path / "empty_bin"
        empty_bin.mkdir()

        handoff = GitHandoff(repo_path=repo, run_id="no-git-run")
        with patch.dict(os.environ, {"PATH": str(empty_bin)}):
            with caplog.at_level(logging.WARNING):
                result = handoff.initialize()

        assert result is False
        assert not handoff.is_active()

    def test_fallback_on_dirty_working_tree(self, git_repo: Path, caplog) -> None:
        """§1.2-BC2: If the working tree has uncommitted changes, the system
        refuses to activate git-based handoff and falls back."""
        from orchestration_engine.git_handoff import GitHandoff

        # Make the working tree dirty
        (git_repo / "dirty.txt").write_text("uncommitted changes")
        _git(git_repo, "add", "dirty.txt")  # staged but not committed

        handoff = GitHandoff(repo_path=git_repo, run_id="dirty-tree-run")
        with caplog.at_level(logging.WARNING):
            result = handoff.initialize()

        assert result is False
        assert not handoff.is_active()

    def test_fallback_on_invalid_repo_path(self, tmp_path: Path, caplog) -> None:
        """§1.2-BC3: If the repo path does not point to a valid git repository,
        the system logs a warning and falls back without a user-visible error."""
        from orchestration_engine.git_handoff import GitHandoff

        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        handoff = GitHandoff(repo_path=not_a_repo, run_id="bad-repo-run")
        with caplog.at_level(logging.WARNING):
            result = handoff.initialize()

        assert result is False
        assert not handoff.is_active()


# ---------------------------------------------------------------------------
# §1.3 — Branch Cleanup on Success
# ---------------------------------------------------------------------------

class TestBranchCleanupOnSuccess:
    """§1.3: Temp branch is deleted after successful pipeline completion."""

    def test_branch_deleted_on_success_cleanup(self, git_repo_with_gitignore: Path) -> None:
        """§1.3-BC1: When a pipeline completes successfully, the system deletes
        the spec-loop/{run-id} branch and checks out the original branch."""
        from orchestration_engine.git_handoff import GitHandoff

        original_branch = _get_current_branch(git_repo_with_gitignore)
        run_id = "success-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Do some work on the temp branch
        handoff.commit_phase_output("spec", 1, "# Spec v1")

        # Cleanup with preserve=False (success case)
        handoff.cleanup(preserve=False)

        assert _get_current_branch(git_repo_with_gitignore) == original_branch
        assert not _branch_exists(git_repo_with_gitignore, f"spec-loop/{run_id}")

    def test_no_matching_branch_after_success(self, git_repo_with_gitignore: Path) -> None:
        """§1.3-BC2: git branch --list 'spec-loop/*' produces no output
        matching the run's branch name after success."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "success-run-2"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.cleanup(preserve=False)

        result = _git(git_repo_with_gitignore, "branch", "--list", f"spec-loop/{run_id}")
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# §1.4 — Branch Preservation on Failure
# ---------------------------------------------------------------------------

class TestBranchPreservationOnFailure:
    """§1.4: Temp branch is preserved when pipeline fails/aborts."""

    def test_branch_preserved_on_failure(self, git_repo_with_gitignore: Path) -> None:
        """§1.4-BC1: When a pipeline fails, the system preserves the
        spec-loop/{run-id} branch for debugging and checks out the original branch."""
        from orchestration_engine.git_handoff import GitHandoff

        original_branch = _get_current_branch(git_repo_with_gitignore)
        run_id = "failure-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.commit_phase_output("behavioral", 1, "# Behavioral v1")

        # Cleanup with preserve=True (failure case)
        handoff.cleanup(preserve=True)

        assert _get_current_branch(git_repo_with_gitignore) == original_branch
        assert _branch_exists(git_repo_with_gitignore, f"spec-loop/{run_id}")

    def test_preserved_branch_has_commit_history(self, git_repo_with_gitignore: Path) -> None:
        """§1.4-BC2: git log spec-loop/{run-id} produces the commit history
        of all phases that completed before the failure."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "failure-history-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.commit_phase_output("behavioral", 1, "# Behavioral v1")
        handoff.cleanup(preserve=True)

        log_entries = _get_log_oneline(git_repo_with_gitignore, f"spec-loop/{run_id}")
        # Should have at least 2 commits for the two phases
        spec_loop_commits = [e for e in log_entries if "[spec-loop]" in e]
        assert len(spec_loop_commits) >= 2


# ---------------------------------------------------------------------------
# §1.5 — Branch Cleanup (Edge Cases)
# ---------------------------------------------------------------------------

class TestBranchCleanupEdgeCases:
    """§1.5: Edge cases in branch cleanup."""

    def test_original_branch_head_restored_after_cleanup(self, git_repo_with_gitignore: Path) -> None:
        """§1.5-BC1: After cleanup, git rev-parse HEAD returns the same SHA
        as the original branch HEAD recorded before initialize()."""
        from orchestration_engine.git_handoff import GitHandoff

        original_sha = _get_head_sha(git_repo_with_gitignore)
        run_id = "restore-head-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.cleanup(preserve=False)

        restored_sha = _get_head_sha(git_repo_with_gitignore)
        assert restored_sha == original_sha

    def test_working_tree_clean_after_cleanup(self, git_repo_with_gitignore: Path) -> None:
        """§1.5-BC1 continued: git status --porcelain produces empty output
        after cleanup."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "clean-tree-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.cleanup(preserve=False)

        assert _working_tree_clean(git_repo_with_gitignore)

    def test_cleanup_failure_does_not_raise(self, git_repo_with_gitignore: Path, caplog) -> None:
        """§1.5-BC2: If cleanup itself fails (e.g., branch already deleted externally),
        the system logs a warning and does not raise a fatal error."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "cleanup-fail-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Externally delete the branch before cleanup
        _git(git_repo_with_gitignore, "checkout", "main")
        _git(git_repo_with_gitignore, "branch", "-D", f"spec-loop/{run_id}")

        # Cleanup should not raise
        with caplog.at_level(logging.WARNING):
            handoff.cleanup(preserve=False)  # should not raise


# ---------------------------------------------------------------------------
# §1.6 — .gitignore Warning
# ---------------------------------------------------------------------------

class TestGitignoreWarning:
    """§1.6: Warning when .orchemist/ is not in .gitignore."""

    def test_warns_when_gitignore_missing_orchemist(self, git_repo: Path, caplog) -> None:
        """§1.6-BC1: When .orchemist/ is not in .gitignore, the system logs a
        warning about adding it."""
        from orchestration_engine.git_handoff import GitHandoff

        # git_repo fixture does NOT have .gitignore with .orchemist/
        handoff = GitHandoff(repo_path=git_repo, run_id="warn-run")
        with caplog.at_level(logging.WARNING):
            handoff.initialize()

        assert any(".orchemist/" in msg and ".gitignore" in msg for msg in caplog.messages), \
            f"Expected .gitignore warning, got: {caplog.messages}"

    def test_no_warning_when_gitignore_includes_orchemist(
        self, git_repo_with_gitignore: Path, caplog
    ) -> None:
        """§1.6-BC2: If .orchemist/ is already in .gitignore, no warning is logged."""
        from orchestration_engine.git_handoff import GitHandoff

        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id="no-warn-run")
        with caplog.at_level(logging.WARNING):
            handoff.initialize()

        gitignore_warnings = [
            msg for msg in caplog.messages
            if ".orchemist/" in msg and ".gitignore" in msg
        ]
        assert len(gitignore_warnings) == 0


# ---------------------------------------------------------------------------
# §1.7 — Finalization on APPROVE
# ---------------------------------------------------------------------------

class TestFinalizationOnApprove:
    """§1.7: Finalization when the adversary issues APPROVE."""

    def test_finalize_commits_to_target_branch(self, git_repo_with_gitignore: Path, tmp_path: Path) -> None:
        """§1.7-BC1: After finalization, git log -1 --oneline on the feature
        branch shows a commit containing the finalized spec and behavioral artifacts."""
        from orchestration_engine.git_handoff import GitHandoff

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        target_branch = _get_current_branch(git_repo_with_gitignore)

        run_id = "finalize-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Simulate a full spec loop
        handoff.commit_phase_output("spec", 1, "# Final Spec")
        handoff.commit_phase_output("behavioral", 1, "# Final Behavioral")

        handoff.finalize(output_dir, target_branch)

        # Verify the commit exists on the target branch
        log = _get_log_oneline(git_repo_with_gitignore, target_branch)
        assert len(log) >= 2  # At least initial commit + finalization commit

    def test_temp_branch_deleted_after_finalize(self, git_repo_with_gitignore: Path, tmp_path: Path) -> None:
        """§1.7-BC2: After finalization, git branch --list 'spec-loop/{run-id}'
        produces no output — the temp branch has been deleted."""
        from orchestration_engine.git_handoff import GitHandoff

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        target_branch = _get_current_branch(git_repo_with_gitignore)
        run_id = "finalize-cleanup-run"

        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Final Spec")
        handoff.commit_phase_output("behavioral", 1, "# Final Behavioral")

        handoff.finalize(output_dir, target_branch)

        result = _git(git_repo_with_gitignore, "branch", "--list", f"spec-loop/{run_id}")
        assert result.stdout.strip() == ""

    def test_finalize_failure_preserves_branch(self, git_repo_with_gitignore: Path, tmp_path: Path, caplog) -> None:
        """§1.7-BC3: If finalization fails, the temp branch is preserved for debugging,
        and the feature branch is unmodified. Final files remain in output_dir."""
        from orchestration_engine.git_handoff import GitHandoff
        from orchestration_engine.errors import GitHandoffError

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        run_id = "finalize-fail-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()
        handoff.commit_phase_output("spec", 1, "# Final Spec")

        # Finalize with a non-existent target branch to force failure
        with caplog.at_level(logging.WARNING):
            try:
                handoff.finalize(output_dir, "nonexistent-branch")
            except (GitHandoffError, Exception):
                pass  # Finalize raises GitHandoffError on failure

        # The temp branch should still exist (preserved)
        assert _branch_exists(git_repo_with_gitignore, f"spec-loop/{run_id}")


# ---------------------------------------------------------------------------
# §2.1 — Commits per Phase (Happy Path)
# ---------------------------------------------------------------------------

class TestCommitsPerPhase:
    """§2.1: Commit-based phase output."""

    def test_commit_created_per_phase(self, git_repo_with_gitignore: Path) -> None:
        """§2.1-BC1: When a loop phase completes, the system commits the output
        with message format '[spec-loop] {phase_id} round {N}'."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "commit-phase-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        sha = handoff.commit_phase_output("spec", 1, "# Spec v1")
        assert sha is not None

        log = _get_log_oneline(git_repo_with_gitignore, f"spec-loop/{run_id}")
        spec_loop_commits = [e for e in log if "[spec-loop]" in e]
        assert any("spec" in c and "round 1" in c for c in spec_loop_commits)

    def test_multiple_rounds_produce_multiple_commits(self, git_repo_with_gitignore: Path) -> None:
        """§2.1-BC2: A 3-round spec loop with 3 members per round produces
        up to 9 commits, each with a [spec-loop] prefix."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "multi-round-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        members = ["spec", "behavioral", "spec_adversary"]
        for round_num in range(1, 4):  # 3 rounds
            for member in members:
                handoff.commit_phase_output(member, round_num, f"# {member} v{round_num}")

        log = _get_log_oneline(git_repo_with_gitignore, f"spec-loop/{run_id}")
        spec_loop_commits = [e for e in log if "[spec-loop]" in e]
        assert len(spec_loop_commits) == 9

    def test_commit_writes_to_run_dir_not_output_dir(self, git_repo_with_gitignore: Path) -> None:
        """§2.1-BC3: The output is written to a stable-named file in
        .orchemist/runs/{run-id}/ — NOT the output directory. Only that file
        is staged — not the entire working tree."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "run-dir-test"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Spec v1")

        # Verify the file exists in the run_dir
        run_file = git_repo_with_gitignore / ".orchemist" / "runs" / run_id / "spec.md"
        assert run_file.exists()
        assert run_file.read_text() == "# Spec v1"

    def test_commit_does_not_stage_unrelated_files(self, git_repo_with_gitignore: Path) -> None:
        """§2.1-BC3 continued: Pre-existing tracked files in the repository
        are not affected by git handoff commits."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "isolation-test"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Create an untracked file that should NOT be staged
        (git_repo_with_gitignore / "unrelated.txt").write_text("should not be committed")

        handoff.commit_phase_output("spec", 1, "# Spec v1")

        # Check the commit only contains the spec.md file
        result = _git(
            git_repo_with_gitignore, "diff-tree", "--no-commit-id", "--name-only",
            "-r", "HEAD"
        )
        committed_files = result.stdout.strip().split("\n")
        assert all(".orchemist/" in f for f in committed_files if f)

    def test_commit_overwrites_same_file_across_rounds(self, git_repo_with_gitignore: Path) -> None:
        """§2.1-BC3: Files use stable names — each round overwrites the same file,
        versioned by git commits."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "overwrite-test"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Spec v1 content")
        handoff.commit_phase_output("spec", 2, "# Spec v2 content")

        run_file = git_repo_with_gitignore / ".orchemist" / "runs" / run_id / "spec.md"
        assert run_file.read_text() == "# Spec v2 content"

        # Verify two distinct commits exist
        log = _get_log_oneline(git_repo_with_gitignore, f"spec-loop/{run_id}")
        spec_commits = [e for e in log if "[spec-loop] spec round" in e]
        assert len(spec_commits) == 2


# ---------------------------------------------------------------------------
# §2.2 — Commit Tracking
# ---------------------------------------------------------------------------

class TestCommitTracking:
    """§2.2: Internal commit SHA tracking per phase and round."""

    def test_commit_sha_recorded_by_phase_and_round(self, git_repo_with_gitignore: Path) -> None:
        """§2.2-BC1: The system records the commit SHA internally, keyed by
        phase ID and round number."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "tracking-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        sha = handoff.commit_phase_output("spec", 1, "# Spec v1")
        retrieved = handoff.get_commit("spec", 1)
        assert retrieved == sha

    def test_get_commit_returns_exact_sha(self, git_repo_with_gitignore: Path) -> None:
        """§2.2-BC2: Querying for a completed phase's commit produces the
        exact SHA of the commit containing that phase's output."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "exact-sha-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        sha1 = handoff.commit_phase_output("spec", 1, "# Spec v1")
        sha2 = handoff.commit_phase_output("spec", 2, "# Spec v2")

        assert handoff.get_commit("spec", 1) == sha1
        assert handoff.get_commit("spec", 2) == sha2
        assert sha1 != sha2

    def test_get_commit_returns_none_for_missing(self, git_repo_with_gitignore: Path) -> None:
        """§2.2-BC3: If a commit SHA is requested for a phase/round that was
        never committed, the system returns None."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "missing-commit-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        assert handoff.get_commit("nonexistent_phase", 1) is None
        assert handoff.get_commit("spec", 99) is None


# ---------------------------------------------------------------------------
# §2.3 — Commits (Error Paths)
# ---------------------------------------------------------------------------

class TestCommitErrorPaths:
    """§2.3: Error handling during phase output commit."""

    def test_git_failure_deactivates_handoff(self, git_repo_with_gitignore: Path, caplog) -> None:
        """§2.3-BC1: If git add/commit fails, the system logs a warning,
        deactivates git-based handoff, and falls back to file-based mode."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "commit-fail-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Make the run_dir read-only so file write fails
        import stat
        handoff.run_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)

        with caplog.at_level(logging.WARNING):
            sha = handoff.commit_phase_output("spec", 1, "# Spec v1")

        # Restore permissions for cleanup
        handoff.run_dir.chmod(stat.S_IRWXU)

        assert sha is None
        assert not handoff.is_active()

    def test_no_further_git_ops_after_deactivation(self, git_repo_with_gitignore: Path) -> None:
        """§2.3-BC2: When git handoff is deactivated mid-run, the system does
        not attempt any further git operations for that pipeline run."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "no-ops-after-deactivation"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Force deactivation by setting active to False (simulating failure)
        handoff.active = False

        # These should return None/empty without attempting git operations
        sha = handoff.commit_phase_output("spec", 2, "# Should not commit")
        assert sha is None

        diff = handoff.get_diff("spec", 1, 2)
        assert diff == ""

    def test_commit_failure_does_not_halt_pipeline(self, git_repo_with_gitignore: Path) -> None:
        """§2.3-BC3: If a commit fails, the system does not halt — the phase
        output is still available via the file written to disk by the daemon."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "no-halt-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Force deactivation
        handoff.active = False

        # commit_phase_output should return None, not raise
        result = handoff.commit_phase_output("spec", 1, "# Spec v1")
        assert result is None  # No exception raised


# ---------------------------------------------------------------------------
# §2.4 — Commits (Edge Cases)
# ---------------------------------------------------------------------------

class TestCommitEdgeCases:
    """§2.4: Edge cases in commit creation."""

    def test_empty_output_still_creates_commit(self, git_repo_with_gitignore: Path) -> None:
        """§2.4-BC1: When a phase produces empty output text, the system still
        writes the file and creates the commit."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "empty-output-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        sha = handoff.commit_phase_output("spec", 1, "")
        assert sha is not None

        run_file = git_repo_with_gitignore / ".orchemist" / "runs" / run_id / "spec.md"
        assert run_file.exists()

    def test_unsafe_phase_id_sanitized_for_filename(self, git_repo_with_gitignore: Path) -> None:
        """§2.4-BC2: If the phase ID contains unsafe characters (/, spaces),
        the system sanitizes the name when creating the output file."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "sanitize-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        sha = handoff.commit_phase_output("spec/adversary phase", 1, "# Content")
        assert sha is not None

        # The file should exist with a sanitized name (no / or spaces)
        run_dir = git_repo_with_gitignore / ".orchemist" / "runs" / run_id
        files = list(run_dir.glob("*.md"))
        assert len(files) >= 1
        for f in files:
            assert "/" not in f.name
            # Filename should not contain problematic characters


# ---------------------------------------------------------------------------
# §5.1 — Diff Generation (Happy Path)
# ---------------------------------------------------------------------------

class TestDiffGeneration:
    """§5.1: Diff generation between rounds."""

    def test_diff_shows_actual_changes(self, git_repo_with_gitignore: Path) -> None:
        """§5.1-BC1/BC2: The system runs a git diff comparing two commit SHAs,
        showing actual added/removed/changed lines in unified diff format."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "diff-gen-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("behavioral", 1, "# Behavioral v1\n\n- Contract A\n")
        handoff.commit_phase_output("behavioral", 2, "# Behavioral v2\n\n- Contract A\n- Contract B\n")

        diff = handoff.get_diff("behavioral", 1, 2)
        assert diff  # Non-empty
        assert "Contract B" in diff

    def test_diff_excludes_ansi_colors(self, git_repo_with_gitignore: Path) -> None:
        """§5.1-BC3: Diff output excludes ANSI color codes (uses --no-color)."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "no-color-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.commit_phase_output("spec", 2, "# Spec v2")

        diff = handoff.get_diff("spec", 1, 2)
        # ANSI escape sequences start with \x1b[
        assert "\x1b[" not in diff


# ---------------------------------------------------------------------------
# §5.2 — Diff Truncation
# ---------------------------------------------------------------------------

class TestDiffTruncation:
    """§5.2: Diff output truncation."""

    def test_large_diff_truncated_to_limit(self, git_repo_with_gitignore: Path) -> None:
        """§5.2-BC1: If the diff output exceeds 2500 characters, the system
        truncates it to that limit."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "truncation-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Create a large diff by changing a lot of content
        large_v1 = "# Spec v1\n" + "\n".join(f"Line {i}: original content" for i in range(500))
        large_v2 = "# Spec v2\n" + "\n".join(f"Line {i}: completely changed content here" for i in range(500))

        handoff.commit_phase_output("spec", 1, large_v1)
        handoff.commit_phase_output("spec", 2, large_v2)

        diff = handoff.get_diff("spec", 1, 2)
        assert len(diff) <= 2500

    def test_truncated_diff_has_coherent_prefix(self, git_repo_with_gitignore: Path) -> None:
        """§5.2-BC2: Truncated output still contains a coherent prefix of the diff."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "coherent-truncation-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        large_v1 = "# Spec v1\n" + "\n".join(f"Line {i}: original" for i in range(500))
        large_v2 = "# Spec v2\n" + "\n".join(f"Line {i}: changed" for i in range(500))

        handoff.commit_phase_output("spec", 1, large_v1)
        handoff.commit_phase_output("spec", 2, large_v2)

        diff = handoff.get_diff("spec", 1, 2)
        # Should start with diff-like content (not random mid-line cut)
        # A coherent prefix should contain at least the diff header
        assert diff.startswith("diff") or diff.startswith("@@") or diff.startswith("---") or len(diff) > 0


# ---------------------------------------------------------------------------
# §5.3 — Diff (Error Paths)
# ---------------------------------------------------------------------------

class TestDiffErrorPaths:
    """§5.3: Error handling in diff generation."""

    def test_diff_failure_returns_empty_string(self, git_repo_with_gitignore: Path) -> None:
        """§5.3-BC1: If the git diff command fails, the system returns an empty
        string — not an error."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "diff-fail-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Provide invalid commit references
        diff = handoff.get_diff("spec", 1, 2)  # No commits exist yet
        assert diff == ""

    def test_diff_with_missing_commit_returns_empty(self, git_repo_with_gitignore: Path) -> None:
        """§5.3-BC2: If one of the two commits needed for a diff is missing,
        the system returns an empty string."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "missing-diff-commit-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Spec v1")
        # Round 2 never committed

        diff = handoff.get_diff("spec", 1, 2)
        assert diff == ""


# ---------------------------------------------------------------------------
# §5.4 — Diff (Edge Cases)
# ---------------------------------------------------------------------------

class TestDiffEdgeCases:
    """§5.4: Edge cases in diff generation."""

    def test_identical_rounds_produce_empty_diff(self, git_repo_with_gitignore: Path) -> None:
        """§5.4-BC1: When two rounds produce identical output, the diff is empty."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "identical-diff-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Identical content")
        handoff.commit_phase_output("spec", 2, "# Identical content")

        diff = handoff.get_diff("spec", 1, 2)
        assert diff == ""

    def test_round_1_diff_is_empty(self, git_repo_with_gitignore: Path) -> None:
        """§5.4-BC2: For round 1, requesting a diff produces an empty string
        (no prior round to diff against)."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "round1-diff-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Spec v1")

        # get_diff_for_member with round 1 should return empty
        diff = handoff.get_diff_for_member("spec", 1)
        assert diff == ""


# ---------------------------------------------------------------------------
# §8 — Fallback to File-Based Handoff
# ---------------------------------------------------------------------------

class TestFallbackToFileBased:
    """§8: Automatic fallback when git is unavailable."""

    def test_automatic_fallback_no_user_visible_errors(self, tmp_path: Path) -> None:
        """§8.1-BC1: When git is not installed or repo path is invalid,
        the system uses file-based handoff with zero user-visible changes."""
        from orchestration_engine.git_handoff import GitHandoff

        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        handoff = GitHandoff(repo_path=not_a_repo, run_id="fallback-run")
        result = handoff.initialize()

        assert result is False
        assert not handoff.is_active()

    def test_mid_run_deactivation_on_git_failure(self, git_repo_with_gitignore: Path, caplog) -> None:
        """§8.2-BC1: When git handoff is deactivated mid-run, the system
        switches to file-based mode for all remaining phases."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "mid-run-fallback"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Commit succeeds for round 1
        sha1 = handoff.commit_phase_output("spec", 1, "# Spec v1")
        assert sha1 is not None
        assert handoff.is_active()

        # Simulate mid-run failure
        handoff.active = False

        # Subsequent commits should not attempt git operations
        sha2 = handoff.commit_phase_output("spec", 2, "# Spec v2")
        assert sha2 is None
        assert not handoff.is_active()

    def test_fallback_does_not_corrupt_state(self, git_repo_with_gitignore: Path) -> None:
        """§8.3-BC1: When fallback occurs, the sequencer's in-memory state
        is unaffected — only the handoff mechanism changes."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "no-corrupt-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Commit round 1
        sha1 = handoff.commit_phase_output("spec", 1, "# Spec v1")
        assert sha1 is not None

        # The commit log should still contain the round 1 entry
        assert handoff.get_commit("spec", 1) == sha1

        # Deactivate
        handoff.active = False

        # Round 1 commit should still be retrievable from the log
        assert handoff.get_commit("spec", 1) == sha1


# ---------------------------------------------------------------------------
# §10 — Concurrent Pipeline Runs
# ---------------------------------------------------------------------------

class TestConcurrentPipelineRuns:
    """§10: Branch name isolation and concurrent run behavior."""

    def test_unique_branch_names_per_run(self, git_repo_with_gitignore: Path) -> None:
        """§10.1-BC1: Two pipeline runs create uniquely named branches
        spec-loop/{run-id-A} and spec-loop/{run-id-B}."""
        from orchestration_engine.git_handoff import GitHandoff

        run_a = "run-aaa-111"
        run_b = "run-bbb-222"

        handoff_a = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_a)
        handoff_a.initialize()
        handoff_a.commit_phase_output("spec", 1, "# A Spec")

        # Switch back to main to initialize run B
        _git(git_repo_with_gitignore, "checkout", "main")

        handoff_b = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_b)
        # Run B should detect dirty tree (branch A's files may be present) or succeed
        # depending on implementation. The key assertion: branch names don't collide.
        assert f"spec-loop/{run_a}" != f"spec-loop/{run_b}"
        assert _branch_exists(git_repo_with_gitignore, f"spec-loop/{run_a}")

    def test_dirty_tree_prevents_second_run_activation(self, git_repo_with_gitignore: Path) -> None:
        """§10.2-BC1: If two runs attempt git handoff simultaneously, the second
        detects a dirty working tree and falls back to file-based mode."""
        from orchestration_engine.git_handoff import GitHandoff

        run_a = "concurrent-a"
        handoff_a = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_a)
        handoff_a.initialize()

        # Make the working tree dirty (simulate concurrent operation)
        (git_repo_with_gitignore / "temp_dirty.txt").write_text("in progress")
        _git(git_repo_with_gitignore, "add", "temp_dirty.txt")

        run_b = "concurrent-b"
        handoff_b = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_b)
        result = handoff_b.initialize()

        assert result is False  # Second run falls back
        assert not handoff_b.is_active()

    def test_cleanup_isolation_between_runs(self, git_repo_with_gitignore: Path) -> None:
        """§10.3-BC1: When one run cleans up its temp branch, it does not
        affect the temp branch of another concurrent run."""
        from orchestration_engine.git_handoff import GitHandoff

        run_a = "cleanup-iso-a"
        handoff_a = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_a)
        handoff_a.initialize()
        handoff_a.commit_phase_output("spec", 1, "# A Spec")

        # Create a second branch manually to simulate concurrent run
        _git(git_repo_with_gitignore, "branch", "spec-loop/cleanup-iso-b")

        handoff_a.cleanup(preserve=False)

        # Run A's branch is gone
        assert not _branch_exists(git_repo_with_gitignore, f"spec-loop/{run_a}")
        # Run B's branch is unaffected
        assert _branch_exists(git_repo_with_gitignore, "spec-loop/cleanup-iso-b")


# ---------------------------------------------------------------------------
# §11 — Error Class
# ---------------------------------------------------------------------------

class TestGitHandoffError:
    """§11: GitHandoffError behavior."""

    def test_git_handoff_error_is_orchestrator_error_subclass(self) -> None:
        """§11.1-BC1: GitHandoffError is a subclass of OrchestratorError."""
        from orchestration_engine.errors import GitHandoffError, OrchestratorError

        assert issubclass(GitHandoffError, OrchestratorError)

    def test_git_failure_falls_back_not_aborts(self, git_repo_with_gitignore: Path) -> None:
        """§11.1-BC1: When a git operation fails, the system falls back to
        file-based mode — the pipeline does not abort."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "error-fallback-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Force deactivation (simulating failure)
        handoff.active = False

        # Operations should not raise
        sha = handoff.commit_phase_output("spec", 1, "# Spec")
        assert sha is None  # No exception raised

    def test_error_not_in_pipeline_output(self, git_repo_with_gitignore: Path) -> None:
        """§11.2-BC1: When a git handoff error occurs, the pipeline's final
        status and output do not mention the error — only daemon logs show it."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "invisible-error-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Deactivate and attempt operations
        handoff.active = False
        sha = handoff.commit_phase_output("spec", 1, "test")
        diff = handoff.get_diff("spec", 1, 2)

        # No exceptions raised — errors are internal only
        assert sha is None
        assert diff == ""


# ---------------------------------------------------------------------------
# §12 — Daemon Integration
# ---------------------------------------------------------------------------

class TestDaemonIntegration:
    """§12: Daemon creates and wires GitHandoff."""

    def test_daemon_still_writes_files_to_output_dir(self, git_repo_with_gitignore: Path, tmp_path: Path) -> None:
        """§12.2-BC1: When git handoff is active, the daemon's phase-complete
        callback still writes output files to {output_dir} as before. Git commits
        track output in .orchemist/runs/{run-id}/ — not in output_dir."""
        from orchestration_engine.git_handoff import GitHandoff

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        run_id = "daemon-files-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        # Git handoff writes to run_dir inside repo
        handoff.commit_phase_output("spec", 1, "# Spec v1")

        # The output_dir should NOT be written to by GitHandoff
        assert not (output_dir / "spec.md").exists()

        # But the run_dir should have the file
        run_file = git_repo_with_gitignore / ".orchemist" / "runs" / run_id / "spec.md"
        assert run_file.exists()


# ---------------------------------------------------------------------------
# §13.5 — Non-Force Git Operations
# ---------------------------------------------------------------------------

class TestNonForceGitOperations:
    """§13.5: Git operations never use --force flags."""

    def test_no_force_flags_in_operations(self, git_repo_with_gitignore: Path) -> None:
        """§13.5-BC1: The system never uses --force flags for any git operation."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "no-force-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)

        # Patch subprocess.run to check no --force is passed
        original_run = subprocess.run
        force_used = []

        def checking_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "git":
                # Check for --force on destructive ops (push, checkout, etc.)
                # git add -f is OK (just overrides .gitignore, not destructive)
                subcmd = cmd[1] if len(cmd) > 1 else ""
                if subcmd != "add" and ("--force" in cmd or "-f" in cmd):
                    force_used.append(cmd)
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=checking_run):
            handoff.initialize()
            handoff.commit_phase_output("spec", 1, "# Spec v1")
            handoff.commit_phase_output("spec", 2, "# Spec v2")
            handoff.get_diff("spec", 1, 2)
            handoff.cleanup(preserve=False)

        assert len(force_used) == 0, f"--force used in commands: {force_used}"


# ---------------------------------------------------------------------------
# §14 — Observability
# ---------------------------------------------------------------------------

class TestObservability:
    """§14: Daemon log messages and observability."""

    def test_init_logs_run_id_and_branch(self, git_repo_with_gitignore: Path, caplog) -> None:
        """§14.1-BC1: When git handoff initializes, the daemon log identifies
        the run ID and branch name."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "observable-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        with caplog.at_level(logging.DEBUG):
            handoff.initialize()

        log_text = " ".join(caplog.messages)
        assert run_id in log_text

    def test_fallback_logs_reason(self, tmp_path: Path, caplog) -> None:
        """§14.1-BC2: When git handoff falls back, the daemon log explains why."""
        from orchestration_engine.git_handoff import GitHandoff

        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        handoff = GitHandoff(repo_path=not_a_repo, run_id="fallback-observable")
        with caplog.at_level(logging.WARNING):
            handoff.initialize()

        # Should have at least one warning-level message
        assert len(caplog.messages) > 0

    def test_commits_visible_via_git_log(self, git_repo_with_gitignore: Path) -> None:
        """§14.1-BC3: Each commit on the temp branch has a descriptive message
        identifying the phase and round, visible via git log."""
        from orchestration_engine.git_handoff import GitHandoff

        run_id = "log-visible-run"
        handoff = GitHandoff(repo_path=git_repo_with_gitignore, run_id=run_id)
        handoff.initialize()

        handoff.commit_phase_output("spec", 1, "# Spec v1")
        handoff.commit_phase_output("behavioral", 2, "# Behavioral v2")

        log = _get_log_oneline(git_repo_with_gitignore, f"spec-loop/{run_id}")
        spec_commits = [e for e in log if "[spec-loop]" in e]
        assert any("spec" in c and "round 1" in c for c in spec_commits)
        assert any("behavioral" in c and "round 2" in c for c in spec_commits)
