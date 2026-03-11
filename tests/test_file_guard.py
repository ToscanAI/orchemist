"""Unit tests for the file_guard module (Issue #531).

Covers compute_hash(), verify_hash(), and FileGuardError.
"""
import pytest
from orchestration_engine.file_guard import compute_hash, verify_hash, FileGuardError


class TestComputeHash:
    """Unit tests for compute_hash()."""

    def test_compute_hash_consistent(self, tmp_path):
        """Same file content → same hash on repeated calls."""
        f = tmp_path / "stable.txt"
        f.write_bytes(b"deterministic content")
        assert compute_hash(f) == compute_hash(f)

    def test_compute_hash_changes_on_modification(self, tmp_path):
        """Hash changes when file content changes."""
        f = tmp_path / "mutable.py"
        f.write_bytes(b"original content")
        h1 = compute_hash(f)
        f.write_bytes(b"tampered content")
        h2 = compute_hash(f)
        assert h1 != h2

    def test_compute_hash_file_not_found(self, tmp_path):
        """Raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            compute_hash(tmp_path / "does_not_exist.py")

    def test_verify_hash_match(self, tmp_path):
        """No exception raised when hash matches."""
        f = tmp_path / "clean.py"
        f.write_bytes(b"clean content")
        verify_hash(f, compute_hash(f))  # should not raise

    def test_verify_hash_mismatch(self, tmp_path):
        """Raises FileGuardError with correct message format."""
        f = tmp_path / "acceptance_tests.py"
        f.write_bytes(b"original tests")
        original_hash = compute_hash(f)
        f.write_bytes(b"modified tests")
        with pytest.raises(FileGuardError) as exc_info:
            verify_hash(f, original_hash)
        msg = str(exc_info.value)
        assert "acceptance_tests.py" in msg
        assert "expected sha256:" in msg
        assert "got sha256:" in msg
        assert "Protected file modified:" in msg

    def test_verify_hash_file_deleted(self, tmp_path):
        """Raises FileNotFoundError when file is gone."""
        f = tmp_path / "gone.py"
        f.write_bytes(b"content")
        h = compute_hash(f)
        f.unlink()
        with pytest.raises(FileNotFoundError):
            verify_hash(f, h)

    def test_verify_hash_file_not_found(self, tmp_path):
        """Raises FileNotFoundError for never-existed path."""
        with pytest.raises(FileNotFoundError):
            verify_hash(tmp_path / "never_existed.py", "a" * 64)

    def test_compute_hash_empty_file(self, tmp_path):
        """Handles zero-byte file (returns valid SHA256)."""
        import hashlib
        f = tmp_path / "empty.py"
        f.write_bytes(b"")
        assert compute_hash(f) == hashlib.sha256(b"").hexdigest()

    def test_compute_hash_large_file(self, tmp_path):
        """Chunked reading works (creates file > 65536 bytes)."""
        import hashlib
        content = b"x" * (65536 * 2 + 1)
        f = tmp_path / "large.bin"
        f.write_bytes(content)
        assert compute_hash(f) == hashlib.sha256(content).hexdigest()
