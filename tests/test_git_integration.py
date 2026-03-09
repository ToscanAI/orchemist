"""Unit tests for git_integration.py.

All tests use local git repos created in tmp_path (pytest fixture).
No real remote required — a bare local repo serves as the "remote".

Tests cover:
- Branch creation (default/custom pattern, collision suffix, max retries)
- Pre-flight checks (dirty dir, not-a-repo, detached HEAD, git not installed)
- Stage-and-commit (with and without changes)
- Branch diff output
- Push to local remote
- Push when no remote exists
- Cleanup (success vs failure)
- Safety: never force-push
- Branch name sanitisation
- GitConfig defaults
- Disabled git config (git.enabled: false → no-op guard)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Tuple
from unittest.mock import patch

import pytest

from orchestration_engine.git_integration import (
    BranchInfo,
    CommitInfo,
    GitConfig,
    GitContext,
    GitError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(args: list, cwd: Path, **kw) -> subprocess.CompletedProcess:
    """Run a git command in the given directory, raising on failure."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        **kw,
    )


def _git_nocheck(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def git_repos(tmp_path: Path) -> dict:
    """Create a bare 'remote' and a working clone with an initial commit on main.

    Returns:
        Dict with keys ``"remote"`` and ``"working"``.
    """
    remote = tmp_path / "remote.git"
    working = tmp_path / "working"

    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(working)],
                   check=True, capture_output=True)

    # Configure local identity so commits work in CI
    _git(["config", "user.email", "test@example.com"], cwd=working)
    _git(["config", "user.name", "Test User"], cwd=working)

    (working / "README.md").write_text("# Test Repo\n")
    _git(["add", "."], cwd=working)
    _git(["commit", "-m", "init"], cwd=working)
    _git(["push", "-u", "origin", "main"], cwd=working)

    return {"remote": remote, "working": working}


