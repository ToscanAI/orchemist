"""Unit tests for Issue #706: Folder Guard (protected_paths).

Tests cover:
- compute_directory_hash determinism
- Symlink skip (target modification does not change hash)
- Exclude patterns (__pycache__, .pyc, .pytest_cache, .git)
- Sequencer _snapshot_protected_paths / _verify_protected_paths flow
- Path resolution fallback (repo_path → working_dir → skip)
- __pycache__ false-positive prevention
- Re-snapshot per iteration (each iteration clears the snapshot)
- _handle_file_write interaction (verify after file write)
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orchestration_engine.file_guard import compute_directory_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_dir(base: Path, files: dict) -> None:
    """Create a directory tree from {rel_path: content} dict."""
    for rel, content in files.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)


# ---------------------------------------------------------------------------
# 1. compute_directory_hash — determinism
# ---------------------------------------------------------------------------

class TestComputeDirectoryHashDeterminism:

    def test_same_contents_same_hash(self, tmp_path):
        d = tmp_path / "repo"
        make_dir(d, {
            "a.py": "alpha",
            "b.py": "beta",
            "sub/c.py": "gamma",
        })
        h1 = compute_directory_hash(str(d))
        h2 = compute_directory_hash(str(d))
        assert h1 == h2

    def test_different_content_different_hash(self, tmp_path):
        d = tmp_path / "repo"
        make_dir(d, {"a.py": "original"})
        h1 = compute_directory_hash(str(d))
        (d / "a.py").write_text("modified")
        h2 = compute_directory_hash(str(d))
        assert h1 != h2

    def test_added_file_changes_hash(self, tmp_path):
        d = tmp_path / "repo"
        make_dir(d, {"a.py": "content"})
        h1 = compute_directory_hash(str(d))
        (d / "b.py").write_text("new file")
        h2 = compute_directory_hash(str(d))
        assert h1 != h2

    def test_deleted_file_changes_hash(self, tmp_path):
        d = tmp_path / "repo"
        make_dir(d, {"a.py": "keep", "b.py": "delete"})
        h1 = compute_directory_hash(str(d))
        (d / "b.py").unlink()
        h2 = compute_directory_hash(str(d))
        assert h1 != h2

    def test_lexicographic_sort_order(self, tmp_path):
        """Files created in different orders must produce the same hash."""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        files = {"z.py": "z", "a.py": "a", "m.py": "m"}
        make_dir(d1, files)
        for rel, content in reversed(list(files.items())):
            (d2 / rel).parent.mkdir(parents=True, exist_ok=True)
            (d2 / rel).write_text(content)
        assert compute_directory_hash(str(d1)) == compute_directory_hash(str(d2))

    def test_returns_64_char_hex_string(self, tmp_path):
        d = tmp_path / "repo"
        make_dir(d, {"f.py": "x"})
        h = compute_directory_hash(str(d))
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdefABCDEF" for c in h)


# ---------------------------------------------------------------------------
# 2. Symlink handling
# ---------------------------------------------------------------------------

class TestComputeDirectoryHashSymlinks:

    def test_symlink_target_modification_does_not_change_hash(self, tmp_path):
        """Modifying the FILE a symlink points to must NOT change the hash.
        The hash covers the link target path string, not linked file content."""
        outside = tmp_path / "outside.txt"
        outside.write_text("original")
        guarded = tmp_path / "guarded"
        guarded.mkdir()
        link = guarded / "link.py"
        link.symlink_to(outside)

        h_before = compute_directory_hash(str(guarded))
        outside.write_text("modified — but hash should not change")
        h_after = compute_directory_hash(str(guarded))

        assert h_before == h_after, (
            "Modifying the target of a symlink outside the guarded dir "
            "must NOT change the hash (link target path string unchanged)"
        )

    def test_symlink_retargeting_changes_hash(self, tmp_path):
        """Changing what a symlink points to (retargeting) MUST change the hash."""
        safe = tmp_path / "safe.py"
        safe.write_text("safe")
        other = tmp_path / "other.py"
        other.write_text("other")

        guarded = tmp_path / "guarded"
        guarded.mkdir()
        link = guarded / "sym.py"
        link.symlink_to(safe)

        h_before = compute_directory_hash(str(guarded))
        link.unlink()
        link.symlink_to(other)
        h_after = compute_directory_hash(str(guarded))

        assert h_before != h_after, (
            "Retargeting a symlink (changing the link target path) must change the hash"
        )


# ---------------------------------------------------------------------------
# 3. Exclude patterns
# ---------------------------------------------------------------------------

class TestComputeDirectoryHashExcludes:

    def test_pycache_excluded_by_default(self, tmp_path):
        d = tmp_path / "tests"
        make_dir(d, {"real.py": "content"})
        h_before = compute_directory_hash(str(d))

        # Add __pycache__ and .pyc — must be ignored
        cache = d / "__pycache__"
        cache.mkdir()
        (cache / "real.cpython-311.pyc").write_bytes(b"\x00bytecode")
        (d / "real.pyc").write_bytes(b"\x00bytecode")

        h_after = compute_directory_hash(str(d))
        assert h_before == h_after, "__pycache__ dirs and .pyc files must be excluded"

    def test_pytest_cache_excluded_by_default(self, tmp_path):
        d = tmp_path / "tests"
        make_dir(d, {"real.py": "content"})
        h_before = compute_directory_hash(str(d))

        pc = d / ".pytest_cache"
        pc.mkdir()
        (pc / "v").mkdir()
        (pc / "v" / "cache").write_text("{}")

        h_after = compute_directory_hash(str(d))
        assert h_before == h_after, ".pytest_cache must be excluded"

    def test_git_excluded_by_default(self, tmp_path):
        d = tmp_path / "tests"
        make_dir(d, {"real.py": "content"})
        h_before = compute_directory_hash(str(d))

        git = d / ".git"
        git.mkdir()
        (git / "COMMIT_EDITMSG").write_text("initial commit")

        h_after = compute_directory_hash(str(d))
        assert h_before == h_after, ".git must be excluded"

    def test_pycache_false_positive_prevention(self, tmp_path):
        """Running tests generates __pycache__ — this must not cause false positives
        on every Python project. Verify that adding __pycache__ between snapshot
        and verify does not trigger PROTECTED_PATH_MODIFIED."""
        d = tmp_path / "tests"
        make_dir(d, {"test_foo.py": "def test_foo(): pass"})

        h1 = compute_directory_hash(str(d))

        # Simulate pytest generating __pycache__ during test execution
        cache = d / "__pycache__"
        cache.mkdir()
        (cache / "test_foo.cpython-311.pyc").write_bytes(b"\x00\x01\x02pytest bytecode")

        h2 = compute_directory_hash(str(d))
        assert h1 == h2, (
            "Adding __pycache__ after snapshot must not change the hash — "
            "this would cause false positives on every Python project"
        )

    def test_nonexistent_path_raises_file_not_found(self, tmp_path):
        """compute_directory_hash raises FileNotFoundError for missing paths —
        the CALLER (sequencer) handles graceful degradation, not this function."""
        with pytest.raises(FileNotFoundError):
            compute_directory_hash("/tmp/this_does_not_exist_706_xyz_abc_123")


# ---------------------------------------------------------------------------
# 4. Sequencer _snapshot_protected_paths / _verify_protected_paths flow
# ---------------------------------------------------------------------------

def _make_sequencer(config=None, working_dir=None, output_dir=None):
    """Build a minimal PhaseSequencer stub with mocked internals."""
    from orchestration_engine.sequencer import PhaseSequencer
    from orchestration_engine.templates import PhaseDefinition, PipelineTemplate

    template = PipelineTemplate(id="test-pipeline", name="Test Pipeline")
    runner = MagicMock()
    runner.queue = MagicMock()
    runner.executors = []

    seq = PhaseSequencer.__new__(PhaseSequencer)
    seq.template = template
    seq.runner = runner
    seq.config = config or {}
    seq.output_dir = output_dir
    seq.phase_outputs = {}
    seq.pipeline_context = {}
    seq.on_phase_complete = None
    seq.on_phase_start = None
    seq.on_pipeline_start = None
    seq.on_pipeline_complete = None
    seq.run_id = None
    seq.db = None
    seq._phase_map = {}
    seq._protected_hashes = {}
    seq._protected_path_snapshots = {}

    import threading
    seq._phase_outputs_lock = threading.Lock()
    seq._callback_lock = threading.Lock()

    if working_dir is not None:
        seq.working_dir = working_dir

    return seq


def _make_phase(protected_paths=None):
    from orchestration_engine.templates import PhaseDefinition
    phase = PhaseDefinition.__new__(PhaseDefinition)
    phase.id = "test-phase"
    phase.name = "Test Phase"
    phase.protected_paths = protected_paths or []
    phase.protected_outputs = []
    phase.write_files = False
    return phase


class TestSequencerProtectedPathsFlow:

    def test_snapshot_and_verify_no_change(self, tmp_path):
        """When nothing changes, _verify_protected_paths returns None."""
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "original"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        assert len(seq._protected_path_snapshots) == 1

        result = seq._verify_protected_paths(phase)
        assert result is None

    def test_snapshot_and_verify_with_modification(self, tmp_path):
        """When a file changes, _verify_protected_paths returns FAILED dict."""
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "original"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        # Simulate agent modifying a file
        (d / "a.py").write_text("UNAUTHORIZED MODIFICATION")

        result = seq._verify_protected_paths(phase)
        assert result is not None
        assert result["error_code"] == "PROTECTED_PATH_MODIFIED"
        assert "protected_path" in result
        assert "expected_hash" in result
        assert "actual_hash" in result
        assert result["expected_hash"] != result["actual_hash"]
        assert len(result["expected_hash"]) == 64
        assert len(result["actual_hash"]) == 64

    def test_no_protected_paths_zero_overhead(self, tmp_path):
        """With no protected_paths, snapshot and verify are no-ops."""
        seq = _make_sequencer()
        phase = _make_phase(protected_paths=[])

        seq._snapshot_protected_paths(phase)
        assert seq._protected_path_snapshots == {}

        result = seq._verify_protected_paths(phase)
        assert result is None

    def test_failed_result_contains_required_keys(self, tmp_path):
        """PROTECTED_PATH_MODIFIED result must include path, expected_hash, actual_hash."""
        d = tmp_path / "tests"
        make_dir(d, {"core.py": "original content"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        (d / "core.py").write_text("modified content")

        result = seq._verify_protected_paths(phase)
        assert result is not None

        # Verify all required keys per behavioral contract
        assert result.get("error_code") == "PROTECTED_PATH_MODIFIED"
        assert "protected_path" in result
        assert "expected_hash" in result
        assert "actual_hash" in result
        assert result["expected_hash"] != result["actual_hash"]


# ---------------------------------------------------------------------------
# 5. Path resolution fallback
# ---------------------------------------------------------------------------

class TestPathResolutionFallback:

    def test_absolute_path_used_directly(self, tmp_path):
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "x"})

        seq = _make_sequencer(config={})  # no repo_path
        phase = _make_phase(protected_paths=[str(d)])  # absolute

        seq._snapshot_protected_paths(phase)
        assert str(d) in seq._protected_path_snapshots

    def test_relative_resolves_against_repo_path(self, tmp_path):
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "x"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        expected_key = str(tmp_path / "tests")
        assert expected_key in seq._protected_path_snapshots

    def test_relative_falls_back_to_working_dir(self, tmp_path):
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "x"})

        seq = _make_sequencer(config={}, working_dir=str(tmp_path))
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        expected_key = str(tmp_path / "tests")
        assert expected_key in seq._protected_path_snapshots

    def test_repo_path_takes_priority_over_working_dir(self, tmp_path):
        repo = tmp_path / "repo"
        wd = tmp_path / "wd"
        (repo / "tests").mkdir(parents=True)
        make_dir(repo / "tests", {"a.py": "repo content"})
        (wd / "tests").mkdir(parents=True)
        make_dir(wd / "tests", {"a.py": "wd content"})

        seq = _make_sequencer(
            config={"repo_path": str(repo)},
            working_dir=str(wd),
        )
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        # Should resolve against repo_path, not working_dir
        assert str(repo / "tests") in seq._protected_path_snapshots
        assert str(wd / "tests") not in seq._protected_path_snapshots

    def test_graceful_skip_when_both_absent(self, tmp_path):
        """When both repo_path and working_dir are absent, path is skipped gracefully."""
        seq = _make_sequencer(config={})  # no repo_path, no working_dir
        phase = _make_phase(protected_paths=["tests"])

        # Should not raise — just log warning and skip
        seq._snapshot_protected_paths(phase)
        assert seq._protected_path_snapshots == {}

    def test_nonexistent_path_skipped_gracefully(self, tmp_path):
        """A protected_path that doesn't exist at snapshot time is skipped, not failed."""
        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["nonexistent_dir_xyz"])

        seq._snapshot_protected_paths(phase)
        # Should not raise, and snapshot dict should be empty
        assert seq._protected_path_snapshots == {}

        # verify should return None (nothing to verify)
        result = seq._verify_protected_paths(phase)
        assert result is None

    def test_output_dir_not_used_for_resolution(self, tmp_path):
        """output_dir must NOT be used for resolving relative protected_paths."""
        output = tmp_path / "output"
        make_dir(output / "tests", {"a.py": "output content"})

        # No repo_path, no working_dir — only output_dir
        seq = _make_sequencer(config={}, output_dir=str(output))
        phase = _make_phase(protected_paths=["tests"])

        seq._snapshot_protected_paths(phase)
        # output_dir/tests should NOT be resolved
        assert str(output / "tests") not in seq._protected_path_snapshots
        # snapshot should be empty (path skipped gracefully)
        assert seq._protected_path_snapshots == {}


