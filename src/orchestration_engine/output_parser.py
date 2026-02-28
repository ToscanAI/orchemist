"""
output_parser.py — Structured output parser for orchestration engine phases.

Extracts ``=== FILE: path ===`` ... ``=== END FILE ===`` blocks from LLM phase
output into structured :class:`FileBlock` objects.  Provides mandatory path
traversal protection so that results can be safely written to disk.

Duplicate paths:
    When the same relative path appears in multiple FILE blocks, **all**
    occurrences are included in the returned ``files`` list.  The caller is
    responsible for deduplication if that matters for their use-case.

Usage::

    from orchestration_engine.output_parser import parse_output, extract_and_write

    result = parse_output(llm_output)
    if result.has_files:
        written = extract_and_write(llm_output, Path("./output"))

"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = ["FileBlock", "ParsedOutput", "parse_output", "extract_and_write"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled regular expressions
# ---------------------------------------------------------------------------

# Matches:  === FILE: some/path.py ===
# Flexible on internal whitespace; path captured in group 1 (still stripped
# after capture to handle any residual spaces).
_FILE_START_RE = re.compile(r"^\s*===\s+FILE:\s+(.*?)\s+===\s*$")

# Matches:  === END FILE ===
_FILE_END_RE = re.compile(r"^\s*===\s+END\s+FILE\s+===\s*$")

# URL-encoded dot / slash detection (case-insensitive)
_URL_ENCODED_RE = re.compile(r"%2[ef]", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FileBlock:
    """A single extracted file block.

    Attributes:
        path:    Relative path as extracted from the FILE delimiter.  Always
                 safe (validated by :func:`parse_output`).
        content: Exact file content with all whitespace and newlines preserved.
                 No stripping of any kind is applied.
    """

    path: str
    content: str


@dataclass
class ParsedOutput:
    """Result of parsing structured LLM output.

    Attributes:
        raw_text:  The original input string, byte-for-byte identical to what
                   was passed to :func:`parse_output`.
        files:     Extracted :class:`FileBlock` objects, in order of appearance.
                   Empty list when no FILE blocks are present.
        has_files: Computed in ``__post_init__``; ``True`` iff ``len(files) > 0``.

    Note:
        ``has_files`` is *always* consistent with ``bool(files)``; it is set
        once during construction and never mutated.
    """

    raw_text: str
    files: list[FileBlock]
    has_files: bool = field(init=False)

    def __post_init__(self) -> None:
        self.has_files = len(self.files) > 0


# ---------------------------------------------------------------------------
# Path safety validation
# ---------------------------------------------------------------------------


def _is_safe_path(path: str) -> bool:
    """Return ``True`` if *path* is safe to use as a relative output path.

    Rejects all of the following with a ``WARNING`` log entry:

    * Empty or whitespace-only strings.
    * Paths containing backslashes (Windows directory separator ``\\``).
    * UNC paths starting with ``//``.
    * Absolute Unix paths (starting with ``/``).
    * Windows drive-letter paths (``C:/``, ``C:\\``, …).
    * Any path component equal to ``..`` (parent-directory traversal).
    * Any path component equal to ``.`` (current-directory reference;
      resolves to the output root itself which is a directory, not a file).
    * URL-encoded dots or slashes (``%2e``, ``%2f``, ``%2E``, ``%2F``).

    Args:
        path: Candidate path string extracted from a FILE delimiter.

    Returns:
        ``True`` if the path passes all checks; ``False`` otherwise.
    """
    # ── 1. Empty / whitespace-only ───────────────────────────────────────────
    if not path or not path.strip():
        logger.warning("output_parser: rejected empty/whitespace path")
        return False

    # ── 2. URL-encoded traversal sequences ──────────────────────────────────
    if _URL_ENCODED_RE.search(path):
        logger.warning(
            "output_parser: rejected URL-encoded path: %r", path
        )
        return False

    # ── 3. Backslash (Windows separator — reject outright) ──────────────────
    if "\\" in path:
        logger.warning(
            "output_parser: rejected path containing backslash: %r", path
        )
        return False

    # ── 4. UNC path  (//server/share) — checked before generic /  ────────────
    # This must come before the absolute-path check so UNC paths receive the
    # more specific log message; both checks are otherwise equivalent for //.
    if path.startswith("//"):
        logger.warning(
            "output_parser: rejected UNC path: %r", path
        )
        return False

    # ── 5. Absolute Unix path ────────────────────────────────────────────────
    if path.startswith("/"):
        logger.warning(
            "output_parser: rejected absolute Unix path: %r", path
        )
        return False

    # ── 6. Windows drive-letter path  (C:/ or just C:) ───────────────────────
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        logger.warning(
            "output_parser: rejected Windows drive-letter path: %r", path
        )
        return False

    # ── 7. Single-dot path "." ───────────────────────────────────────────────
    # PurePosixPath normalises "." to an empty-parts path, so we must check
    # the normalised string representation before the parts loop below.
    # "." resolves to output_dir itself (a directory), which would cause
    # write_text() to raise IsADirectoryError — reject it explicitly.
    try:
        normalised = PurePosixPath(path)
    except Exception:
        logger.warning(
            "output_parser: rejected unparseable path: %r", path
        )
        return False

    if str(normalised) == ".":
        logger.warning(
            "output_parser: rejected single-dot path: %r", path
        )
        return False

    # ── 8. Parent-directory traversal via .. component ───────────────────────
    parts = normalised.parts
    if ".." in parts:
        logger.warning(
            "output_parser: rejected path with '..' component: %r", path
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_output(text: str) -> ParsedOutput:
    """Parse structured FILE blocks from LLM phase output.

    Scans *text* line-by-line for ``=== FILE: <path> ===`` markers and
    collects content until the matching ``=== END FILE ===`` terminator.
    Each valid, closed block becomes a :class:`FileBlock`.

    Graceful degradation guarantees:

    * **No FILE markers** → ``ParsedOutput(raw_text=text, files=[], has_files=False)``
    * **Empty string** → same as above with ``raw_text=""``
    * **Unclosed block** (no END FILE) → silently dropped; not partially included
    * **Invalid path** → block silently skipped; warning emitted to ``logging``
    * **Any other malformed input** → never raises; always returns ``ParsedOutput``

    Duplicate paths:
        Both blocks are included.  The caller decides dedup policy.

    Known limitation:
        A ``=== FILE: xxx ===`` marker that appears *inside* an already-open
        block is treated as literal content, not as a new block boundary.  If
        the LLM produces two consecutive FILE markers without an intervening
        END FILE, the second marker becomes content of the first block, and
        whatever follows it is never parsed as its own block.  Blocks that are
        never closed (no END FILE before EOF) are silently dropped.

    Args:
        text: Raw LLM output to parse.  Non-string values are coerced via
              ``str()``; if that fails ``""`` is used.

    Returns:
        :class:`ParsedOutput` whose ``raw_text`` is the exact input and
        ``files`` contains all successfully extracted :class:`FileBlock` objects.

    Examples::

        result = parse_output(
            "=== FILE: hello.py ===\\n"
            "print('hello')\\n"
            "=== END FILE ==="
        )
        assert result.has_files
        assert result.files[0].path == "hello.py"
        assert result.files[0].content == "print('hello')\\n"
    """
    # ── Coerce non-string input gracefully ───────────────────────────────────
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            text = ""

    files: list[FileBlock] = []

    # ── Line-by-line state machine ────────────────────────────────────────────
    # States:
    #   in_block=False                → scanning for FILE marker
    #   in_block=True, path is str   → collecting content for a valid block
    #   in_block=True, path is None  → skipping content for an invalid-path block

    in_block: bool = False
    current_path: str | None = None
    content_lines: list[str] = []

    # keepends=True preserves exact newlines inside content blocks.
    for line in text.splitlines(keepends=True):
        # Strip only trailing newline characters for regex matching, so that
        # indentation / leading whitespace is preserved in content_lines.
        line_for_match = line.rstrip("\r\n")

        if not in_block:
            m = _FILE_START_RE.match(line_for_match)
            if m:
                candidate = m.group(1).strip()
                if _is_safe_path(candidate):
                    in_block = True
                    current_path = candidate
                    content_lines = []
                else:
                    # Invalid path: enter block to swallow content, but mark
                    # current_path=None so we discard the block at END FILE.
                    in_block = True
                    current_path = None
                    content_lines = []
            # Lines outside blocks are discarded (they are prose / context).
        else:
            # Inside a FILE block.
            if _FILE_END_RE.match(line_for_match):
                if current_path is not None:
                    # Well-formed, valid-path block → save it.
                    files.append(
                        FileBlock(path=current_path, content="".join(content_lines))
                    )
                # Reset state regardless.
                in_block = False
                current_path = None
                content_lines = []
            else:
                # Accumulate content, including lines that look like FILE markers
                # (they are inside the block and must be treated as literal text).
                content_lines.append(line)

    # If still in_block at EOF → unclosed block; silently drop per AC-7.

    return ParsedOutput(raw_text=text, files=files)


def extract_and_write(text: str, output_dir: Path | str) -> list[FileBlock]:
    """Parse FILE blocks from *text* and write them under *output_dir*.

    Internally calls :func:`parse_output` (which enforces path traversal
    protection), then writes each :class:`FileBlock` to disk, creating
    parent directories as needed.

    Error handling:
        * If *output_dir* cannot be created, logs a warning and returns ``[]``.
        * If an individual file cannot be written, logs a warning and skips it.
        * Never raises exceptions.

    Security note:
        Path traversal is prevented at parse time by :func:`_is_safe_path`, and
        a secondary ``resolve() / relative_to()`` check is performed before each
        write.  There is an inherent TOCTOU (time-of-check-to-time-of-use) race
        between the ``resolve()`` check and the actual ``write_text()`` call: a
        symlink placed at the target path between those two operations could
        theoretically redirect the write outside *output_dir*.  This is a known
        filesystem-level limitation that is acceptable for local workspace use
        where the output directory is not exposed to untrusted concurrent writers.

    Args:
        text:       Raw LLM output containing FILE blocks.
        output_dir: Root directory for output files.  Accepts both
                    :class:`~pathlib.Path` and ``str``.  Created (with parents)
                    if it does not already exist.

    Returns:
        List of :class:`FileBlock` objects that were **successfully written** to
        disk, in the order they appear in *text*.

    Examples::

        from pathlib import Path
        written = extract_and_write(llm_text, Path("/tmp/phase_out"))
        for fb in written:
            print(fb.path, "written OK")
    """
    parsed = parse_output(text)

    if not parsed.has_files:
        return []

    output_dir = Path(output_dir)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(
            "extract_and_write: cannot create output_dir %r: %s", str(output_dir), exc
        )
        return []

    written: list[FileBlock] = []

    for fb in parsed.files:
        # parse_output already validated the path, but perform a final
        # resolution check to guarantee the target sits inside output_dir.
        target = output_dir / fb.path

        try:
            resolved_target = target.resolve()
            resolved_root = output_dir.resolve()
            resolved_target.relative_to(resolved_root)  # raises ValueError if outside
        except (ValueError, OSError) as exc:
            logger.warning(
                "extract_and_write: %r resolves outside output_dir, skipping: %s",
                fb.path,
                exc,
            )
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fb.content, encoding="utf-8")
            written.append(fb)
            logger.debug("extract_and_write: wrote %s", target)
        except Exception as exc:
            logger.warning(
                "extract_and_write: failed to write %r: %s", str(target), exc
            )

    return written
