"""
tests/test_cli_overwrite.py — Tests for Issue #210: CLI overwrites agent-written files

Covers the fix in cli.py where, before writing captured phase output to disk,
we check whether the sub-agent already wrote a larger file and, if so, keep it.

Two code paths are tested (both delegate to the shared helper):
    Location 1: _on_phase_complete callback (per-phase write during execution)
    Location 2: Final summary loop (batch write at pipeline end)

The helper under test is `_safe_write_phase_output` from cli.py — this is the
*real* function used in production, not a copy-pasted stand-in.

Key behaviours:
  - If the existing file is STRICTLY LARGER (in UTF-8 bytes) than new_content,
    keep the existing file (agent wrote something better).
  - Otherwise (no file, equal size, or new_content is larger) → write new_content.
  - The byte comparison uses encode('utf-8') so multi-byte chars are counted correctly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the *real* helper from cli.py
# ---------------------------------------------------------------------------
from orchestration_engine.cli import _safe_write_phase_output


def _make_path(tmp_path: Path, phase_id: str) -> Path:
    """Return the expected output path for a given phase_id."""
    safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
    return tmp_path / f"{safe_pid}.md"


def _new_content(phase_id: str, phase_text: str) -> str:
    """Reproduce the content string that cli.py passes to the helper."""
    return f"# Phase: {phase_id}\n\n{phase_text}\n"


# ---------------------------------------------------------------------------
# Test 1: CLI preserves the larger agent-written file
# ---------------------------------------------------------------------------

class TestCliPreservesLargerAgentFile:
    """When the sub-agent has already written a large file, the CLI must not
    overwrite it with the (smaller) captured output.  Issue #210 scenario."""

    def test_cli_preserves_larger_agent_file(self, tmp_path):
        """Large agent file → captured output smaller → agent file kept."""
        phase_id = "research"
        out_path = _make_path(tmp_path, phase_id)

        # Simulate the sub-agent writing a full ~3000-word article (~18 000 chars)
        agent_content = "Agent output: " + ("word " * 3000)
        out_path.write_text(agent_content)
        agent_size = out_path.stat().st_size

        # CLI captures only a short summary (~670 words / ~4000 chars)
        captured_text = "Summary: " + ("word " * 670)
        new_content = _new_content(phase_id, captured_text)

        _safe_write_phase_output(out_path, new_content, phase_id)

        # File on disk must still be the agent's version
        result = out_path.read_text()
        assert result == agent_content, (
            f"Expected agent-written content ({agent_size} bytes) to be preserved, "
            f"but got {len(result)} bytes"
        )

    def test_cli_preserves_larger_file_regardless_of_content(self, tmp_path):
        """The size-based guard must fire for any content where agent file > capture."""
        phase_id = "write_phase"
        out_path = _make_path(tmp_path, phase_id)

        large_agent_content = "x" * 50_000  # 50 KB
        out_path.write_text(large_agent_content)

        small_captured_text = "tiny"
        new_content = _new_content(phase_id, small_captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.read_text() == large_agent_content

    def test_cli_logs_info_when_keeping_agent_file(self, tmp_path, caplog):
        """An INFO log must be emitted when the agent file is kept."""
        phase_id = "research"
        out_path = _make_path(tmp_path, phase_id)

        out_path.write_text("A" * 10_000)  # large agent file

        new_content = _new_content(phase_id, "tiny captured text")
        with caplog.at_level(logging.INFO):
            _safe_write_phase_output(out_path, new_content, phase_id)

        log_messages = " ".join(r.message for r in caplog.records)
        assert "keeping agent-written file" in log_messages, (
            "Expected an INFO log mentioning 'keeping agent-written file'"
        )

    def test_cli_preserves_file_even_when_content_differs(self, tmp_path):
        """Content comparison is size-only; the guard must not require identical bytes."""
        phase_id = "factcheck"
        out_path = _make_path(tmp_path, phase_id)

        agent_content = "Completely different content from agent " + "x" * 5_000
        out_path.write_text(agent_content)

        # Captured output has different content but is shorter
        captured_text = "Captured: different text but shorter"
        new_content = _new_content(phase_id, captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.read_text() == agent_content

    def test_phase_id_with_special_chars_preserved(self, tmp_path):
        """Phase IDs with special chars are sanitised; agent file still kept."""
        phase_id = "phase/with spaces"
        out_path = _make_path(tmp_path, phase_id)

        agent_content = "Large agent output " + "y" * 8_000
        out_path.write_text(agent_content)

        new_content = _new_content(phase_id, "short capture")
        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.read_text() == agent_content

    def test_multibyte_chars_bytes_comparison(self, tmp_path):
        """UTF-8 multi-byte chars: size guard must use byte length, not char count.

        An em-dash (—) is 3 bytes in UTF-8 but 1 Python char.  If the guard
        compared st_size against len(str) instead of len(str.encode('utf-8')),
        it would get a wrong answer for files containing such characters.
        """
        phase_id = "unicode_phase"
        out_path = _make_path(tmp_path, phase_id)

        # Agent writes a file full of em-dashes (3 bytes each in UTF-8)
        # 200 em-dashes = 600 bytes on disk but only 200 Python chars
        agent_content = "—" * 200
        out_path.write_bytes(agent_content.encode("utf-8"))

        # new_content that is shorter in bytes than agent_content
        # If the guard wrongly compared st_size (600) > len(str) (len of new_content chars)
        # it might make the wrong decision for borderline cases.
        # Here we make new_content clearly shorter in bytes to confirm the guard works.
        captured_text = "x" * 10
        new_content = _new_content(phase_id, captured_text)
        # new_content in bytes is much less than 600 bytes, so agent file should be kept
        assert len(new_content.encode("utf-8")) < out_path.stat().st_size

        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.read_bytes() == agent_content.encode("utf-8"), (
            "Agent file with multi-byte chars should be preserved when it is larger in bytes"
        )


# ---------------------------------------------------------------------------
# Test 2: CLI writes normally when no existing file
# ---------------------------------------------------------------------------

class TestCliOverwritesWhenNoExistingFile:
    """When no file exists at the output path, the CLI should write normally."""

    def test_cli_overwrites_when_no_existing_file(self, tmp_path):
        """No pre-existing file → CLI writes the captured output."""
        phase_id = "research"
        out_path = _make_path(tmp_path, phase_id)

        assert not out_path.exists(), "Pre-condition: file must not exist"

        captured_text = "Captured agent output: " + ("word " * 200)
        new_content = _new_content(phase_id, captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.exists(), "CLI must create the file when none exists"
        written = out_path.read_text()
        assert f"# Phase: {phase_id}" in written
        assert captured_text in written

    def test_cli_creates_correct_file_name(self, tmp_path):
        """Output file name must match the sanitised phase_id."""
        phase_id = "my phase"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        expected_path = tmp_path / f"{safe_pid}.md"

        new_content = _new_content(phase_id, "some text")
        _safe_write_phase_output(expected_path, new_content, phase_id)

        assert expected_path.exists()

    def test_cli_writes_correct_header_format(self, tmp_path):
        """Written file must start with '# Phase: {phase_id}'."""
        phase_id = "build"
        out_path = _make_path(tmp_path, phase_id)

        new_content = _new_content(phase_id, "Build output here.")
        _safe_write_phase_output(out_path, new_content, phase_id)

        content = out_path.read_text()
        assert content.startswith(f"# Phase: {phase_id}\n")

    def test_cli_writes_phase_text_in_body(self, tmp_path):
        """The captured phase text must appear in the written file body."""
        phase_id = "analysis"
        captured_text = "This is the analysis output."
        out_path = _make_path(tmp_path, phase_id)

        new_content = _new_content(phase_id, captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        content = out_path.read_text()
        assert captured_text in content

    def test_cli_no_log_when_file_absent(self, tmp_path, caplog):
        """No 'keeping agent-written file' log when the file doesn't exist."""
        phase_id = "research"
        out_path = _make_path(tmp_path, phase_id)

        new_content = _new_content(phase_id, "some content")
        with caplog.at_level(logging.INFO):
            _safe_write_phase_output(out_path, new_content, phase_id)

        for record in caplog.records:
            assert "keeping agent-written file" not in record.message


# ---------------------------------------------------------------------------
# Test 3: CLI overwrites when captured output is larger
# ---------------------------------------------------------------------------

class TestCliOverwritesWhenCaptureIsLarger:
    """When the CLI's captured output is larger than the existing file
    (or equal in size), the CLI must overwrite it with the fresh capture."""

    def test_cli_overwrites_when_capture_is_larger(self, tmp_path):
        """Existing file smaller than captured output → CLI overwrites."""
        phase_id = "research"
        out_path = _make_path(tmp_path, phase_id)

        # Existing file is a short stub (e.g. agent wrote a placeholder)
        out_path.write_text("short stub")

        # CLI captures the full, longer output
        captured_text = "Full captured output: " + ("word " * 500)
        new_content = _new_content(phase_id, captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        written = out_path.read_text()
        assert captured_text in written, (
            "CLI should overwrite the smaller existing file with the larger capture"
        )

    def test_cli_overwrites_when_existing_file_is_empty(self, tmp_path):
        """An empty existing file should always be overwritten."""
        phase_id = "write"
        out_path = _make_path(tmp_path, phase_id)

        out_path.write_text("")  # empty file

        captured_text = "Some captured content"
        new_content = _new_content(phase_id, captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        assert captured_text in out_path.read_text()

    def test_cli_overwrites_when_sizes_equal(self, tmp_path):
        """When sizes are identical, the CLI should overwrite (no special-casing)."""
        phase_id = "qa"
        out_path = _make_path(tmp_path, phase_id)

        # Write a file with content that will be exactly the same size as new_content
        phase_text = "equal size content"
        new_content = _new_content(phase_id, phase_text)
        # Write exactly the same bytes
        out_path.write_text(new_content)

        # Re-apply with same phase_text — same size → should overwrite (guard is STRICTLY >)
        _safe_write_phase_output(out_path, new_content, phase_id)

        # Content should be the formatted version
        assert out_path.read_text() == new_content

    def test_cli_no_keep_log_when_overwriting(self, tmp_path, caplog):
        """No 'keeping agent-written file' log when CLI overwrites."""
        phase_id = "research"
        out_path = _make_path(tmp_path, phase_id)

        out_path.write_text("tiny")

        new_content = _new_content(phase_id, "much larger captured output here " * 50)
        with caplog.at_level(logging.INFO):
            _safe_write_phase_output(out_path, new_content, phase_id)

        for record in caplog.records:
            assert "keeping agent-written file" not in record.message

    def test_cli_overwrites_with_correct_content_format(self, tmp_path):
        """Overwritten file must use the '# Phase: {id}\\n\\n{text}\\n' format."""
        phase_id = "summary"
        out_path = _make_path(tmp_path, phase_id)

        out_path.write_text("old small content")

        captured_text = "New large captured content " + "x" * 1000
        new_content = _new_content(phase_id, captured_text)
        _safe_write_phase_output(out_path, new_content, phase_id)

        content = out_path.read_text()
        assert content == new_content

    def test_cli_boundary_one_byte_larger_keeps_agent_file(self, tmp_path):
        """Agent file exactly 1 byte larger than new_content → must be kept."""
        phase_id = "delta"
        out_path = _make_path(tmp_path, phase_id)

        captured_text = "X" * 100
        new_content = _new_content(phase_id, captured_text)
        # Agent file is 1 byte larger
        agent_content = new_content + "Y"
        out_path.write_text(agent_content)

        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.read_text() == agent_content

    def test_cli_boundary_one_byte_smaller_overwrites(self, tmp_path):
        """Agent file exactly 1 byte smaller than new_content → must be overwritten."""
        phase_id = "delta"
        out_path = _make_path(tmp_path, phase_id)

        captured_text = "X" * 100
        new_content = _new_content(phase_id, captured_text)
        # Agent file is 1 byte smaller
        agent_content = new_content[:-1]
        out_path.write_text(agent_content)

        _safe_write_phase_output(out_path, new_content, phase_id)

        assert out_path.read_text() == new_content
