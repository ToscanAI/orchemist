"""file_guard.py — SHA256 checksum helpers for protected pipeline outputs.

These functions are called by the sequencer to silently verify that
nominated files have not been modified between pipeline phases.
No information about the verification mechanism is exposed to running agents.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import List, Optional, Union

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default exclusion patterns for compute_directory_hash
# ---------------------------------------------------------------------------

#: Directory names to skip entirely when recursing.
DEFAULT_EXCLUDES: List[str] = ["__pycache__", ".pytest_cache", ".git"]

#: File suffixes to skip.
DEFAULT_EXCLUDE_SUFFIXES: List[str] = [".pyc"]


class FileGuardError(Exception):
    """Raised when a protected file's hash does not match the expected value."""

    pass


def compute_hash(path: Union[str, Path]) -> str:
    """Compute SHA256 of the file at *path* and return the hex digest.

    Args:
        path: Path to the file (str or Path).

    Returns:
        Lowercase hex SHA256 digest string (64 chars).

    Raises:
        FileNotFoundError: If the file does not exist.
        OSError: On any other I/O error.
    """
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_directory_hash(  # noqa: C901
    path: Union[str, Path],
    exclude_patterns: Optional[List[str]] = None,
    exclude_suffixes: Optional[List[str]] = None,
) -> Optional[str]:
    """Compute a deterministic SHA256 hash over a directory tree.

    Recursively walks *path*, sorts entries **lexicographically by relative
    path** (making the result deterministic across platforms), and produces a
    single SHA256 digest that covers both file paths and their contents.

    Symlinks are **not** followed — the link *target path string* is hashed
    instead of the linked file's content.  This avoids accidental escaping of
    the guarded directory boundary.

    Directories named in *exclude_patterns* and files whose suffix is in
    *exclude_suffixes* are silently skipped.  The defaults are designed to
    prevent Python test-execution artefacts from causing false positives:

    * Directories: ``__pycache__``, ``.pytest_cache``, ``.git``
    * Suffixes: ``.pyc``

    Args:
        path: Root directory to hash (str or Path).
        exclude_patterns: Directory names to skip entirely.  Defaults to
            :data:`DEFAULT_EXCLUDES`.
        exclude_suffixes: File suffixes (e.g. ``".pyc"``) to skip.  Defaults
            to :data:`DEFAULT_EXCLUDE_SUFFIXES`.

    Returns:
        Lowercase SHA256 hex digest string (64 chars), or ``None`` when *path*
        does not exist or is not a directory (graceful degradation — logs a
        WARNING and skips instead of raising).
    """
    if exclude_patterns is None:
        exclude_patterns = DEFAULT_EXCLUDES
    if exclude_suffixes is None:
        exclude_suffixes = DEFAULT_EXCLUDE_SUFFIXES

    root = Path(path)

    if not root.exists():
        _logger.warning("compute_directory_hash: path does not exist: %s — skipping.", root)
        return None
    if not root.is_dir():
        _logger.warning("compute_directory_hash: path is not a directory: %s — skipping.", root)
        return None

    # Collect (relative_path_str, is_symlink, symlink_target_or_None) for all
    # entries, then sort lexicographically so the digest is deterministic.
    entries: List[tuple] = []

    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        # Prune excluded directories in-place so os.walk skips them entirely.
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_patterns)

        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            rel = str(full.relative_to(root))

            # Skip excluded suffixes
            if any(filename.endswith(sfx) for sfx in exclude_suffixes):
                continue

            entries.append((rel, full))

    # Sort by relative path string (lexicographic, deterministic)
    entries.sort(key=lambda e: e[0])

    h = hashlib.sha256()
    for rel, full in entries:
        # Always include the relative path so renames/additions change the hash.
        h.update(rel.encode())
        h.update(b"\x00")

        if full.is_symlink():
            # Hash the *link target string* — do NOT read linked file content.
            target = os.readlink(str(full))
            h.update(b"SYMLINK:")
            h.update(target.encode())
        else:
            # Hash the file contents in chunks.
            try:
                with full.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
            except OSError as exc:
                # Skip unreadable files and log so operators can diagnose
                # unexpected permission/IO issues in the guarded directory.
                _logger.debug(
                    "compute_directory_hash: skipping unreadable file %s (%s)",
                    full,
                    exc,
                )

        h.update(b"\xff")  # separator between entries

    return h.hexdigest()