@pytest.fixture()
def no_remote_repo(tmp_path: Path) -> Path:
    """Create a local git repo with no remote."""
    repo = tmp_path / "local"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    (repo / "README.md").write_text("# Local only\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    return repo


def _make_context(
    repos: dict,
    *,
    pipeline_id: str = "code-development-pipeline",
    run_id: str = "abc12345",
    commit_phases: list | None = None,
    branch_pattern: str = "feat/{pipeline_id}-{run_id}",
    push: bool = True,
    merge_gate: bool = False,
    create_pr: bool = False,
    base_branch: str | None = None,
    output_dir: Path | None = None,
) -> GitContext:
    config = GitConfig(
        enabled=True,
        branch_pattern=branch_pattern,
        auto_commit=True,
        commit_phases=commit_phases or ["implement", "fix", "test_generation"],
        working_dir=str(repos["working"]),
        push=push,
        merge_gate=merge_gate,
        create_pr=create_pr,
        base_branch=base_branch,
    )
    return GitContext(
        config=config,
        pipeline_id=pipeline_id,
        run_id=run_id,
        output_dir=output_dir or repos["working"],
    )


# ---------------------------------------------------------------------------
# Branch creation tests
# ---------------------------------------------------------------------------


def test_create_branch_default_pattern(git_repos: dict) -> None:
    """Branch is created with the correct name from the default pattern."""
    ctx = _make_context(git_repos)
    info = ctx.on_pipeline_start()

    assert isinstance(info, BranchInfo)
    assert info.branch_name == "feat/code-development-pipeline-abc12345"
    assert info.base_branch == "main"

    # Verify branch actually exists in git
    result = _git_nocheck(["branch", "--list", info.branch_name], cwd=git_repos["working"])
    assert info.branch_name in result.stdout


def test_create_branch_custom_pattern(git_repos: dict) -> None:
    """Custom branch_pattern with different tokens resolves correctly."""
    ctx = _make_context(git_repos, branch_pattern="orch/{run_id}", run_id="xyz99")
    info = ctx.on_pipeline_start()
    assert info.branch_name == "orch/xyz99"


def test_create_branch_collision_suffix(git_repos: dict) -> None:
    """If branch already exists, a -2 suffix is appended."""
    # Pre-create the branch so a collision occurs
    _git(["checkout", "-b", "feat/code-development-pipeline-abc12345"],
         cwd=git_repos["working"])
    _git(["checkout", "main"], cwd=git_repos["working"])

    ctx = _make_context(git_repos)
    info = ctx.on_pipeline_start()
    assert info.branch_name == "feat/code-development-pipeline-abc12345-2"


def test_create_branch_collision_max_retries(git_repos: dict) -> None:
    """After 5 collision retries, GitError is raised."""
    base = "feat/code-development-pipeline-abc12345"
    # Create original and 5 suffixed variants
    _git(["checkout", "-b", base], cwd=git_repos["working"])
    _git(["checkout", "main"], cwd=git_repos["working"])
    for i in range(2, 7):
        _git(["checkout", "-b", f"{base}-{i}"], cwd=git_repos["working"])
        _git(["checkout", "main"], cwd=git_repos["working"])

    ctx = _make_context(git_repos)
    with pytest.raises(GitError, match="after 5 attempts"):
        ctx.on_pipeline_start()


def test_base_branch_explicit(git_repos: dict) -> None:
    """Explicit base_branch is respected."""
    ctx = _make_context(git_repos, base_branch="main")
    info = ctx.on_pipeline_start()
    assert info.base_branch == "main"


def test_base_branch_nonexistent_raises(git_repos: dict) -> None:
    """Configuring a non-existent base_branch raises GitError."""
    ctx = _make_context(git_repos, base_branch="nonexistent-branch")
    with pytest.raises(GitError, match="does not exist"):
        ctx.on_pipeline_start()


# ---------------------------------------------------------------------------
# Pre-flight check tests
# ---------------------------------------------------------------------------


def test_dirty_working_dir_aborts(git_repos: dict) -> None:
    """Uncommitted changes cause a clear GitError."""
    (git_repos["working"] / "dirty.txt").write_text("I am dirty\n")
    ctx = _make_context(git_repos)
    with pytest.raises(GitError, match="dirty"):
        ctx.on_pipeline_start()


def test_not_a_git_repo(tmp_path: Path) -> None:
    """Non-repo directory produces a clear GitError."""
    plain_dir = tmp_path / "not_a_repo"
    plain_dir.mkdir()
    config = GitConfig(enabled=True, working_dir=str(plain_dir))
    ctx = GitContext(config=config, pipeline_id="test", run_id="abc",
                     output_dir=plain_dir)
    with pytest.raises(GitError, match="not inside a git repository"):
        ctx.on_pipeline_start()


def test_detached_head(git_repos: dict) -> None:
    """Detached HEAD produces a clear GitError."""
    # Detach HEAD to the initial commit
    result = _git(["rev-parse", "HEAD"], cwd=git_repos["working"])
    sha = result.stdout.strip()
    _git(["checkout", sha], cwd=git_repos["working"])

    ctx = _make_context(git_repos)
    with pytest.raises(GitError, match="detached"):
        ctx.on_pipeline_start()


def test_git_not_installed() -> None:
    """Missing git binary produces a clear GitError."""
    with patch(
        "orchestration_engine.git_integration.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        config = GitConfig(enabled=True, working_dir=".")
        ctx = GitContext(config=config, pipeline_id="test", run_id="abc")
        with pytest.raises(GitError, match="not found"):
            ctx.on_pipeline_start()


# ---------------------------------------------------------------------------
# Stage-and-commit tests
# ---------------------------------------------------------------------------


def test_stage_and_commit_with_changes(git_repos: dict) -> None:
    """Files added to working dir are staged and committed."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()

    # Simulate output file written by a sub-agent
    (git_repos["working"] / "new_feature.py").write_text("def hello(): pass\n")

    commit_info = ctx.on_phase_complete("implement", {"state": "success"})

    assert commit_info is not None
    assert isinstance(commit_info, CommitInfo)
    assert commit_info.phase_id == "implement"
    assert "implement" in commit_info.message
    assert commit_info.sha  # non-empty
    assert commit_info.files_changed >= 1


def test_stage_and_commit_no_changes(git_repos: dict) -> None:
    """If no files changed, commit is skipped and None is returned."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()

    # No files added or modified
    commit_info = ctx.on_phase_complete("implement", {"state": "success"})
    assert commit_info is None


def test_commit_only_for_listed_phases(git_repos: dict) -> None:
    """Phases NOT in commit_phases do not trigger a commit."""
    ctx = _make_context(git_repos, commit_phases=["implement"])
    ctx.on_pipeline_start()

    (git_repos["working"] / "review_output.txt").write_text("LGTM\n")
    commit_info = ctx.on_phase_complete("code_review", {"state": "success"})
    assert commit_info is None

    # File should still be untracked
    result = _git(["status", "--porcelain"], cwd=git_repos["working"])
    assert "review_output.txt" in result.stdout


def test_commit_skipped_if_auto_commit_false(git_repos: dict) -> None:
    """auto_commit=False disables all commits."""
    config = GitConfig(
        enabled=True,
        auto_commit=False,
        commit_phases=["implement"],
        working_dir=str(git_repos["working"]),
    )
    ctx = GitContext(config=config, pipeline_id="test", run_id="abc",
                     output_dir=git_repos["working"])
    ctx.on_pipeline_start()
    (git_repos["working"] / "file.py").write_text("x = 1\n")
    result = ctx.on_phase_complete("implement", {})
    assert result is None


# ---------------------------------------------------------------------------
# Diff tests
# ---------------------------------------------------------------------------


def test_get_diff_shows_changes(git_repos: dict) -> None:
    """Diff includes files added on the feature branch."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()
    (git_repos["working"] / "feature.py").write_text("# feature\ndef foo(): pass\n")
    ctx.on_phase_complete("implement", {})

    diff = ctx.get_branch_diff()
    assert "feature.py" in diff


def test_get_diff_empty_before_start() -> None:
    """get_branch_diff returns empty string if on_pipeline_start was not called."""
    config = GitConfig(enabled=True, working_dir=".")
    ctx = GitContext(config=config, pipeline_id="test", run_id="abc")
    assert ctx.get_branch_diff() == ""


# ---------------------------------------------------------------------------
# Push tests
# ---------------------------------------------------------------------------


def test_push_to_remote(git_repos: dict) -> None:
    """Branch appears on the remote after push."""
    ctx = _make_context(git_repos, push=True)
    info = ctx.on_pipeline_start()
    (git_repos["working"] / "impl.py").write_text("x = 1\n")
    ctx.on_phase_complete("implement", {})

    # Trigger push via on_pipeline_complete (merge_gate=False for simplicity)
    ctx.on_pipeline_complete(success=True)

    # Verify branch exists in the bare remote
    result = subprocess.run(
        ["git", "branch", "--list", info.branch_name],
        cwd=git_repos["remote"],
        capture_output=True,
        text=True,
    )
    assert info.branch_name in result.stdout


def test_push_no_remote(no_remote_repo: Path) -> None:
    """No remote → push is skipped with a warning (no crash)."""
    config = GitConfig(
        enabled=True,
        push=True,
        merge_gate=False,
        working_dir=str(no_remote_repo),
    )
    ctx = GitContext(config=config, pipeline_id="test", run_id="abc",
                     output_dir=no_remote_repo)
    ctx.on_pipeline_start()
    result = ctx.on_pipeline_complete(success=True)
    # Should complete without raising
    assert result.status in ("awaiting_approval", "skipped")


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


def test_cleanup_success_stays_on_branch(git_repos: dict) -> None:
    """On success, cleanup() is a no-op; we stay on the feature branch."""
    ctx = _make_context(git_repos)
    info = ctx.on_pipeline_start()

    ctx.cleanup(success=True)

    result = _git(["symbolic-ref", "--short", "HEAD"], cwd=git_repos["working"])
    assert result.stdout.strip() == info.branch_name


def test_cleanup_failure_restores_base(git_repos: dict) -> None:
    """On failure, cleanup() checks out the base branch."""
    ctx = _make_context(git_repos)
    info = ctx.on_pipeline_start()
    assert info.base_branch == "main"

    # Verify we're on the feature branch
    result = _git(["symbolic-ref", "--short", "HEAD"], cwd=git_repos["working"])
    assert result.stdout.strip() == info.branch_name

    ctx.cleanup(success=False)

    # Should be back on main
    result = _git(["symbolic-ref", "--short", "HEAD"], cwd=git_repos["working"])
    assert result.stdout.strip() == "main"


# ---------------------------------------------------------------------------
# Safety tests
# ---------------------------------------------------------------------------


def test_never_force_push(git_repos: dict) -> None:
    """_run_git raises ValueError if --force is in a push command."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()

    with pytest.raises(ValueError, match="not allowed"):
        ctx._run_git(
            ["git", "push", "--force", "origin", "main"],
            cwd=git_repos["working"],
        )


def test_never_force_push_short_flag(git_repos: dict) -> None:
    """_run_git raises ValueError if -f is used with push."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()

    with pytest.raises(ValueError, match="not allowed"):
        ctx._run_git(
            ["git", "push", "-f", "origin", "main"],
            cwd=git_repos["working"],
        )


def test_never_force_push_with_lease(git_repos: dict) -> None:
    """_run_git raises ValueError if --force-with-lease is used with push."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()

    with pytest.raises(ValueError, match="not allowed"):
        ctx._run_git(
            ["git", "push", "--force-with-lease", "origin", "main"],
            cwd=git_repos["working"],
        )


def test_force_not_rejected_for_non_push(git_repos: dict) -> None:
    """-f is not rejected for non-push commands (e.g. git checkout -f)."""
    ctx = _make_context(git_repos)
    ctx.on_pipeline_start()
    # This should NOT raise — -f is a valid flag for checkout
    try:
        ctx._run_git(
            ["git", "checkout", "-f", "HEAD"],
            cwd=git_repos["working"],
        )
    except GitError:
        pass  # git error (e.g. nothing to checkout) is OK; ValueError is not


def test_no_force_push_in_source() -> None:
    """Static analysis: the git_integration module must not contain --force
    combined with push as a constructed string literal."""
    import inspect
    import orchestration_engine.git_integration as mod

    source = inspect.getsource(mod)

    # These patterns would indicate a force-push being hard-coded
    forbidden_patterns = [
        '"--force"',
        "'--force'",
    ]
    # We allow those in the *rejection* checks themselves, so we verify
    # the pattern is only used in safety-check context (ValueError raise lines).
    # The simplest check: ensure we never have a list containing both
    # "push" and "--force" as adjacent positional values in a _run_git call
    # other than inside the ValueError check block.
    # We do a coarse but reliable check: the word "--force" must NOT appear
    # inside a list literal passed to _run_git (i.e., not inside [...]).
    import ast
    tree = ast.parse(source)

    class ForcePushFinder(ast.NodeVisitor):
        def __init__(self):
            self.violations = []

        def visit_Call(self, node):
            # Look for calls to _run_git with ["git", "push", ..., "--force", ...]
            if isinstance(node.func, ast.Attribute) and node.func.attr == "_run_git":
                for arg in node.args:
                    if isinstance(arg, ast.List):
                        elts = [
                            e.s if isinstance(e, ast.Constant) else None
                            for e in arg.elts
                        ]
                        if "push" in elts and "--force" in elts:
                            self.violations.append(ast.unparse(node))
            self.generic_visit(node)

    finder = ForcePushFinder()
    finder.visit(tree)
    assert not finder.violations, (
        f"Found _run_git calls with force-push: {finder.violations}"
    )


# ---------------------------------------------------------------------------
# Branch name sanitisation
# ---------------------------------------------------------------------------


def test_branch_name_sanitization(git_repos: dict) -> None:
    """Special chars in pipeline_id are sanitised for git."""
    ctx = _make_context(
        git_repos,
        pipeline_id="my pipeline! v2.0",
        run_id="abc",
        branch_pattern="feat/{pipeline_id}-{run_id}",
    )
    branch = ctx._make_branch_name()
    # No spaces, exclamation marks, etc.
    assert " " not in branch
    assert "!" not in branch
    # Should still contain recognizable parts
    assert "my" in branch
    assert "pipeline" in branch
    assert "abc" in branch


def test_branch_name_no_double_hyphens(git_repos: dict) -> None:
    """Consecutive hyphens are collapsed to a single hyphen."""
    ctx = _make_context(
        git_repos,
        pipeline_id="test--pipeline",
        run_id="x1",
        branch_pattern="feat/{pipeline_id}-{run_id}",
    )
    branch = ctx._make_branch_name()
    assert "--" not in branch


# ---------------------------------------------------------------------------
# GitConfig defaults
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    """Missing GitConfig fields get correct defaults."""
    cfg = GitConfig()
    assert cfg.enabled is False
    assert cfg.branch_pattern == "feat/{pipeline_id}-{run_id}"
    assert cfg.auto_commit is True
    assert cfg.commit_phases == []
    assert cfg.working_dir == "."
    assert cfg.push is True
    assert cfg.merge_gate is True
    assert cfg.create_pr is False
    assert cfg.base_branch is None


def test_config_disabled_flag() -> None:
    """GitConfig with enabled=False is a sentinel for 'do nothing'."""
    cfg = GitConfig(enabled=False)
    assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Gate file tests
# ---------------------------------------------------------------------------


def test_gate_file_written(git_repos: dict, tmp_path: Path) -> None:
    """on_pipeline_complete writes a gate file when merge_gate=True."""
    output_dir = tmp_path / "output"
    ctx = _make_context(
        git_repos,
        push=False,  # skip push since we have a remote — keep test fast
        merge_gate=True,
        output_dir=output_dir,
    )
    ctx.on_pipeline_start()
    (git_repos["working"] / "impl.py").write_text("x = 1\n")
    ctx.on_phase_complete("implement", {})

    result = ctx.on_pipeline_complete(success=True)
    assert result.status == "awaiting_approval"

    gate_file = output_dir / "_gate.json"
    assert gate_file.exists()

    import json
    gate_data = json.loads(gate_file.read_text())
    assert gate_data["status"] == "awaiting_approval"
    assert "branch" in gate_data
    assert "approve_command" in gate_data
    assert "reject_command" in gate_data


def test_gate_load_and_update(tmp_path: Path) -> None:
    """load_gate and update_gate_status work correctly."""
    import json

    # Temporarily redirect GATES_DIR to tmp_path
    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    GitContext.GATES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        gate_data = {
            "run_id": "test123",
            "status": "awaiting_approval",
            "branch": "feat/test123",
            "base_branch": "main",
            "pipeline_id": "test-pipeline",
            "diff_stats": "+5 -2",
            "commits": [],
            "output_dir": str(tmp_path),
            "created_at": "2025-01-01T00:00:00+00:00",
            "approve_command": "orch gate approve test123",
            "reject_command": "orch gate reject test123",
            "create_pr": False,
        }
        (GitContext.GATES_DIR / "test123.json").write_text(json.dumps(gate_data))

        loaded = GitContext.load_gate("test123")
        assert loaded is not None
        assert loaded["status"] == "awaiting_approval"

        updated = GitContext.update_gate_status("test123", "approved", message="LGTM")
        assert updated["status"] == "approved"

        # Reload to verify persistence
        reloaded = GitContext.load_gate("test123")
        assert reloaded["status"] == "approved"

    finally:
        GitContext.GATES_DIR = original_gates_dir


def test_load_gate_missing_returns_none(tmp_path: Path) -> None:
    """Loading a non-existent gate returns None."""
    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    try:
        result = GitContext.load_gate("does-not-exist")
        assert result is None
    finally:
        GitContext.GATES_DIR = original_gates_dir


def test_list_gates_empty(tmp_path: Path) -> None:
    """list_gates returns empty list when no gates exist."""
    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "nonexistent-gates"
    try:
        gates = GitContext.list_gates()
        assert gates == []
    finally:
        GitContext.GATES_DIR = original_gates_dir


# ===========================================================================
# Tests for GitContext.create_gate (Issue #495)
# ===========================================================================


def test_create_gate_minimal(tmp_path: Path) -> None:
    """create_gate with only run_id + branch_name writes a valid gate file."""
    import json

    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    try:
        gate = GitContext.create_gate(run_id="run-minimal", branch_name="feat/minimal")

        assert gate["run_id"] == "run-minimal"
        assert gate["branch"] == "feat/minimal"
        assert gate["status"] == "awaiting_approval"
        assert gate["base_branch"] == "main"
        assert gate["scoring_status"] is None
        assert gate["scoring_score"] is None
        assert gate["commits"] == []
        assert gate["diff_stats"] == ""

        gate_file = GitContext.GATES_DIR / "run-minimal.json"
        assert gate_file.exists()
        on_disk = json.loads(gate_file.read_text())
        assert on_disk["branch"] == "feat/minimal"
    finally:
        GitContext.GATES_DIR = original_gates_dir


def test_create_gate_all_args(tmp_path: Path) -> None:
    """create_gate with all optional args writes correct gate file content."""
    import json

    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    try:
        gate = GitContext.create_gate(
            run_id="run-full",
            branch_name="fix/issue-42",
            repo_path="/home/user/myrepo",
            pipeline_id="coding-pipeline-v1",
            base_branch="develop",
            issue_number=42,
            output_dir=str(out_dir),
        )

        assert gate["run_id"] == "run-full"
        assert gate["branch"] == "fix/issue-42"
        assert gate["base_branch"] == "develop"
        assert gate["pipeline_id"] == "coding-pipeline-v1"
        assert gate["issue_number"] == 42
        assert gate["repo_path"] == "/home/user/myrepo"
        assert gate["output_dir"] == str(out_dir)

        # Central registry file
        registry_file = GitContext.GATES_DIR / "run-full.json"
        assert registry_file.exists()
        on_disk = json.loads(registry_file.read_text())
        assert on_disk["issue_number"] == 42

        # Output-dir copy
        out_gate = out_dir / "_gate.json"
        assert out_gate.exists()
        out_data = json.loads(out_gate.read_text())
        assert out_data["branch"] == "fix/issue-42"
    finally:
        GitContext.GATES_DIR = original_gates_dir


def test_create_gate_no_output_dir(tmp_path: Path) -> None:
    """create_gate without output_dir does not create an output-dir _gate.json."""
    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    try:
        GitContext.create_gate(run_id="run-noop", branch_name="feat/no-out")

        gate_file = GitContext.GATES_DIR / "run-noop.json"
        assert gate_file.exists()

        # No spurious _gate.json elsewhere
        spurious = tmp_path / "_gate.json"
        assert not spurious.exists()
    finally:
        GitContext.GATES_DIR = original_gates_dir


def test_create_gate_then_load_gate(tmp_path: Path) -> None:
    """load_gate can read back what create_gate wrote."""
    original_gates_dir = GitContext.GATES_DIR
    GitContext.GATES_DIR = tmp_path / "gates"
    try:
        GitContext.create_gate(
            run_id="run-roundtrip",
            branch_name="feat/roundtrip",
            issue_number=99,
        )

        loaded = GitContext.load_gate("run-roundtrip")
        assert loaded is not None
        assert loaded["run_id"] == "run-roundtrip"
        assert loaded["branch"] == "feat/roundtrip"
        assert loaded["issue_number"] == 99
        assert loaded["status"] == "awaiting_approval"
    finally:
        GitContext.GATES_DIR = original_gates_dir


# ===========================================================================
# Tests for GitContext.auto_merge_pr (Issue #350)
# ===========================================================================


class TestAutoMergePr:
    """Unit tests for GitContext.auto_merge_pr with mocked subprocess."""

    def test_success_squash(self, tmp_path):
        """Successful squash merge calls gh with correct args and does not raise."""
        import subprocess
        from orchestration_engine.git_integration import GitContext

        mock_result = subprocess.CompletedProcess(
            args=["gh", "pr", "merge", "--squash", "--delete-branch", "feat/x"],
            returncode=0,
            stdout="✓ Merged pull request #42",
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitContext.auto_merge_pr(
                run_id="run-001",
                branch_name="feat/x",
                strategy="squash",
                working_dir=tmp_path,
            )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "--squash" in cmd
        assert "--delete-branch" in cmd
        assert "feat/x" in cmd

    def test_success_merge_strategy(self, tmp_path):
        """--merge strategy flag passed correctly."""
        import subprocess
        from orchestration_engine.git_integration import GitContext

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="merged", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitContext.auto_merge_pr("run-002", "feat/y", strategy="merge", working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--merge" in cmd

    def test_success_rebase_strategy(self, tmp_path):
        """--rebase strategy flag passed correctly."""
        import subprocess
        from orchestration_engine.git_integration import GitContext

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="rebased", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            GitContext.auto_merge_pr("run-003", "feat/z", strategy="rebase", working_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "--rebase" in cmd

    def test_non_zero_exit_raises_git_error(self, tmp_path):
        """Non-zero exit code from gh must raise GitError."""
        import subprocess
        from orchestration_engine.git_integration import GitContext, GitError

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no open PRs found"
        )
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(GitError, match="gh pr merge failed"):
                GitContext.auto_merge_pr("run-fail", "feat/bad", working_dir=tmp_path)

    def test_file_not_found_logs_warning_no_raise(self, tmp_path):
        """Missing gh CLI logs a warning and does NOT raise (non-fatal)."""
        from orchestration_engine.git_integration import GitContext
        import logging

        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            # Should not raise
            GitContext.auto_merge_pr("run-ngh", "feat/ngh", working_dir=tmp_path)

    def test_timeout_logs_warning_no_raise(self, tmp_path):
        """subprocess.TimeoutExpired logs warning and does NOT raise (non-fatal)."""
        import subprocess
        from orchestration_engine.git_integration import GitContext

        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=60)):
            # Should not raise
            GitContext.auto_merge_pr("run-timeout", "feat/slow", working_dir=tmp_path)
