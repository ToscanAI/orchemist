"""file_guard.py — SHA256 checksum helpers for protected pipeline outputs.

These functions are called by the sequencer to silently verify that
nominated files have not been modified between pipeline phases.
No information about the verification mechanism is exposed to running agents.
"""
import hashlib
from pathlib import Path
from typing import Union


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
            f"Protected file modified: {name} "
            f"(expected sha256:{expected}, got sha256:{actual})"
        )
