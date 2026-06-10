"""Immutable Acceptance Test Store (Issue #541).

Provides a dedicated, filesystem-isolated store for sealing acceptance test
files after they are written by the acceptance-test phase of the coding
pipeline. Once sealed, both the test file and its manifest are set read-only
(chmod 444) so that no subsequent pipeline phase can modify them.

Typical usage (module-level convenience functions)::

    from orchestration_engine.test_store import seal_tests, verify_store, get_test_path, get_manifest

    # Seal after the acceptance-test phase completes:
    manifest = seal_tests(run_id="abc123", test_file_path="/tmp/output/acceptance_tests.py",
                          spec_hash="<sha256 of spec>")

    # Later, in the validator:
    if verify_store("abc123"):
        path = get_test_path("abc123")

Or with a custom root (e.g. in tests)::

    from orchestration_engine.test_store import TestStore

    store = TestStore(store_root=Path("/tmp/my_store"))
    store.seal_tests(...)

Configuration
-------------
The root directory for all test stores is resolved as follows:

1. If the environment variable ``ORCHEMIST_TEST_STORE`` is set, its value is
   used as the root directory.
2. Otherwise, the default is ``~/.orchemist/test_store/``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .errors import OrchestratorError
from .file_guard import compute_hash
from .timestamps import now_utc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The only manifest schema versions this module can read or write.
SUPPORTED_MANIFEST_VERSIONS: frozenset[str] = frozenset({"1.0"})

#: Current schema version written by :meth:`TestStore.seal_tests`.
_CURRENT_MANIFEST_VERSION = "1.0"

#: Name of the sealed test file inside a run directory.
_TEST_FILE_NAME = "acceptance_tests.py"

#: Name of the manifest file inside a run directory.
_MANIFEST_FILE_NAME = "manifest.json"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TestStoreError(OrchestratorError):
    """Raised for any test store operation failure.

    Subclasses :class:`~orchestration_engine.errors.OrchestratorError` so
    that callers who catch the base error class still handle these.
    """


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TestManifest:
    """Deserialized metadata record for a sealed acceptance-test store.

    Attributes:
        run_id:            The pipeline run identifier used to seal this store.
        sealed_at:         ISO 8601 UTC timestamp of when sealing occurred
                           (e.g. ``"2026-03-22T07:30:00.123456Z"``).
        test_file_hash:    SHA-256 hex digest of the sealed ``acceptance_tests.py``.
        spec_hash:         SHA-256 hex digest of the spec that generated the tests
                           (stored verbatim — not re-computed by the test store).
        manifest_version:  Schema version string (e.g. ``"1.0"``).
    """

    run_id: str
    sealed_at: str
    test_file_hash: str
    spec_hash: str
    manifest_version: str


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _get_store_root() -> Path:
    """Return the configured test-store root directory.

    Reads the ``ORCHEMIST_TEST_STORE`` environment variable; falls back to
    ``~/.orchemist/test_store/`` when the variable is not set.

    Returns:
        An *expanded* :class:`~pathlib.Path`.  The directory may not yet exist;
        callers are responsible for creating it when needed.
    """
    env = os.environ.get("ORCHEMIST_TEST_STORE")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".orchemist" / "test_store"


def _validate_run_id(run_id: str) -> None:
    """Raise :class:`TestStoreError` if *run_id* would escape the store root.

    Checks for:
    - Absolute paths (e.g. ``"/etc/passwd"``)
    - Path-traversal components (``".."``)
    - Empty string

    Args:
        run_id: The run identifier to validate.

    Raises:
        TestStoreError: With a message containing ``"invalid run_id"`` when the
            identifier fails validation.
    """
    if not run_id:
        raise TestStoreError("invalid run_id: must not be empty")
    p = Path(run_id)
    if p.is_absolute() or ".." in p.parts:
        raise TestStoreError(f"invalid run_id: '{run_id}' contains path traversal or is absolute")


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------


class TestStore:
    """Manager for the immutable acceptance-test store.

    Each *run identifier* maps to a dedicated sub-directory inside the store
    root.  That sub-directory contains exactly two files once sealed:

    * ``acceptance_tests.py`` — a verbatim copy of the test file.
    * ``manifest.json`` — JSON metadata (see :class:`TestManifest`).

    Both files are ``chmod 444`` after sealing so that no pipeline phase can
    accidentally overwrite them.

    Args:
        store_root: Override the root directory.  When *None* (the default),
            the root is resolved via :func:`_get_store_root`.
    """

    def __init__(self, store_root: Optional[Path] = None) -> None:
        self._root: Path = store_root if store_root is not None else _get_store_root()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def seal_tests(
        self,
        run_id: str,
        test_file_path: Union[str, Path],
        spec_hash: str,
    ) -> TestManifest:
        """Copy *test_file_path* into the store and seal it as read-only.

        The store root is created automatically if it does not yet exist.

        Args:
            run_id:         Unique identifier for this pipeline run.  Must not
                            be an absolute path or contain ``".."`` components.
            test_file_path: Path to the source ``acceptance_tests.py`` file.
                            Must be a regular file (not a directory).
            spec_hash:      SHA-256 hex digest of the spec that generated the
                            tests.  Stored verbatim in the manifest — the test
                            store does not re-hash or validate this value.

        Returns:
            A :class:`TestManifest` describing the sealed store.

        Raises:
            TestStoreError: With ``"invalid run_id"`` when *run_id* is
                invalid (absolute path or contains ``".."``) .
            TestStoreError: With ``"not found"`` when *test_file_path* does
                not exist or is a directory.
            TestStoreError: With ``"already sealed"`` when the store for
                *run_id* already exists.
            TestStoreError: With ``"permissions"`` when ``chmod 444`` fails.
            TestStoreError: For any other unexpected failure.
        """
        _validate_run_id(run_id)
        src = Path(test_file_path)

        # Validate source: must exist AND be a regular file (not a directory)
        if not src.exists() or not src.is_file():
            raise TestStoreError(f"Test file not found: {src}")

        # Ensure store root exists
        self._root.mkdir(parents=True, exist_ok=True)

        # Atomic directory creation — POSIX mkdir is atomic; exist_ok=False
        # ensures exactly one concurrent caller can succeed.
        run_dir = self._root / run_id
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise TestStoreError(f"Run '{run_id}' already sealed")

        try:
            # Copy the test file into the store
            dest_test = run_dir / _TEST_FILE_NAME
            shutil.copy2(src, dest_test)

            # Hash the *copy* (not the source) to detect any copy corruption
            test_hash = compute_hash(dest_test)

            # Build and serialise the manifest
            manifest = TestManifest(
                run_id=run_id,
                sealed_at=now_utc().isoformat().replace("+00:00", "Z"),
                test_file_hash=test_hash,
                spec_hash=spec_hash,
                manifest_version=_CURRENT_MANIFEST_VERSION,
            )
            dest_manifest = run_dir / _MANIFEST_FILE_NAME
            dest_manifest.write_text(
                json.dumps(
                    {
                        "run_id": manifest.run_id,
                        "sealed_at": manifest.sealed_at,
                        "test_file_hash": manifest.test_file_hash,
                        "spec_hash": manifest.spec_hash,
                        "manifest_version": manifest.manifest_version,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            # Make both files read-only
            try:
                os.chmod(dest_test, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                os.chmod(dest_manifest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError as exc:
                raise TestStoreError(
                    f"Failed to set read-only permissions on sealed files: {exc}"
                ) from exc

            return manifest

        except TestStoreError:
            # Clean up the partially-created run directory so that a future
            # seal attempt with the same run_id is not blocked.
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise TestStoreError(f"Seal failed unexpectedly: {exc}") from exc

    def verify_store(self, run_id: str) -> bool:
        """Verify the integrity of a sealed test store.

        Re-computes the SHA-256 of the stored ``acceptance_tests.py`` and
        compares it to the hash recorded in ``manifest.json``.

        Args:
            run_id: The pipeline run identifier.

        Returns:
            ``True`` if the stored test file matches the recorded hash;
            ``False`` if the hash does not match (file has been tampered with).

        Raises:
            TestStoreError: With ``"not found"`` when there is no sealed store
                for *run_id*, or when the sealed test file has been deleted.
            TestStoreError: When the manifest is invalid JSON or is missing
                required fields, or when its version is unsupported.
        """
        _validate_run_id(run_id)
        run_dir = self._root / run_id

        if not run_dir.exists():
            raise TestStoreError(f"Store not found for run '{run_id}'")

        # get_manifest validates JSON, required fields, and version
        manifest = self.get_manifest(run_id)

        dest_test = run_dir / _TEST_FILE_NAME
        if not dest_test.exists():
            raise TestStoreError(f"Sealed test file not found for run '{run_id}'")

        actual_hash = compute_hash(dest_test)
        return actual_hash == manifest.test_file_hash

    def get_test_path(self, run_id: str) -> Path:
        """Return the filesystem path to the sealed ``acceptance_tests.py``.

        Args:
            run_id: The pipeline run identifier.

        Returns:
            :class:`~pathlib.Path` to the sealed test file.

        Raises:
            TestStoreError: With ``"not found"`` when there is no sealed store
                for *run_id*, or when the test file has been deleted from the
                store directory.
        """
        _validate_run_id(run_id)
        run_dir = self._root / run_id
        if not run_dir.exists():
            raise TestStoreError(f"Store not found for run '{run_id}'")
        test_path = run_dir / _TEST_FILE_NAME
        if not test_path.exists():
            raise TestStoreError(f"Sealed test file not found for run '{run_id}'")
        return test_path

    def get_manifest(self, run_id: str) -> TestManifest:
        """Return the deserialized manifest for a sealed test store.

        Args:
            run_id: The pipeline run identifier.

        Returns:
            :class:`TestManifest` with all fields populated.

        Raises:
            TestStoreError: With ``"not found"`` when there is no sealed store
                or when the manifest file has been deleted from the store.
            TestStoreError: When the manifest contains invalid JSON or is
                missing required fields.
            TestStoreError: With ``"unsupported manifest version"`` when the
                ``manifest_version`` field is not in
                :data:`SUPPORTED_MANIFEST_VERSIONS`.
        """
        _validate_run_id(run_id)
        run_dir = self._root / run_id
        if not run_dir.exists():
            raise TestStoreError(f"Store not found for run '{run_id}'")

        manifest_path = run_dir / _MANIFEST_FILE_NAME
        if not manifest_path.exists():
            raise TestStoreError(f"Manifest not found for run '{run_id}'")

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TestStoreError(f"Failed to parse manifest for run '{run_id}': {exc}") from exc

        # Validate required fields
        required_fields = {"run_id", "sealed_at", "test_file_hash", "spec_hash", "manifest_version"}
        missing = required_fields - set(data.keys())
        if missing:
            raise TestStoreError(
                f"Manifest for run '{run_id}' is missing required fields: {missing}"
            )

        # Check manifest version before constructing the dataclass
        version = data["manifest_version"]
        if version not in SUPPORTED_MANIFEST_VERSIONS:
            raise TestStoreError(f"Unsupported manifest version '{version}' for run '{run_id}'")

        return TestManifest(
            run_id=data["run_id"],
            sealed_at=data["sealed_at"],
            test_file_hash=data["test_file_hash"],
            spec_hash=data["spec_hash"],
            manifest_version=data["manifest_version"],
        )


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------
# These create a default TestStore (backed by _get_store_root()) and delegate.
# Useful for callers who don't need a custom root.


def seal_tests(
    run_id: str,
    test_file_path: Union[str, Path],
    spec_hash: str,
) -> TestManifest:
    """Module-level wrapper for :meth:`TestStore.seal_tests`."""
    return TestStore().seal_tests(run_id, test_file_path, spec_hash)


def verify_store(run_id: str) -> bool:
    """Module-level wrapper for :meth:`TestStore.verify_store`."""
    return TestStore().verify_store(run_id)


def get_test_path(run_id: str) -> Path:
    """Module-level wrapper for :meth:`TestStore.get_test_path`."""
    return TestStore().get_test_path(run_id)


def get_manifest(run_id: str) -> TestManifest:
    """Module-level wrapper for :meth:`TestStore.get_manifest`."""
    return TestStore().get_manifest(run_id)