# ---------------------------------------------------------------------------
# 6. Re-snapshot per iteration
# ---------------------------------------------------------------------------

class TestReSnapshotPerIteration:

    def test_snapshot_cleared_between_iterations(self, tmp_path):
        """Each call to _snapshot_protected_paths with a cleared dict produces
        a fresh snapshot — simulating the per-iteration re-snapshot behavior."""
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "iteration 1 state"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        # === Iteration 1 ===
        seq._protected_path_snapshots = {}  # reset (as sequencer does before each iteration)
        seq._snapshot_protected_paths(phase)
        snapshot_iter1 = dict(seq._protected_path_snapshots)

        # Simulate no change during iteration 1
        result1 = seq._verify_protected_paths(phase)
        assert result1 is None, "Iteration 1 should pass (no change)"

        # === Legitimate change between iterations ===
        (d / "a.py").write_text("iteration 2 legitimate state")

        # === Iteration 2 — re-snapshot BEFORE executing ===
        seq._protected_path_snapshots = {}  # reset
        seq._snapshot_protected_paths(phase)
        snapshot_iter2 = dict(seq._protected_path_snapshots)

        # The two snapshots should differ (file was legitimately changed)
        assert snapshot_iter1 != snapshot_iter2, (
            "Re-snapshot in iteration 2 must reflect the new state, not iteration-1 state"
        )

        # No unauthorized change during iteration 2
        result2 = seq._verify_protected_paths(phase)
        assert result2 is None, (
            "Iteration 2 should pass when verified against the iteration-2 snapshot — "
            "re-snapshot prevents false PROTECTED_PATH_MODIFIED for legitimate changes"
        )

    def test_snapshot_before_each_iteration_catches_unauthorized_change(self, tmp_path):
        """Each iteration re-snapshots; if a change occurs WITHIN an iteration, it's caught."""
        d = tmp_path / "tests"
        make_dir(d, {"a.py": "original"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        # Iteration 2 snapshot (after legitimate inter-iteration change)
        seq._protected_path_snapshots = {}
        seq._snapshot_protected_paths(phase)

        # Now simulate an UNAUTHORIZED change during iteration 2 execution
        (d / "a.py").write_text("UNAUTHORIZED during iteration 2")

        result = seq._verify_protected_paths(phase)
        assert result is not None
        assert result["error_code"] == "PROTECTED_PATH_MODIFIED"


# ---------------------------------------------------------------------------
# 7. _handle_file_write interaction
# ---------------------------------------------------------------------------

class TestHandleFileWriteInteraction:

    def test_verify_after_file_write_detects_write_to_protected_path(self, tmp_path):
        """The verify step catches FILE-block writes targeting protected paths.
        This simulates the verification that occurs AFTER _handle_file_write."""
        d = tmp_path / "tests"
        make_dir(d, {"core.py": "original content"})

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        # Take snapshot before "execution"
        seq._protected_path_snapshots = {}
        seq._snapshot_protected_paths(phase)

        # Simulate _handle_file_write writing to a protected path
        (d / "core.py").write_text("content written via FILE block")

        # Verify AFTER file write (mirrors sequencer's post-file-write position)
        result = seq._verify_protected_paths(phase)
        assert result is not None, (
            "FILE-block write to a protected path must be detected by "
            "verify_protected_paths called after _handle_file_write"
        )
        assert result["error_code"] == "PROTECTED_PATH_MODIFIED"

    def test_verify_after_file_write_no_false_positive(self, tmp_path):
        """When FILE-block writes go to NON-protected paths, no false positive."""
        tests_dir = tmp_path / "tests"
        output_dir = tmp_path / "output"
        make_dir(tests_dir, {"core.py": "original"})
        output_dir.mkdir()

        seq = _make_sequencer(config={"repo_path": str(tmp_path)})
        phase = _make_phase(protected_paths=["tests"])

        seq._protected_path_snapshots = {}
        seq._snapshot_protected_paths(phase)

        # Simulate FILE-block write to output_dir (not protected)
        (output_dir / "result.md").write_text("pipeline output")

        result = seq._verify_protected_paths(phase)
        assert result is None, (
            "FILE-block write to a non-protected path must not trigger PROTECTED_PATH_MODIFIED"
        )
