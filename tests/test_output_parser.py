"""
tests/test_output_parser.py — Comprehensive tests for output_parser.py

Covers all acceptance criteria (AC-1 through AC-9) plus additional edge cases
and branch-level coverage for all error-handling paths.

Test classes:
    TestModuleImport             — AC-1: module exists and imports cleanly
    TestDataStructures           — AC-2: FileBlock and ParsedOutput shapes
    TestParserSignature          — AC-3: function signature and graceful degradation
    TestDelimiterParsing         — AC-4: FILE / END FILE recognition
    TestBackwardCompatibility    — AC-5: no markers → empty result
    TestPathTraversalProtection  — AC-6: all unsafe paths rejected
    TestEdgeCases                — AC-7: all specified edge cases
    TestExtractAndWrite          — AC-8: disk-write integration
    TestExtractAndWriteErrors    — AC-8 error paths: mkdir failure, write failure,
                                   symlink traversal bypass
    TestRobustness               — extra security and correctness checks
    TestInternalBranchCoverage   — covers the exception branches in _is_safe_path
                                   and parse_output that are unreachable in normal use

All tests are independent — no shared mutable state.
"""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# AC-1: Module import guard — run before any test collection
# ---------------------------------------------------------------------------
from orchestration_engine.output_parser import (  # noqa: E402
    FileBlock,
    ParsedOutput,
    _is_safe_path,  # private but tested for branch coverage
    extract_and_write,
    parse_output,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_block(path: str, content: str) -> str:
    """Build a well-formed FILE block string."""
    return f"=== FILE: {path} ===\n{content}=== END FILE ===\n"


def _assert_path_rejected(test: unittest.TestCase, path: str) -> None:
    """Assert that *path* is rejected — no FileBlock produced for it."""
    text = f"=== FILE: {path} ===\ncontent\n=== END FILE ===\n"
    result = parse_output(text)
    test.assertEqual(
        result.files, [], f"Expected path {path!r} to be rejected, got {result.files}"
    )


# ===========================================================================
# AC-1: Module exists and imports clean
# ===========================================================================


class TestModuleImport(unittest.TestCase):
    """AC-1 — module-level checks."""

    def test_symbols_exported(self) -> None:
        """parse_output, ParsedOutput, FileBlock are importable and callable/types."""
        self.assertTrue(callable(parse_output))
        self.assertTrue(callable(extract_and_write))
        self.assertIsInstance(FileBlock, type)
        self.assertIsInstance(ParsedOutput, type)

    def test_extract_and_write_callable(self) -> None:
        self.assertTrue(callable(extract_and_write))

    def test_no_third_party_imports(self) -> None:
        """Module must not introduce non-stdlib dependencies."""
        import orchestration_engine.output_parser as _mod

        source_file = Path(_mod.__file__).read_text()
        for lib in ["pydantic", "requests", "attrs", "numpy", "yaml", "click"]:
            self.assertNotIn(
                lib, source_file, f"Unexpected third-party import: {lib}"
            )

    def test_only_stdlib_modules_used(self) -> None:
        """Only re, pathlib, dataclasses, logging (plus __future__) should appear."""
        import orchestration_engine.output_parser as _mod

        source = Path(_mod.__file__).read_text()
        allowed = {"re", "pathlib", "dataclasses", "logging", "__future__"}
        # grep import lines
        import re

        import_lines = re.findall(r"^(?:import|from)\s+(\S+)", source, re.MULTILINE)
        for imp in import_lines:
            root = imp.split(".")[0]
            self.assertIn(
                root,
                allowed | {"orchestration_engine"},
                f"Unexpected import of {imp!r}",
            )


# ===========================================================================
# AC-2: Data structures
# ===========================================================================


class TestDataStructures(unittest.TestCase):
    """AC-2 — FileBlock and ParsedOutput shape."""

    def test_file_block_fields(self) -> None:
        fb = FileBlock(path="src/foo.py", content="print('hi')\n")
        self.assertEqual(fb.path, "src/foo.py")
        self.assertEqual(fb.content, "print('hi')\n")

    def test_file_block_empty_content_allowed(self) -> None:
        fb = FileBlock(path="empty.py", content="")
        self.assertEqual(fb.content, "")

    def test_parsed_output_fields(self) -> None:
        raw = "hello world"
        po = ParsedOutput(raw_text=raw, files=[])
        self.assertEqual(po.raw_text, raw)
        self.assertEqual(po.files, [])
        self.assertFalse(po.has_files)

    def test_has_files_true_when_files_present(self) -> None:
        fb = FileBlock(path="x.py", content="")
        po = ParsedOutput(raw_text="", files=[fb])
        self.assertTrue(po.has_files)

    def test_has_files_false_when_no_files(self) -> None:
        po = ParsedOutput(raw_text="", files=[])
        self.assertFalse(po.has_files)

    def test_has_files_consistent_with_len_files(self) -> None:
        """has_files is always bool(len(files) > 0) — never out of sync."""
        for n in [0, 1, 2, 5]:
            files = [FileBlock(path=f"{i}.py", content="") for i in range(n)]
            po = ParsedOutput(raw_text="", files=files)
            self.assertEqual(po.has_files, len(files) > 0)

    def test_raw_text_exact_identity(self) -> None:
        raw = "  hello\nworld\t"
        po = parse_output(raw)
        self.assertIs(type(po.raw_text), str)
        self.assertEqual(po.raw_text.encode(), raw.encode())

    def test_parsed_output_files_is_list(self) -> None:
        result = parse_output("")
        self.assertIsInstance(result.files, list)

    def test_file_block_is_dataclass(self) -> None:
        """FileBlock supports equality comparison (dataclass default)."""
        fb1 = FileBlock(path="a.py", content="x")
        fb2 = FileBlock(path="a.py", content="x")
        self.assertEqual(fb1, fb2)


# ===========================================================================
# AC-3: Parser function signature / graceful degradation
# ===========================================================================


class TestParserSignature(unittest.TestCase):
    """AC-3 — parse_output never raises, always returns ParsedOutput."""

    def test_returns_parsed_output_type(self) -> None:
        result = parse_output("anything")
        self.assertIsInstance(result, ParsedOutput)

    def test_empty_string(self) -> None:
        result = parse_output("")
        self.assertIsInstance(result, ParsedOutput)
        self.assertEqual(result.raw_text, "")
        self.assertEqual(result.files, [])
        self.assertFalse(result.has_files)

    def test_non_string_int_does_not_raise(self) -> None:
        result = parse_output(42)  # type: ignore[arg-type]
        self.assertIsInstance(result, ParsedOutput)

    def test_non_string_float_does_not_raise(self) -> None:
        result = parse_output(3.14)  # type: ignore[arg-type]
        self.assertIsInstance(result, ParsedOutput)

    def test_none_like_does_not_raise(self) -> None:
        result = parse_output(None)  # type: ignore[arg-type]
        self.assertIsInstance(result, ParsedOutput)

    def test_list_does_not_raise(self) -> None:
        result = parse_output(["a", "b"])  # type: ignore[arg-type]
        self.assertIsInstance(result, ParsedOutput)

    def test_binary_gibberish_does_not_raise(self) -> None:
        result = parse_output("\x00\xff\xfe broken stuff")
        self.assertIsInstance(result, ParsedOutput)

    def test_very_long_string_does_not_raise(self) -> None:
        result = parse_output("x" * 1_000_000)
        self.assertIsInstance(result, ParsedOutput)

    def test_only_newlines_does_not_raise(self) -> None:
        result = parse_output("\n" * 1000)
        self.assertIsInstance(result, ParsedOutput)
        self.assertEqual(result.files, [])

    def test_unicode_content_does_not_raise(self) -> None:
        result = parse_output("=== FILE: ü.py ===\nöäü\n=== END FILE ===\n")
        self.assertIsInstance(result, ParsedOutput)


# ===========================================================================
# AC-4: Delimiter parsing
# ===========================================================================


class TestDelimiterParsing(unittest.TestCase):
    """AC-4 — FILE / END FILE recognition."""

    def test_basic_single_block(self) -> None:
        text = "=== FILE: foo.py ===\nprint('hi')\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "foo.py")
        self.assertEqual(result.files[0].content, "print('hi')\n")

    def test_path_whitespace_stripped(self) -> None:
        text = "===  FILE:   src/bar.py   ===\nx\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].path, "src/bar.py")

    def test_file_marker_with_leading_whitespace_on_line(self) -> None:
        """Marker may have leading whitespace (not required to be column 0)."""
        text = "   === FILE: a.txt ===\nhello\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "a.txt")

    def test_end_file_flexible_whitespace(self) -> None:
        text = "=== FILE: z.py ===\npass\n   ===   END FILE   ===   \n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)

    def test_raw_text_unmodified_after_parse(self) -> None:
        text = "=== FILE: foo.py ===\nhello\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.raw_text, text)

    def test_path_with_subdirectory(self) -> None:
        text = "=== FILE: src/module/utils.py ===\ndef f(): pass\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].path, "src/module/utils.py")

    def test_delimiter_case_sensitive_lower_case_ignored(self) -> None:
        """Lowercase 'file' does NOT match — markers are uppercase."""
        text = "=== file: foo.py ===\ncontent\n=== end file ===\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])

    def test_end_file_without_space_not_matched(self) -> None:
        """=== ENDFILE === (no space) is not a valid terminator."""
        text = "=== FILE: x.py ===\ncode\n=== ENDFILE ===\n"
        result = parse_output(text)
        # Block never closed → silently dropped
        self.assertEqual(result.files, [])

    def test_single_block_no_trailing_newline(self) -> None:
        """Input without trailing newline after END FILE is still parsed."""
        text = "=== FILE: x.py ===\ncode\n=== END FILE ==="
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "x.py")


