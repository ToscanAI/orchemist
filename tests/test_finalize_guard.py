"""Acceptance tests for GitHandoff.finalize() size guard — issue #679.

Tests the behavioral contracts for the size guard added to finalize():
before copying a file from the spec-loop git branch to output_dir, it checks
whether a larger agent-written file already exists at the destination.
If so, it keeps the larger file.

These tests are ACCEPTANCE tests only — they verify observable behavior,
not implementation details. They are expected to FAIL until the production
code is implemented.

Classes:
    TestFinalizeGuardCore       — happy-path size guard contracts
    TestFinalizeGuardEdgeCases  — boundary conditions and error paths
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers (mirrors patterns in test_git_handoff.py)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in *repo*."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(tmp_path: Path, *, branch: str = "main") -> Path:
    """Create a minimal git repo with one initial commit on *branch*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch)
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# Test Repo\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def _make_handoff(repo: Path, run_id: str = "test-679"):
    """Construct and initialize a GitHandoff instance, return it."""
    from orchestration_engine.git_handoff import GitHandoff

    handoff = GitHandoff(repo_path=repo, run_id=run_id)
    initialized = handoff.initialize()
    assert initialized, "GitHandoff.initialize() must succeed for finalize tests"
    return handoff


def _write_run_dir_file(handoff, filename: str, content: str) -> Path:
    """Write *content* to *filename* inside handoff.run_dir and return the path."""
    path = handoff.run_dir / filename
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Provide a clean git repo with one commit on 'main'."""
    return _init_repo(tmp_path)


@pytest.fixture
def target_branch(git_repo: Path) -> str:
    """Ensure a 'main' branch exists to receive the finalize commit."""
    # 'main' was created by _init_repo; just return the name
    return "main"


# ---------------------------------------------------------------------------
# TestFinalizeGuardCore — happy-path size guard contracts
# ---------------------------------------------------------------------------

class TestFinalizeGuardCore:
    """Happy-path behavioral contracts for the size guard in finalize()."""

    def test_larger_agent_file_is_kept(
        self, git_repo: Path, target_branch: str, tmp_path: Path
    ) -> None:
        """#679-BC1: Given agent-written file in output_dir is LARGER than the
        git-committed version, finalize() keeps the agent file and does NOT
        overwrite it.

        Observable outcome: the file in output_dir still contains the larger
        agent content after finalize() returns.
        """
        handoff = _make_handoff(git_repo, run_id="679-bc1")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Git (run_dir) version — small
        git_content = "short git version\n"
        _write_run_dir_file(handoff, "spec.md", git_content)

        # Agent version in output_dir — larger
        agent_content = "x" * (len(git_content) + 100)
        agent_file = output_dir / "spec.md"
        agent_file.write_text(agent_content)

        agent_size_before = agent_file.stat().st_size

        handoff.finalize(output_dir=output_dir, target_branch=target_branch)

        # Agent file must still be the larger version
        assert agent_file.exists(), "output_dir/spec.md must still exist after finalize()"
        assert agent_file.stat().st_size == agent_size_before, (
            "finalize() must NOT overwrite a larger agent file with a smaller git version"
        )
        assert agent_file.read_text() == agent_content, (
            "Content of larger agent file must be unchanged after finalize()"
        )

    def test_smaller_agent_file_is_overwritten(
        self, git_repo: Path, target_branch: str, tmp_path: Path
    ) -> None:
        """#679-BC2: Given agent-written file in output_dir is SMALLER than the
        git-committed version, finalize() copies the git version (existing
        behavior preserved).

        Observable outcome: output_dir file contains the git version content.
        """
        handoff = _make_handoff(git_repo, run_id="679-bc2")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Git (run_dir) version — larger
        git_content = "x" * 500
        _write_run_dir_file(handoff, "spec.md", git_content)

        # Agent version in output_dir — smaller
        agent_content = "tiny\n"
        agent_file = output_dir / "spec.md"
        agent_file.write_text(agent_content)

        handoff.finalize(output_dir=output_dir, target_branch=target_branch)

        assert agent_file.exists()
        assert agent_file.read_text() == git_content, (
            "finalize() must overwrite a smaller agent file with the larger git version"
        )

    def test_missing_agent_file_is_copied_from_git(
        self, git_repo: Path, target_branch: str, tmp_path: Path
    ) -> None:
        """#679-BC3: Given NO agent-written file in output_dir, finalize() copies
        the git version normally (existing behavior preserved).

        Observable outcome: output_dir file is created with the git version content.
        """
        handoff = _make_handoff(git_repo, run_id="679-bc3")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        git_content = "fresh from git\n"
        _write_run_dir_file(handoff, "spec.md", git_content)

        # No pre-existing file in output_dir
        dest_file = output_dir / "spec.md"
        assert not dest_file.exists()

        handoff.finalize(output_dir=output_dir, target_branch=target_branch)

        assert dest_file.exists(), "finalize() must create output_dir/spec.md when it is missing"
        assert dest_file.read_text() == git_content, (
            "finalize() must copy the git version when no agent file exists"
        )


# ---------------------------------------------------------------------------
# TestFinalizeGuardEdgeCases — boundary conditions and error paths
# ---------------------------------------------------------------------------

class TestFinalizeGuardEdgeCases:
    """Edge-case behavioral contracts for the size guard in finalize()."""

    def test_same_size_git_version_wins(
        self, git_repo: Path, target_branch: str, tmp_path: Path
    ) -> None:
        """#679-EC1: Given agent-written file and git version are the SAME SIZE,
        finalize() copies the git version (tie-breaks to git).

        Observable outcome: output_dir file contains the git version content.
        Rationale: the guard condition is strictly-greater-than, so equal size
        does NOT trigger the skip.
        """
        handoff = _make_handoff(git_repo, run_id="679-ec1")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Both exactly 20 bytes
        git_content = "git_version_AAAAAAAA"   # 20 chars
        agent_content = "agent_ver_BBBBBBBBBB"  # 20 chars
        assert len(git_content) == len(agent_content), "Test setup: sizes must be equal"

        _write_run_dir_file(handoff, "spec.md", git_content)
        agent_file = output_dir / "spec.md"
        agent_file.write_text(agent_content)

        handoff.finalize(output_dir=output_dir, target_branch=target_branch)

        assert agent_file.read_text() == git_content, (
            "finalize() must copy git version when sizes are equal (tie-breaks to git)"
        )

    def test_empty_agent_file_git_version_wins(
        self, git_repo: Path, target_branch: str, tmp_path: Path
    ) -> None:
        """#679-EC2: Given output_dir file is 0 bytes (empty), git version wins.

        Observable outcome: output_dir file is replaced with the non-empty git version.
        Rationale: 0 is not larger than any positive size, so guard does not fire.
        """
        handoff = _make_handoff(git_repo, run_id="679-ec2")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        git_content = "non-empty git content\n"
        _write_run_dir_file(handoff, "spec.md", git_content)

        # Agent file is empty (0 bytes)
        agent_file = output_dir / "spec.md"
        agent_file.write_bytes(b"")
        assert agent_file.stat().st_size == 0

        handoff.finalize(output_dir=output_dir, target_branch=target_branch)

        assert agent_file.stat().st_size > 0, (
            "finalize() must overwrite a 0-byte agent file with git version"
        )
        assert agent_file.read_text() == git_content

    def test_non_md_files_in_output_dir_are_ignored(
        self, git_repo: Path, target_branch: str, tmp_path: Path
    ) -> None:
        """#679-EC3: Given output_dir has files that are NOT .md, finalize()
        ignores them (glob is *.md only).

        Observable outcome: non-.md files in output_dir are untouched after
        finalize(); finalize() does not raise an error.
        """
        handoff = _make_handoff(git_repo, run_id="679-ec3")
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Prepare one .md file in run_dir so finalize() has something to do
        _write_run_dir_file(handoff, "spec.md", "git spec\n")

        # Non-.md files in output_dir with specific content
        txt_file = output_dir / "notes.txt"
        txt_file.write_text("important notes")
        json_file = output_dir / "config.json"
        json_file.write_text('{"key": "value"}')
        py_file = output_dir / "script.py"
        py_file.write_text("print('hello')")

        # finalize() must not raise
        handoff.finalize(output_dir=output_dir, target_branch=target_branch)

        # Non-.md files must be untouched
        assert txt_file.read_text() == "important notes", "notes.txt must not be modified"
        assert json_file.read_text() == '{"key": "value"}', "config.json must not be modified"
        assert py_file.read_text() == "print('hello')", "script.py must not be modified"

    def test_missing_output_dir_does_not_crash(
        self, git_repo: Path, target_branch: str, tmp_path: Path, caplog
    ) -> None:
        """#679-EC4: Given output_dir does not exist, finalize() logs a warning
        and does not crash (no unhandled exception).

        Observable outcome: finalize() returns without raising; a warning is logged.
        """
        handoff = _make_handoff(git_repo, run_id="679-ec4")
        output_dir = tmp_path / "nonexistent_output"
        assert not output_dir.exists()

        # Prepare a file in run_dir so finalize() has content to work with
        _write_run_dir_file(handoff, "spec.md", "some content\n")

        with caplog.at_level(logging.WARNING):
            # Must not raise — either succeeds gracefully or logs warning
            try:
                handoff.finalize(output_dir=output_dir, target_branch=target_branch)
            except Exception as exc:
                pytest.fail(
                    f"finalize() must not raise when output_dir does not exist, "
                    f"but raised {type(exc).__name__}: {exc}"
                )

        # A warning must have been logged about the missing output_dir
        warning_messages = [
            msg for msg in caplog.messages
            if "output" in msg.lower() or "warn" in msg.lower() or "missing" in msg.lower()
               or "exist" in msg.lower() or "679" in msg or "output_dir" in msg.lower()
        ]
        assert warning_messages, (
            f"finalize() must log a warning when output_dir does not exist. "
            f"Got log messages: {caplog.messages}"
        )