def _normalize_glob(pattern: str) -> str:
    """Normalize a glob so a trailing ``**`` matches FILES recursively (Issue #986).

    On Python 3.10–3.12 (the supported / CI-tested versions) ``Path.glob("src/**")``
    matches DIRECTORIES ONLY — with an ``is_file()`` filter it yields an empty set,
    so a change to ``src/x.ts`` could never flip the hash. The fix: rewrite a
    trailing ``**`` segment (or a bare ``**``) into ``…/**/*`` so the subsequent
    ``is_file()/is_symlink()`` filter sees the actual files under the tree. A
    pattern that does not end in ``**`` is returned unchanged.

    Examples:
        ``"src/**"``  → ``"src/**/*"``   (recursive files under src/)
        ``"**"``       → ``"**/*"``
        ``"src/**/*.py"`` → ``"src/**/*.py"`` (already file-matching; unchanged)
        ``"package.json"`` → ``"package.json"`` (no ``**``; unchanged)
    """
    if pattern == "**":
        return "**/*"
    if pattern.endswith("/**"):
        return pattern + "/*"
    return pattern


def hash_glob_set(root: Union[str, Path], globs: List[str]) -> str:
    """Deterministic SHA256 over the SET of files matched by *globs* under *root*.

    Drives the same digest recipe as :func:`compute_directory_hash` (rel-path +
    ``b"\\x00"`` + content + ``b"\\xff"`` separator, entries sorted lexicographically
    by POSIX rel-path) but over a glob-matched file set instead of a full
    ``os.walk``. Used by the Issue #986 warm cache as the per-hook invalidation key:

    * identical content ⇒ identical digest;
    * a 1-byte change, or an add/remove under the globs ⇒ a different digest;
    * the result is order-independent (the globs are flattened into a set then
      sorted);
    * an empty match-set returns the SHA256 of the empty byte string — a stable,
      non-empty hex sentinel, so "no inputs yet" is well-defined and never
      collides with a non-empty match-set's digest.

    Symlinks are NOT followed (their target string is hashed) — the same boundary
    safety as :func:`compute_directory_hash`. Directories matched by a glob are
    skipped (only files contribute). A missing *root* is treated as an empty
    match-set (the sentinel).

    Each glob is normalized via :func:`_normalize_glob` so a trailing ``**``
    matches files recursively on Python 3.10–3.12 (see that function).

    Args:
        root: Root directory the globs are rooted at (str or Path).
        globs: List of path-glob strings, relative to *root*.

    Returns:
        Lowercase SHA256 hex digest string (64 chars).
    """
    root_path = Path(root)
    matched: set = set()
    if root_path.exists():
        for pattern in globs:
            normalized = _normalize_glob(pattern)
            for p in root_path.glob(normalized):
                if p.is_file() or p.is_symlink():
                    matched.add(p)

    # Sort by POSIX rel-path string for deterministic, cross-platform ordering.
    entries = sorted(matched, key=lambda p: p.relative_to(root_path).as_posix())

    h = hashlib.sha256()
    for full in entries:
        rel = full.relative_to(root_path).as_posix()
        h.update(rel.encode())
        h.update(b"\x00")
        if full.is_symlink():
            # Hash the link target string — do NOT read linked file content.
            h.update(b"SYMLINK:")
            h.update(os.readlink(str(full)).encode())
        else:
            try:
                with full.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
            except OSError as exc:
                # Unreadable → contributes only its rel-path (mirror
                # compute_directory_hash's graceful skip).
                _logger.debug("hash_glob_set: skipping unreadable file %s (%s)", full, exc)
        h.update(b"\xff")  # separator between entries
    return h.hexdigest()


def verify_hash(path: Union[str, Path], expected: str) -> None:
    """Verify that the file at *path* has the expected SHA256 hash.

    Args:
        path:     Path to the file.
        expected: Expected lowercase hex SHA256 digest.

    Returns:
        None when the hash matches.

    Raises:
        FileNotFoundError: If the file does not exist.
        FileGuardError: If the actual hash differs from *expected*.
            The error message follows the format:
            "Protected file modified: <name> (expected sha256:<abc>, got sha256:<def>)"
    """
    actual = compute_hash(path)
    if actual != expected:
        name = Path(path).name
        raise FileGuardError(
            f"Protected file modified: {name} " f"(expected sha256:{expected}, got sha256:{actual})"
        )