# ===========================================================================
# AC-5: Backward compatibility
# ===========================================================================


class TestBackwardCompatibility(unittest.TestCase):
    """AC-5 — no markers → empty files list, raw_text preserved."""

    def test_no_markers_returns_empty_files(self) -> None:
        text = "Just some prose with no file blocks."
        result = parse_output(text)
        self.assertEqual(result.files, [])
        self.assertFalse(result.has_files)
        self.assertEqual(result.raw_text, text)

    def test_empty_string_backward_compat(self) -> None:
        result = parse_output("")
        self.assertEqual(result.raw_text, "")
        self.assertEqual(result.files, [])
        self.assertFalse(result.has_files)

    def test_only_prose_before_block(self) -> None:
        text = "Here is a file:\n=== FILE: x.py ===\ncode\n=== END FILE ===\nAfterword."
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.raw_text, text)

    def test_markdown_table_no_false_positives(self) -> None:
        text = "| col1 | col2 |\n|------|------|\n| a    | b    |\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])

    def test_code_fence_no_false_positives(self) -> None:
        text = "```python\nprint('hi')\n```\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])


# ===========================================================================
# AC-6: Path traversal protection
# ===========================================================================


class TestPathTraversalProtection(unittest.TestCase):
    """AC-6 — all unsafe paths are rejected, safe paths are accepted."""

    # ── Traversal via .. ────────────────────────────────────────────────────

    def test_reject_dotdot_at_start(self) -> None:
        _assert_path_rejected(self, "../etc/passwd")

    def test_reject_dotdot_in_middle(self) -> None:
        _assert_path_rejected(self, "foo/../bar")

    def test_reject_dotdot_standalone(self) -> None:
        _assert_path_rejected(self, "..")

    def test_reject_dotdot_deep(self) -> None:
        _assert_path_rejected(self, "a/b/../../etc/passwd")

    def test_reject_dotdot_triple(self) -> None:
        _assert_path_rejected(self, "../../../root/.ssh/id_rsa")

    # ── Absolute Unix paths ─────────────────────────────────────────────────

    def test_reject_absolute_unix_root(self) -> None:
        _assert_path_rejected(self, "/etc/passwd")

    def test_reject_absolute_unix_deep(self) -> None:
        _assert_path_rejected(self, "/home/user/.bashrc")

    def test_reject_absolute_unix_single_slash(self) -> None:
        _assert_path_rejected(self, "/")

    # ── Windows paths ───────────────────────────────────────────────────────

    def test_reject_windows_drive_forward_slash(self) -> None:
        _assert_path_rejected(self, "C:/Windows/System32")

    def test_reject_windows_drive_backslash(self) -> None:
        _assert_path_rejected(self, "C:\\Windows\\System32")

    def test_reject_windows_drive_lower_case(self) -> None:
        _assert_path_rejected(self, "c:/bad/path")

    def test_reject_windows_drive_z(self) -> None:
        _assert_path_rejected(self, "Z:/secret")

    def test_reject_windows_drive_colon_only(self) -> None:
        _assert_path_rejected(self, "C:")

    # ── Backslash paths ─────────────────────────────────────────────────────

    def test_reject_backslash_separator(self) -> None:
        _assert_path_rejected(self, "foo\\bar")

    def test_reject_backslash_unc(self) -> None:
        _assert_path_rejected(self, "\\\\server\\share")

    # ── UNC paths ───────────────────────────────────────────────────────────

    def test_reject_unc_double_forward_slash(self) -> None:
        _assert_path_rejected(self, "//server/share/file.txt")

    def test_reject_unc_double_slash_short(self) -> None:
        _assert_path_rejected(self, "//evil")

    # ── URL-encoded traversal ────────────────────────────────────────────────

    def test_reject_url_encoded_slash_uppercase(self) -> None:
        _assert_path_rejected(self, "..%2Fetc%2Fpasswd")

    def test_reject_url_encoded_slash_lowercase(self) -> None:
        _assert_path_rejected(self, "..%2fetc")

    def test_reject_url_encoded_dot_uppercase(self) -> None:
        _assert_path_rejected(self, "%2E%2E/etc/passwd")

    def test_reject_url_encoded_dot_lowercase(self) -> None:
        _assert_path_rejected(self, "%2e%2e/etc/passwd")

    def test_reject_url_encoded_mixed_case(self) -> None:
        _assert_path_rejected(self, "..%2Fetc")

    # ── Empty / whitespace paths ─────────────────────────────────────────────

    def test_reject_empty_path_from_marker(self) -> None:
        """=== FILE:  === (whitespace-only path) is rejected."""
        text = "=== FILE:  ===\ncontent\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])

    def test_reject_single_dot_path(self) -> None:
        """'.' resolves to output_dir itself — rejected to prevent IsADirectoryError."""
        _assert_path_rejected(self, ".")

    # ── Safe paths accepted ──────────────────────────────────────────────────

    def test_accept_simple_filename(self) -> None:
        text = _make_block("README.md", "# Hello\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "README.md")

    def test_accept_safe_relative_path(self) -> None:
        text = _make_block("src/module/helper.py", "pass\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "src/module/helper.py")

    def test_accept_hidden_file(self) -> None:
        text = _make_block(".gitignore", "*.pyc\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)

    def test_accept_path_with_dots_in_name(self) -> None:
        """Dots in filenames (e.g. setup.cfg) are not traversal."""
        text = _make_block("setup.cfg", "[tool]\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)

    def test_accepted_path_not_in_rejected_list(self) -> None:
        """Content of a rejected block must not appear in any FileBlock."""
        text = "=== FILE: ../secret ===\nSECRET_DATA\n=== END FILE ===\n"
        result = parse_output(text)
        for fb in result.files:
            self.assertNotIn("SECRET_DATA", fb.content)


# ===========================================================================
# AC-7: Edge cases
# ===========================================================================


class TestEdgeCases(unittest.TestCase):
    """AC-7 — all specified edge cases."""

    def test_empty_file_block_content_is_empty_string(self) -> None:
        text = "=== FILE: empty.py ===\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].content, "")

    def test_multiple_file_blocks_in_order(self) -> None:
        text = (
            "=== FILE: a.py ===\nAAA\n=== END FILE ===\n"
            "=== FILE: b.py ===\nBBB\n=== END FILE ===\n"
            "=== FILE: c.py ===\nCCC\n=== END FILE ===\n"
        )
        result = parse_output(text)
        self.assertEqual(len(result.files), 3)
        self.assertEqual([fb.path for fb in result.files], ["a.py", "b.py", "c.py"])
        self.assertEqual(result.files[0].content, "AAA\n")
        self.assertEqual(result.files[1].content, "BBB\n")
        self.assertEqual(result.files[2].content, "CCC\n")

    def test_nested_code_fences_preserved_verbatim(self) -> None:
        content = "```python\nprint('hello')\n```\n"
        text = f"=== FILE: demo.md ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].content, content)

    def test_nested_file_marker_inside_block_is_literal_content(self) -> None:
        """FILE marker appearing inside a block is treated as literal content."""
        inner_marker = "=== FILE: inner.py ===\n"
        text = (
            "=== FILE: outer.py ===\n"
            f"{inner_marker}"
            "some code\n"
            "=== END FILE ===\n"
        )
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "outer.py")
        self.assertIn(inner_marker, result.files[0].content)

    def test_unclosed_block_silently_dropped(self) -> None:
        text = "=== FILE: orphan.py ===\nsome content\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])
        self.assertFalse(result.has_files)

    def test_unclosed_block_no_partial_content_leaked(self) -> None:
        text = "=== FILE: unclosed.py ===\nSECRET_PARTIAL_DATA\n"
        result = parse_output(text)
        for fb in result.files:
            self.assertNotIn("SECRET_PARTIAL_DATA", fb.content)

    def test_duplicate_paths_both_included(self) -> None:
        """Documented policy: both occurrences included; caller deduplicates."""
        text = (
            "=== FILE: dup.py ===\nversion 1\n=== END FILE ===\n"
            "=== FILE: dup.py ===\nversion 2\n=== END FILE ===\n"
        )
        result = parse_output(text)
        self.assertEqual(len(result.files), 2)
        self.assertEqual(result.files[0].content, "version 1\n")
        self.assertEqual(result.files[1].content, "version 2\n")

    def test_whitespace_only_content_preserved_exactly(self) -> None:
        content = "   \n\t\n   \n"
        text = f"=== FILE: ws.py ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].content, content)

    def test_extra_spaces_in_file_marker(self) -> None:
        """===  FILE:  path.py  === with extra internal spaces still matches."""
        text = "===  FILE:  src/foo.py  ===\ncode\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "src/foo.py")

    def test_content_with_blank_lines_preserved(self) -> None:
        content = "\nline1\n\nline2\n\n"
        text = f"=== FILE: spaced.py ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].content, content)

    def test_large_content_block(self) -> None:
        content = "x = 1\n" * 10_000
        text = f"=== FILE: big.py ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].content, content)

    def test_prose_before_and_after_block(self) -> None:
        text = (
            "Here is some prose.\n"
            "=== FILE: mid.py ===\ncode\n=== END FILE ===\n"
            "More prose after.\n"
        )
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].content, "code\n")

    def test_end_file_marker_must_be_on_its_own_line(self) -> None:
        """END FILE concatenated with content on same line is not recognised.

        The whole line becomes content, block is never closed → silently dropped.
        """
        text = "=== FILE: nonl.py ===\nno newline at end=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])

    def test_content_trailing_newline_preserved(self) -> None:
        """The newline preceding END FILE is part of the content."""
        text = "=== FILE: trail.py ===\nlast line\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].content, "last line\n")

    def test_crlf_line_endings_in_text(self) -> None:
        """CRLF-terminated input is handled — splitlines normalises it."""
        text = "=== FILE: win.py ===\r\nprint(1)\r\n=== END FILE ===\r\n"
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "win.py")

    def test_mixed_valid_invalid_blocks(self) -> None:
        """Valid blocks survive even when surrounded by invalid-path blocks."""
        text = (
            "=== FILE: ../bad.py ===\nevil\n=== END FILE ===\n"
            "=== FILE: good.py ===\nclean\n=== END FILE ===\n"
            "=== FILE: /abs/bad ===\nalso evil\n=== END FILE ===\n"
        )
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "good.py")
        self.assertEqual(result.files[0].content, "clean\n")

    def test_five_blocks_all_extracted(self) -> None:
        blocks = [_make_block(f"file{i}.py", f"content{i}\n") for i in range(5)]
        text = "".join(blocks)
        result = parse_output(text)
        self.assertEqual(len(result.files), 5)
        for i, fb in enumerate(result.files):
            self.assertEqual(fb.path, f"file{i}.py")
            self.assertEqual(fb.content, f"content{i}\n")

    def test_content_no_leading_stripping(self) -> None:
        """Leading whitespace in content is preserved, not stripped."""
        content = "    indented line\n"
        text = f"=== FILE: indent.py ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].content, content)

    def test_content_no_trailing_stripping(self) -> None:
        """Trailing whitespace in content is preserved, not stripped."""
        content = "trailing spaces   \n"
        text = f"=== FILE: trail.py ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].content, content)


