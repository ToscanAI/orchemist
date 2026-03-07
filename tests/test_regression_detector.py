"""Tests for RegressionDetector and the two new GitContext helper methods.

Issue: #3.3a.2 — Breaking Commit Identification

Coverage:
- GitContext.get_commit_range: normal, empty, git failure, cap at 50
- GitContext.get_commit_files: normal, git failure
- RegressionDetector.detect: normal flow, no commits in range
- RegressionDetector._score_commit: overlap match, no overlap, empty inputs
- RegressionDetector._find_best_commit: selects highest score, tie-breaks newest
- Integration: Regression record persisted to in-memory DB with correct fields
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch, call

import pytest

from orchestration_engine.db import Database
from orchestration_engine.git_integration import GitConfig, GitContext
from orchestration_engine.regression import (
    Regression,
    RegressionDetector,
    RegressionStatus,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a mock subprocess.CompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


@pytest.fixture
def db():
    return Database(db_path=Path(":memory:"))


@pytest.fixture
def git_ctx():
    """A GitContext wired with a dummy config (git not actually enabled)."""
    cfg = GitConfig(enabled=False)
    return GitContext(config=cfg, pipeline_id="test-pipe", run_id="run-001")


@pytest.fixture
def detector(db, git_ctx):
    return RegressionDetector(db=db, git_context=git_ctx)


# ---------------------------------------------------------------------------
# TestGetCommitRange
# ---------------------------------------------------------------------------


class TestGetCommitRange:
    """Tests for GitContext.get_commit_range."""

    def test_returns_shas_from_git_log(self, git_ctx, tmp_path):
        """Normal path: parse SHAs from oneline git log output."""
        fake_output = (
            "abc1234 fix: handle null pointer\n"
            "def5678 feat: add widget\n"
            "ghi9012 chore: update deps\n"
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=fake_output)) as mock_run:
            result = git_ctx.get_commit_range("oldhash", "newhash", tmp_path)

        assert result == ["abc1234", "def5678", "ghi9012"]
        mock_run.assert_called_once_with(
            ["git", "log", "--oneline", "oldhash..newhash"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_empty_range_returns_empty_list(self, git_ctx, tmp_path):
        """When git log produces no output, return []."""
        with patch("subprocess.run", return_value=_make_completed(stdout="")):
            result = git_ctx.get_commit_range("a", "b", tmp_path)
        assert result == []

    def test_git_failure_returns_empty_list(self, git_ctx, tmp_path):
        """Non-zero returncode → return [] without raising."""
        with patch(
            "subprocess.run",
            return_value=_make_completed(stderr="bad object", returncode=128),
        ):
            result = git_ctx.get_commit_range("bad", "sha", tmp_path)
        assert result == []

    def test_timeout_returns_empty_list(self, git_ctx, tmp_path):
        """TimeoutExpired → return [] without raising."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
            result = git_ctx.get_commit_range("a", "b", tmp_path)
        assert result == []

    def test_file_not_found_returns_empty_list(self, git_ctx, tmp_path):
        """FileNotFoundError (git not installed) → return []."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = git_ctx.get_commit_range("a", "b", tmp_path)
        assert result == []

    def test_capped_at_50(self, git_ctx, tmp_path):
        """More than 50 commits in range → return first 50 only."""
        lines = "\n".join(f"sha{i:04d} commit {i}" for i in range(60))
        with patch("subprocess.run", return_value=_make_completed(stdout=lines)):
            result = git_ctx.get_commit_range("a", "b", tmp_path)
        assert len(result) == 50
        assert result[0] == "sha0000"
        assert result[49] == "sha0049"

    def test_whitespace_lines_are_ignored(self, git_ctx, tmp_path):
        """Blank lines in git log output are filtered out."""
        fake_output = "\nabc1234 msg\n\ndef5678 msg2\n\n"
        with patch("subprocess.run", return_value=_make_completed(stdout=fake_output)):
            result = git_ctx.get_commit_range("a", "b", tmp_path)
        assert result == ["abc1234", "def5678"]


# ---------------------------------------------------------------------------
# TestGetCommitFiles
# ---------------------------------------------------------------------------


class TestGetCommitFiles:
    """Tests for GitContext.get_commit_files."""

    def test_returns_file_paths(self, git_ctx, tmp_path):
        """Normal path: parse file names from git show output."""
        fake_output = (
            "src/orchestration_engine/scorer.py\n"
            "tests/test_scorer.py\n"
            " 2 files changed, 42 insertions(+), 5 deletions(-)\n"
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=fake_output)):
            result = git_ctx.get_commit_files("abc1234", tmp_path)

        assert "src/orchestration_engine/scorer.py" in result
        assert "tests/test_scorer.py" in result

    def test_stat_summary_lines_filtered_out(self, git_ctx, tmp_path):
        """Stat summary line ('N files changed …') is not included in result."""
        fake_output = (
            "README.md\n"
            "setup.py\n"
            " 2 files changed, 10 insertions(+)\n"
        )
        with patch("subprocess.run", return_value=_make_completed(stdout=fake_output)):
            result = git_ctx.get_commit_files("abc1234", tmp_path)
        assert "2 files changed, 10 insertions(+)" not in result
        assert "README.md" in result
        assert "setup.py" in result

    def test_git_failure_returns_empty_list(self, git_ctx, tmp_path):
        """Non-zero returncode → return []."""
        with patch(
            "subprocess.run",
            return_value=_make_completed(stderr="bad object abc", returncode=128),
        ):
            result = git_ctx.get_commit_files("bad_sha", tmp_path)
        assert result == []

    def test_timeout_returns_empty_list(self, git_ctx, tmp_path):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
            result = git_ctx.get_commit_files("sha", tmp_path)
        assert result == []

    def test_correct_git_command(self, git_ctx, tmp_path):
        """Verify the exact git command used."""
        with patch("subprocess.run", return_value=_make_completed(stdout="file.py\n")) as mock_run:
            git_ctx.get_commit_files("deadbeef", tmp_path)
        mock_run.assert_called_once_with(
            ["git", "show", "--stat", "--name-only", "--format=", "deadbeef"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )


# ---------------------------------------------------------------------------
# TestScoreCommit
# ---------------------------------------------------------------------------


class TestScoreCommit:
    """Tests for RegressionDetector._score_commit (static method)."""

    def test_matching_file_scores_one(self):
        files = ["src/orchestration_engine/scorer.py"]
        log = "ERROR in src/orchestration_engine/scorer.py line 42: assertion failed"
        score = RegressionDetector._score_commit(files, log)
        assert score == 1

    def test_no_matching_files_scores_zero(self):
        files = ["src/foo/bar.py"]
        log = "ERROR in tests/test_other.py line 5"
        score = RegressionDetector._score_commit(files, log)
        assert score == 0

    def test_multiple_matching_files(self):
        files = ["src/a.py", "src/b.py", "src/c.py"]
        log = "FAIL src/a.py:10  src/b.py:20  unrelated.go:5"
        score = RegressionDetector._score_commit(files, log)
        assert score == 2

    def test_empty_files_scores_zero(self):
        assert RegressionDetector._score_commit([], "some error log") == 0

    def test_empty_log_scores_zero(self):
        assert RegressionDetector._score_commit(["foo.py"], "") == 0

    def test_basename_match(self):
        """Match by basename even when full path differs."""
        files = ["deep/nested/path/my_module.py"]
        log = "ImportError: cannot import name 'x' from my_module.py"
        score = RegressionDetector._score_commit(files, log)
        assert score == 1

    def test_each_file_counted_once(self):
        """A file that appears multiple times in the log still counts as 1."""
        files = ["scorer.py"]
        log = "scorer.py scorer.py scorer.py"
        score = RegressionDetector._score_commit(files, log)
        assert score == 1


# ---------------------------------------------------------------------------
# TestFindBestCommit
# ---------------------------------------------------------------------------


class TestFindBestCommit:
    """Tests for RegressionDetector._find_best_commit."""

    def test_selects_highest_scoring_commit(self, detector, tmp_path):
        """The commit with the most file overlap wins."""
        shas = ["sha1", "sha2", "sha3"]

        def fake_get_files(sha, path):
            return {
                "sha1": ["unrelated.py"],
                "sha2": ["scorer.py", "pipeline.py"],
                "sha3": ["other.py"],
            }[sha]

        detector._git.get_commit_files = fake_get_files
        ci_log = "FAIL: scorer.py:10 pipeline.py:20"

        best_sha, best_files, best_score = detector._find_best_commit(shas, ci_log, tmp_path)

        assert best_sha == "sha2"
        assert best_score == 2
        assert "scorer.py" in best_files

    def test_tie_breaks_to_newest(self, detector, tmp_path):
        """When two commits share the same score, the newest (first in list) wins."""
        shas = ["sha_new", "sha_old"]

        detector._git.get_commit_files = lambda sha, path: ["scorer.py"]
        ci_log = "error in scorer.py"

        best_sha, _, _ = detector._find_best_commit(shas, ci_log, tmp_path)
        assert best_sha == "sha_new"

    def test_single_commit(self, detector, tmp_path):
        """Single-commit range always returns that commit."""
        detector._git.get_commit_files = lambda sha, path: ["any.py"]
        best_sha, best_files, _ = detector._find_best_commit(
            ["only_sha"], "error in any.py", tmp_path
        )
        assert best_sha == "only_sha"
        assert best_files == ["any.py"]


# ---------------------------------------------------------------------------
# TestRegressionDetectorDetect
# ---------------------------------------------------------------------------


class TestRegressionDetectorDetect:
    """Integration tests for RegressionDetector.detect."""

    def test_detect_normal_flow_persists_regression(self, detector, db, tmp_path):
        """detect() should persist a Regression record and return it."""
        detector._git.get_commit_range = lambda lg, h, p: ["abc1234", "def5678"]
        detector._git.get_commit_files = lambda sha, p: (
            ["scorer.py"] if sha == "abc1234" else ["unrelated.py"]
        )

        ci_log = "FAIL: scorer.py line 42 AssertionError"
        result = detector.detect(
            last_green_sha="oldsha",
            head_sha="newsha",
            ci_error_log=ci_log,
            ci_run_url="https://github.com/org/repo/actions/runs/999",
            failure_type="test_failure",
            repo_path=tmp_path,
        )

        assert result is not None
        assert isinstance(result, Regression)
        assert result.commit_sha == "abc1234"
        assert result.status == RegressionStatus.DETECTED
        assert result.ci_run_url == "https://github.com/org/repo/actions/runs/999"
        assert result.failure_type == "test_failure"

        # Verify it was persisted to the DB
        row = db.get_regression(result.id)
        assert row is not None
        assert row["commit_sha"] == "abc1234"
        assert row["status"] == "detected"
        assert row["failure_type"] == "test_failure"

    def test_detect_no_commits_returns_none(self, detector, tmp_path):
        """When commit range is empty, detect() returns None."""
        detector._git.get_commit_range = lambda lg, h, p: []

        result = detector.detect(
            last_green_sha="a",
            head_sha="b",
            ci_error_log="some error",
            ci_run_url="https://example.com/ci",
            failure_type="test_failure",
            repo_path=tmp_path,
        )
        assert result is None

    def test_detect_persists_affected_files(self, detector, db, tmp_path):
        """Affected files from the winning commit are stored in the DB."""
        detector._git.get_commit_range = lambda lg, h, p: ["sha1"]
        detector._git.get_commit_files = lambda sha, p: ["src/foo.py", "src/bar.py"]

        result = detector.detect(
            last_green_sha="a",
            head_sha="b",
            ci_error_log="error in src/foo.py",
            ci_run_url="https://example.com/ci/1",
            failure_type="build_error",
            repo_path=tmp_path,
        )
        assert result is not None
        row = db.get_regression(result.id)
        import json
        stored_files = json.loads(row["affected_files"]) if isinstance(row["affected_files"], str) else row["affected_files"]
        assert "src/foo.py" in stored_files

    def test_detect_assigns_detected_status(self, detector, db, tmp_path):
        """Persisted record always starts in DETECTED status."""
        detector._git.get_commit_range = lambda lg, h, p: ["sha1"]
        detector._git.get_commit_files = lambda sha, p: []

        result = detector.detect(
            last_green_sha="a",
            head_sha="b",
            ci_error_log="",
            ci_run_url="https://example.com/ci/2",
            failure_type="lint_error",
            repo_path=tmp_path,
        )
        assert result.status == RegressionStatus.DETECTED
        row = db.get_regression(result.id)
        assert row["status"] == "detected"

    def test_detect_multiple_commits_picks_best(self, detector, db, tmp_path):
        """With multiple commits, the one with highest file overlap wins."""
        detector._git.get_commit_range = lambda lg, h, p: [
            "commit_a", "commit_b", "commit_c"
        ]

        def fake_files(sha, path):
            return {
                "commit_a": ["docs/readme.md"],
                "commit_b": ["src/engine.py", "src/runner.py"],
                "commit_c": ["tests/test_engine.py"],
            }[sha]

        detector._git.get_commit_files = fake_files
        ci_log = "FAIL tests/test_engine.py src/engine.py src/runner.py"

        result = detector.detect(
            last_green_sha="old",
            head_sha="new",
            ci_error_log=ci_log,
            ci_run_url="https://example.com/ci/3",
            failure_type="test_failure",
            repo_path=tmp_path,
        )
        # commit_b touches 2 files mentioned in the log → highest score
        assert result.commit_sha == "commit_b"
