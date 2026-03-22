"""Unit tests for the immutable acceptance-test store (Issue #541).

Covers all behavioral contracts defined in the spec and tested by the
acceptance tests, plus edge cases specific to the internal implementation.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestration_engine.test_store import (
    SUPPORTED_MANIFEST_VERSIONS,
    TestManifest,
    TestStore,
    TestStoreError,
    _get_store_root,
    _validate_run_id,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """A TestStore backed by a temporary directory."""
    return TestStore(store_root=tmp_path / "test_store")


@pytest.fixture
def sample_test_file(tmp_path):
    """A minimal acceptance_tests.py source file."""
    p = tmp_path / "acceptance_tests.py"
    p.write_text("# sample\nimport pytest\ndef test_x(): pass\n")
    return p


@pytest.fixture(autouse=True)
def restore_permissions(tmp_path):
    """Restore write permissions so pytest can clean up sealed (read-only) files."""
    yield
    for p in tmp_path.rglob("*"):
        try:
            os.chmod(p, 0o755)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TestSealTests
# ---------------------------------------------------------------------------


class TestSealTests:
    def test_happy_path_returns_manifest(self, store, sample_test_file):
        meta = store.seal_tests("run-001", sample_test_file, spec_hash="abc123")
        assert isinstance(meta, TestManifest)

    def test_returns_correct_run_id(self, store, sample_test_file):
        meta = store.seal_tests("run-001", sample_test_file, spec_hash="abc123")
        assert meta.run_id == "run-001"

    def test_returns_non_empty_timestamp(self, store, sample_test_file):
        meta = store.seal_tests("run-ts", sample_test_file, spec_hash="ts")
        assert meta.sealed_at and meta.sealed_at.endswith("Z")

    def test_returns_non_empty_hash(self, store, sample_test_file):
        meta = store.seal_tests("run-hash", sample_test_file, spec_hash="h")
        assert meta.test_file_hash
        assert len(meta.test_file_hash) == 64  # SHA-256 hex length

    def test_spec_hash_stored_verbatim(self, store, sample_test_file):
        arbitrary = "6a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef0123456789"
        store.seal_tests("run-spec", sample_test_file, spec_hash=arbitrary)
        meta = store.get_manifest("run-spec")
        assert meta.spec_hash == arbitrary

    def test_manifest_version_is_10(self, store, sample_test_file):
        meta = store.seal_tests("run-ver", sample_test_file, spec_hash="v")
        assert meta.manifest_version == "1.0"

    def test_manifest_json_written(self, store, sample_test_file):
        store.seal_tests("run-mjson", sample_test_file, spec_hash="x")
        manifest_path = store._root / "run-mjson" / "manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["run_id"] == "run-mjson"

    def test_test_file_copied_into_store(self, store, sample_test_file):
        store.seal_tests("run-copy", sample_test_file, spec_hash="c")
        dest = store._root / "run-copy" / "acceptance_tests.py"
        assert dest.exists()

    def test_sealed_test_file_is_read_only(self, store, sample_test_file):
        store.seal_tests("run-ro", sample_test_file, spec_hash="deadbeef")
        sealed = store.get_test_path("run-ro")
        mode = stat.S_IMODE(os.stat(sealed).st_mode)
        assert not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def test_sealed_manifest_is_read_only(self, store, sample_test_file):
        store.seal_tests("run-rom", sample_test_file, spec_hash="deadbeef")
        sealed = store.get_test_path("run-rom")
        manifest = sealed.parent / "manifest.json"
        mode = stat.S_IMODE(os.stat(manifest).st_mode)
        assert not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def test_empty_file_seals_without_error(self, store, tmp_path):
        empty = tmp_path / "acceptance_tests.py"
        empty.write_text("")
        meta = store.seal_tests("run-empty", empty, spec_hash="emptyhash")
        assert meta is not None

    def test_missing_source_raises_not_found(self, store, tmp_path):
        nonexistent = tmp_path / "does_not_exist.py"
        with pytest.raises(TestStoreError) as exc_info:
            store.seal_tests("run-missing", nonexistent, spec_hash="x")
        assert "not found" in str(exc_info.value).lower()

    def test_source_directory_raises_not_found(self, store, tmp_path):
        a_dir = tmp_path / "some_dir"
        a_dir.mkdir()
        with pytest.raises(TestStoreError) as exc_info:
            store.seal_tests("run-dir", a_dir, spec_hash="x")
        assert "not found" in str(exc_info.value).lower()

    def test_duplicate_seal_raises_already_sealed(self, store, sample_test_file):
        store.seal_tests("run-dup", sample_test_file, spec_hash="first")
        with pytest.raises(TestStoreError) as exc_info:
            store.seal_tests("run-dup", sample_test_file, spec_hash="second")
        assert "already sealed" in str(exc_info.value).lower()

    def test_creates_store_root_if_missing(self, tmp_path, sample_test_file):
        new_root = tmp_path / "brand_new" / "nested" / "store"
        assert not new_root.exists()
        store = TestStore(store_root=new_root)
        meta = store.seal_tests("run-autocreate", sample_test_file, spec_hash="x")
        assert meta is not None
        assert new_root.exists()

    def test_permissions_error_raises_test_store_error(self, store, sample_test_file):
        # Only raise on the explicit chmod 444 (read-only) calls, not on shutil.copy2's
        # internal chmod calls (which preserve original file permissions, not 0o444).
        read_only_mode = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH

        def chmod_side_effect(path, mode):
            if mode == read_only_mode:
                raise OSError("Permission denied")

        with patch("orchestration_engine.test_store.os.chmod", side_effect=chmod_side_effect):
            with pytest.raises(TestStoreError) as exc_info:
                store.seal_tests("run-perm", sample_test_file, spec_hash="p")
        assert "permissions" in str(exc_info.value).lower()

    def test_path_traversal_run_id_raises_invalid(self, store, sample_test_file):
        with pytest.raises(TestStoreError) as exc_info:
            store.seal_tests("../evil", sample_test_file, spec_hash="x")
        assert "invalid run_id" in str(exc_info.value).lower()

    def test_absolute_run_id_raises_invalid(self, store, sample_test_file):
        with pytest.raises(TestStoreError) as exc_info:
            store.seal_tests("/etc/passwd", sample_test_file, spec_hash="x")
        assert "invalid run_id" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TestVerifyStore
# ---------------------------------------------------------------------------


class TestVerifyStore:
    def test_unmodified_returns_true(self, store, sample_test_file):
        store.seal_tests("run-ok", sample_test_file, spec_hash="v1")
        assert store.verify_store("run-ok") is True

    def test_tampered_file_returns_false(self, store, sample_test_file):
        store.seal_tests("run-tamper", sample_test_file, spec_hash="v2")
        sealed = store.get_test_path("run-tamper")
        os.chmod(sealed, 0o644)
        sealed.write_text("# tampered!\n")
        assert store.verify_store("run-tamper") is False

    def test_missing_store_raises_not_found(self, store):
        with pytest.raises(TestStoreError) as exc_info:
            store.verify_store("run-does-not-exist")
        assert "not found" in str(exc_info.value).lower()

    def test_tampered_manifest_invalid_json_raises(self, store, sample_test_file):
        store.seal_tests("run-bad-json", sample_test_file, spec_hash="v3")
        sealed = store.get_test_path("run-bad-json")
        manifest = sealed.parent / "manifest.json"
        os.chmod(manifest, 0o644)
        manifest.write_text("NOT VALID JSON {{{")
        with pytest.raises(TestStoreError):
            store.verify_store("run-bad-json")

    def test_tampered_manifest_missing_field_raises(self, store, sample_test_file):
        store.seal_tests("run-missing-field", sample_test_file, spec_hash="v4")
        sealed = store.get_test_path("run-missing-field")
        manifest = sealed.parent / "manifest.json"
        os.chmod(manifest, 0o644)
        data = json.loads(manifest.read_text())
        del data["test_file_hash"]
        manifest.write_text(json.dumps(data))
        with pytest.raises(TestStoreError):
            store.verify_store("run-missing-field")

    def test_deleted_test_file_raises_not_found(self, store, sample_test_file):
        store.seal_tests("run-deleted-test", sample_test_file, spec_hash="v5")
        sealed = store.get_test_path("run-deleted-test")
        os.chmod(sealed, 0o644)
        sealed.unlink()
        with pytest.raises(TestStoreError) as exc_info:
            store.verify_store("run-deleted-test")
        assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TestGetTestPath
# ---------------------------------------------------------------------------


class TestGetTestPath:
    def test_returns_path_object(self, store, sample_test_file):
        store.seal_tests("run-path", sample_test_file, spec_hash="p1")
        path = store.get_test_path("run-path")
        assert isinstance(path, Path)
        assert path.exists()
        assert path.is_file()

    def test_missing_store_raises_not_found(self, store):
        with pytest.raises(TestStoreError) as exc_info:
            store.get_test_path("run-nonexistent")
        assert "not found" in str(exc_info.value).lower()

    def test_deleted_test_file_raises_not_found(self, store, sample_test_file):
        store.seal_tests("run-path-del", sample_test_file, spec_hash="p2")
        sealed = store.get_test_path("run-path-del")
        os.chmod(sealed, 0o644)
        sealed.unlink()
        with pytest.raises(TestStoreError) as exc_info:
            store.get_test_path("run-path-del")
        assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TestGetManifest
# ---------------------------------------------------------------------------


class TestGetManifest:
    def test_returns_manifest_dataclass(self, store, sample_test_file):
        store.seal_tests("run-manifest", sample_test_file, spec_hash="m1")
        meta = store.get_manifest("run-manifest")
        assert isinstance(meta, TestManifest)

    def test_all_fields_populated(self, store, sample_test_file):
        spec_hash = "0011223344556677889900aabbccddeeff"
        store.seal_tests("run-all-fields", sample_test_file, spec_hash=spec_hash)
        meta = store.get_manifest("run-all-fields")
        assert meta.run_id == "run-all-fields"
        assert meta.sealed_at
        assert meta.test_file_hash
        assert meta.spec_hash == spec_hash
        assert meta.manifest_version

    def test_spec_hash_matches_what_was_sealed(self, store, sample_test_file):
        store.seal_tests("run-sh", sample_test_file, spec_hash="verbatim-value")
        meta = store.get_manifest("run-sh")
        assert meta.spec_hash == "verbatim-value"

    def test_missing_store_raises_not_found(self, store):
        with pytest.raises(TestStoreError) as exc_info:
            store.get_manifest("run-ghost")
        assert "not found" in str(exc_info.value).lower()

    def test_deleted_manifest_raises_not_found(self, store, sample_test_file):
        store.seal_tests("run-del-manifest", sample_test_file, spec_hash="dm1")
        sealed = store.get_test_path("run-del-manifest")
        manifest = sealed.parent / "manifest.json"
        os.chmod(manifest, 0o644)
        manifest.unlink()
        with pytest.raises(TestStoreError) as exc_info:
            store.get_manifest("run-del-manifest")
        assert "not found" in str(exc_info.value).lower()

    def test_unknown_manifest_version_raises_unsupported(self, store, sample_test_file):
        store.seal_tests("run-ver", sample_test_file, spec_hash="v99")
        sealed = store.get_test_path("run-ver")
        manifest = sealed.parent / "manifest.json"
        os.chmod(manifest, 0o644)
        data = json.loads(manifest.read_text())
        data["manifest_version"] = "99.99"
        manifest.write_text(json.dumps(data))
        with pytest.raises(TestStoreError) as exc_info:
            store.get_manifest("run-ver")
        assert "unsupported manifest version" in str(exc_info.value).lower()

    def test_invalid_json_raises_test_store_error(self, store, sample_test_file):
        store.seal_tests("run-inv-json", sample_test_file, spec_hash="j")
        sealed = store.get_test_path("run-inv-json")
        manifest = sealed.parent / "manifest.json"
        os.chmod(manifest, 0o644)
        manifest.write_text("{not valid json")
        with pytest.raises(TestStoreError):
            store.get_manifest("run-inv-json")

    def test_missing_required_field_raises_test_store_error(self, store, sample_test_file):
        store.seal_tests("run-mrf", sample_test_file, spec_hash="j2")
        sealed = store.get_test_path("run-mrf")
        manifest = sealed.parent / "manifest.json"
        os.chmod(manifest, 0o644)
        data = json.loads(manifest.read_text())
        del data["spec_hash"]
        manifest.write_text(json.dumps(data))
        with pytest.raises(TestStoreError):
            store.get_manifest("run-mrf")


# ---------------------------------------------------------------------------
# TestConfiguration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_env_var_overrides_store_root(self, tmp_path, monkeypatch, sample_test_file):
        custom_root = tmp_path / "custom_root"
        monkeypatch.setenv("ORCHEMIST_TEST_STORE", str(custom_root))
        default_store = TestStore()
        default_store.seal_tests("run-envvar", sample_test_file, spec_hash="env1")
        assert (custom_root / "run-envvar").exists()

    def test_default_root_when_env_var_unset(self, monkeypatch):
        monkeypatch.delenv("ORCHEMIST_TEST_STORE", raising=False)
        root = _get_store_root()
        expected = Path.home() / ".orchemist" / "test_store"
        assert root == expected


# ---------------------------------------------------------------------------
# TestValidateRunId (unit tests for the helper)
# ---------------------------------------------------------------------------


class TestValidateRunId:
    def test_valid_simple_id(self):
        # Should not raise
        _validate_run_id("run-001")

    def test_valid_nested_id(self):
        # Nested but not traversal
        _validate_run_id("2026/03/22/run-001")

    def test_empty_string_raises(self):
        with pytest.raises(TestStoreError) as exc_info:
            _validate_run_id("")
        assert "invalid run_id" in str(exc_info.value).lower()

    def test_dotdot_raises(self):
        with pytest.raises(TestStoreError) as exc_info:
            _validate_run_id("../evil")
        assert "invalid run_id" in str(exc_info.value).lower()

    def test_nested_dotdot_raises(self):
        with pytest.raises(TestStoreError) as exc_info:
            _validate_run_id("good/../evil")
        assert "invalid run_id" in str(exc_info.value).lower()

    def test_absolute_path_raises(self):
        with pytest.raises(TestStoreError) as exc_info:
            _validate_run_id("/etc/passwd")
        assert "invalid run_id" in str(exc_info.value).lower()