# ===========================================================================
# AC-8: extract_and_write — happy paths
# ===========================================================================


class TestExtractAndWrite(unittest.TestCase):
    """AC-8 — extract_and_write writes files to disk correctly."""

    def test_writes_single_file(self) -> None:
        text = "=== FILE: hello.txt ===\nHello, World!\n=== END FILE ===\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write(text, out)
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].path, "hello.txt")
            self.assertEqual((out / "hello.txt").read_text(encoding="utf-8"), "Hello, World!\n")

    def test_creates_output_dir_if_missing(self) -> None:
        text = _make_block("a.py", "pass\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "does" / "not" / "exist"
            self.assertFalse(out.exists())
            written = extract_and_write(text, out)
            self.assertTrue(out.exists())
            self.assertEqual(len(written), 1)

    def test_creates_parent_dirs_for_nested_file(self) -> None:
        text = _make_block("src/module/helper.py", "# helper\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write(text, out)
            self.assertEqual(len(written), 1)
            target = out / "src" / "module" / "helper.py"
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "# helper\n")

    def test_returns_only_successfully_written_blocks(self) -> None:
        text = (
            _make_block("good.py", "ok\n")
            + _make_block("also_good.py", "also ok\n")
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write(text, out)
            self.assertEqual(len(written), 2)

    def test_no_files_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write("just prose", out)
            self.assertEqual(written, [])

    def test_multiple_files_written_in_order(self) -> None:
        text = _make_block("a.txt", "AAA\n") + _make_block("b.txt", "BBB\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write(text, out)
            self.assertEqual(len(written), 2)
            self.assertEqual((out / "a.txt").read_text(encoding="utf-8"), "AAA\n")
            self.assertEqual((out / "b.txt").read_text(encoding="utf-8"), "BBB\n")

    def test_does_not_write_traversal_paths(self) -> None:
        """Traversal paths are filtered by parse_output, not written."""
        text = "=== FILE: ../escape.py ===\nevil\n=== END FILE ===\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write(text, out)
            self.assertEqual(written, [])
            escape = Path(tmpdir) / "escape.py"
            self.assertFalse(escape.exists())

    def test_does_not_raise_on_empty_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            result = extract_and_write("", out)
            self.assertEqual(result, [])

    def test_file_content_preserved_exactly(self) -> None:
        content = "line1\n\nline3\n\ttabbed\n"
        text = f"=== FILE: exact.py ===\n{content}=== END FILE ===\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            extract_and_write(text, out)
            self.assertEqual(
                (out / "exact.py").read_text(encoding="utf-8"), content
            )

    def test_accepts_string_output_dir(self) -> None:
        """output_dir accepts str (not just Path)."""
        text = _make_block("str_dir.py", "x\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            out_str = str(Path(tmpdir) / "str_out")
            written = extract_and_write(text, out_str)
            self.assertEqual(len(written), 1)

    def test_accepts_path_output_dir(self) -> None:
        """output_dir accepts pathlib.Path."""
        text = _make_block("path_dir.py", "y\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "path_out"
            written = extract_and_write(text, out)
            self.assertEqual(len(written), 1)

    def test_returned_file_blocks_have_correct_content(self) -> None:
        """Returned FileBlocks match what was actually written."""
        content = "written content\n"
        text = _make_block("check.py", content)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            written = extract_and_write(text, out)
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].content, content)
            self.assertEqual(written[0].path, "check.py")

    def test_unicode_content_written_correctly(self) -> None:
        content = "# Ünïcödé\nprint('héllo')\n"
        text = _make_block("unicode.py", content)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            extract_and_write(text, out)
            self.assertEqual(
                (out / "unicode.py").read_text(encoding="utf-8"), content
            )


# ===========================================================================
# AC-8: extract_and_write — error paths (branch coverage)
# ===========================================================================


class TestExtractAndWriteErrors(unittest.TestCase):
    """AC-8 error paths — mkdir failure, write failure, resolution bypass."""

    def test_mkdir_fails_returns_empty_list(self) -> None:
        """If output_dir cannot be created (e.g. it exists as a file), return []."""
        text = _make_block("hello.txt", "Hello\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a regular FILE at the output_dir path to force mkdir() to fail
            out = Path(tmpdir) / "output"
            out.touch()  # now it's a file, mkdir will raise FileExistsError
            result = extract_and_write(text, out)
            self.assertEqual(result, [])

    def test_write_failure_skips_block_does_not_raise(self) -> None:
        """If write_text fails, the block is skipped and no exception propagates."""
        text = _make_block("fail.txt", "data\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            # Patch Path.write_text to simulate disk-full / permission error
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                result = extract_and_write(text, out)
            # Skipped — not raised
            self.assertEqual(result, [])

    def test_write_failure_partial_success(self) -> None:
        """Only the successfully written blocks are returned."""
        text = _make_block("ok.txt", "good\n") + _make_block("fail.txt", "bad\n")

        original_write_text = Path.write_text
        call_count = {"n": 0}

        def patched_write_text(self_path, content, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return original_write_text(self_path, content, **kwargs)
            raise OSError("disk full on second write")

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "output"
            with patch.object(Path, "write_text", patched_write_text):
                result = extract_and_write(text, out)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].path, "ok.txt")

    def test_resolve_outside_output_dir_skipped(self) -> None:
        """A symlink pointing outside output_dir is detected and skipped.

        This exercises the resolve()/relative_to() secondary safety check in
        extract_and_write — an attacker who bypasses parse_output's path filter
        should still be blocked at the write step.
        """
        # NOTE: parse_output already rejects paths with '..', so we cannot
        # construct this scenario through normal input.  We test the resolution
        # check directly by injecting a pre-built FileBlock via mocking
        # parse_output's return value.
        evil_fb = FileBlock(path="legit_name.txt", content="evil")
        fake_parsed = ParsedOutput(raw_text="", files=[evil_fb])

        with patch(
            "orchestration_engine.output_parser.parse_output", return_value=fake_parsed
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "output"
                out.mkdir()
                # Create a symlink inside out that points outside tmpdir
                link_target = Path(tmpdir) / "outside.txt"
                link_target.write_text("should not be overwritten")
                link_path = out / "legit_name.txt"
                link_path.symlink_to(link_target)

                result = extract_and_write("", out)
                # The symlink resolves outside output_dir → block skipped
                # (resolved_target.relative_to(resolved_root) raises ValueError)
                self.assertEqual(result, [])
                # Ensure the external file was NOT overwritten
                self.assertEqual(link_target.read_text(), "should not be overwritten")


# ===========================================================================
# AC-9 / robustness: extra correctness and security checks
# ===========================================================================


class TestRobustness(unittest.TestCase):
    """Extra robustness / security checks beyond explicit ACs."""

    def test_only_end_file_no_start_is_ignored(self) -> None:
        text = "=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files, [])

    def test_multiple_end_file_markers_first_closes_block(self) -> None:
        """First END FILE closes the block; the second is a stray no-op."""
        text = (
            "=== FILE: x.py ===\nline\n"
            "=== END FILE ===\n"
            "=== END FILE ===\n"
        )
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].content, "line\n")

    def test_deeply_nested_safe_relative_path(self) -> None:
        text = _make_block("a/b/c/d/e/f.py", "deep\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "a/b/c/d/e/f.py")

    def test_path_with_dots_in_filename_accepted(self) -> None:
        text = _make_block("setup.cfg", "[tool]\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)

    def test_path_with_version_dots_accepted(self) -> None:
        text = _make_block("mylib-1.0.2/core.py", "code\n")
        result = parse_output(text)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "mylib-1.0.2/core.py")

    def test_has_files_always_consistent_with_files_list(self) -> None:
        for text in [
            "no blocks",
            "=== FILE: x.py ===\n\n=== END FILE ===\n",
            "",
        ]:
            result = parse_output(text)
            self.assertEqual(result.has_files, bool(result.files))

    def test_raw_text_byte_identical_to_input(self) -> None:
        raw = "abc\ndef\r\nghi\x00\n"
        result = parse_output(raw)
        self.assertEqual(
            result.raw_text.encode("utf-8", errors="replace"),
            raw.encode("utf-8", errors="replace"),
        )

    def test_url_encoded_all_variants_rejected(self) -> None:
        """All case variants of URL-encoded traversal must be rejected."""
        for path in ["..%2Fetc", "%2E%2E/etc", "..%2fetc", "%2e%2e/etc"]:
            text = f"=== FILE: {path} ===\nevil\n=== END FILE ===\n"
            result = parse_output(text)
            self.assertEqual(result.files, [], f"Expected {path!r} to be rejected")

    def test_windows_drive_lower_case_rejected(self) -> None:
        _assert_path_rejected(self, "c:/bad")

    def test_path_injection_via_null_byte(self) -> None:
        """Null bytes in path — no exception is raised regardless of outcome."""
        text = "=== FILE: foo\x00bar.py ===\ndata\n=== END FILE ===\n"
        result = parse_output(text)
        self.assertIsInstance(result, ParsedOutput)

    def test_many_invalid_then_one_valid(self) -> None:
        """One valid block after many invalid ones is correctly extracted."""
        invalids = "".join(
            f"=== FILE: {p} ===\nX\n=== END FILE ===\n"
            for p in ["../a", "/b", "C:/c", "//d", "..%2Fe"]
        )
        valid = _make_block("final.py", "success\n")
        result = parse_output(invalids + valid)
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, "final.py")
        self.assertEqual(result.files[0].content, "success\n")

    def test_content_contains_triple_backticks_preserved(self) -> None:
        """Triple backtick fences inside a file block are literal content."""
        content = '```python\nprint("nested")\n```\n'
        text = f"=== FILE: readme.md ===\n{content}=== END FILE ===\n"
        result = parse_output(text)
        self.assertEqual(result.files[0].content, content)

    def test_adjacent_file_blocks_no_bleed(self) -> None:
        """Content of block N must not bleed into block N+1."""
        text = (
            "=== FILE: a.py ===\nONLY_A\n=== END FILE ===\n"
            "=== FILE: b.py ===\nONLY_B\n=== END FILE ===\n"
        )
        result = parse_output(text)
        self.assertNotIn("ONLY_A", result.files[1].content)
        self.assertNotIn("ONLY_B", result.files[0].content)


# ===========================================================================
# Internal branch coverage — exception paths in _is_safe_path / parse_output
# ===========================================================================


class TestInternalBranchCoverage(unittest.TestCase):
    """Cover the exception-handling branches that are unreachable via normal input.

    These tests use unittest.mock to inject failures into normally-infallible
    operations (PurePosixPath construction, str() coercion) to ensure the
    graceful-degradation branches are exercised.
    """

    def test_is_safe_path_pure_posix_path_exception_returns_false(self) -> None:
        """If PurePosixPath() raises unexpectedly, _is_safe_path returns False."""
        # We need to pass something that passes all earlier checks (not empty,
        # no backslash, no //, no leading /, no drive letter, no URL-encoded)
        # but causes PurePosixPath to throw.
        with patch(
            "orchestration_engine.output_parser.PurePosixPath",
            side_effect=ValueError("mock failure"),
        ):
            result = _is_safe_path("seemingly_safe_path.py")
        self.assertFalse(result)

    def test_parse_output_str_coercion_exception_uses_empty_string(self) -> None:
        """If str(non_string_input) raises, parse_output uses '' and returns cleanly."""

        class UnStringable:
            def __str__(self):
                raise RuntimeError("cannot stringify me")

        result = parse_output(UnStringable())  # type: ignore[arg-type]
        self.assertIsInstance(result, ParsedOutput)
        # raw_text should be "" (the fallback)
        self.assertEqual(result.raw_text, "")
        self.assertEqual(result.files, [])

    def test_is_safe_path_called_directly_with_empty_string(self) -> None:
        """_is_safe_path('') returns False."""
        self.assertFalse(_is_safe_path(""))

    def test_is_safe_path_called_directly_with_dotdot(self) -> None:
        self.assertFalse(_is_safe_path(".."))

    def test_is_safe_path_called_directly_with_safe_path(self) -> None:
        self.assertTrue(_is_safe_path("src/module.py"))

    def test_is_safe_path_called_directly_with_absolute(self) -> None:
        self.assertFalse(_is_safe_path("/etc/passwd"))

    def test_is_safe_path_called_directly_with_unc(self) -> None:
        self.assertFalse(_is_safe_path("//server/share"))

    def test_is_safe_path_called_directly_with_url_encoded(self) -> None:
        self.assertFalse(_is_safe_path("..%2Fetc"))

    def test_is_safe_path_called_directly_with_backslash(self) -> None:
        self.assertFalse(_is_safe_path("foo\\bar"))

    def test_is_safe_path_called_directly_with_dot(self) -> None:
        self.assertFalse(_is_safe_path("."))

    def test_is_safe_path_called_directly_with_windows_drive(self) -> None:
        self.assertFalse(_is_safe_path("C:/windows"))

    def test_is_safe_path_whitespace_only_returns_false(self) -> None:
        self.assertFalse(_is_safe_path("   "))


# ===========================================================================
# Entry point for running directly
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
