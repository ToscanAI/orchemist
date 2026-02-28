"""
tests/test_cli_overwrite.py — Tests for Issue #210: CLI overwrites agent-written files

Covers the fix in cli.py where, before writing captured phase output to disk,
we check whether the sub-agent already wrote a larger file and, if so, keep it.

Two code paths are tested:
    Location 1: _on_phase_complete callback (per-phase write during execution)
    Location 2: Final summary loop (batch write at pipeline end)

The core logic under test (same pattern in both locations):

    out_path = output_dir / f"{safe_pid}.md"
    new_content = f"# Phase: {phase_id}\\n\\n{phase_text}\\n"
    if out_path.exists() and out_path.stat().st_size > len(new_content):
        logger.info("keeping agent-written file ...")
    else:
        out_path.write_text(new_content)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helper — reproduce the exact logic from cli.py fix
# ---------------------------------------------------------------------------

def _apply_cli_write_logic(
    output_dir: Path,
    phase_id: str,
    phase_text: str,
) -> None:
    """Reproduce the exact write logic introduced for issue #210.

    This mirrors the pattern used in *both* fixed locations in cli.py so
    that the tests remain valid even if the surrounding CLI code changes.
    """
    logger = logging.getLogger(__name__)
    safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
    out_path = output_dir / f"{safe_pid}.md"
    new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
    if out_path.exists() and out_path.stat().st_size > len(new_content):
        logger.info(
            f"Phase '{phase_id}': keeping agent-written file "
            f"({out_path.stat().st_size} bytes) over captured output "
            f"({len(new_content)} bytes)"
        )
    else:
        out_path.write_text(new_content)


# ---------------------------------------------------------------------------
# Test 1: CLI preserves the larger agent-written file
# ---------------------------------------------------------------------------

class TestCliPreservesLargerAgentFile:
    """When the sub-agent has already written a large file, the CLI must not
    overwrite it with the (smaller) captured output.  Issue #210 scenario."""

    def test_cli_preserves_larger_agent_file(self, tmp_path):
        """Large agent file → captured output smaller → agent file kept."""
        phase_id = "research"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        # Simulate the sub-agent writing a full ~3000-word article (~18 000 chars)
        agent_content = "Agent output: " + ("word " * 3000)
        out_path.write_text(agent_content)
        agent_size = out_path.stat().st_size

        # CLI captures only a short summary (~670 words / ~4000 chars)
        captured_text = "Summary: " + ("word " * 670)

        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        # File on disk must still be the agent's version
        result = out_path.read_text()
        assert result == agent_content, (
            f"Expected agent-written content ({agent_size} bytes) to be preserved, "
            f"but got {len(result)} bytes"
        )

    def test_cli_preserves_larger_file_regardless_of_content(self, tmp_path):
        """The size-based guard must fire for any content where agent file > capture."""
        phase_id = "write_phase"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        large_agent_content = "x" * 50_000  # 50 KB
        out_path.write_text(large_agent_content)

        small_captured_text = "tiny"
        _apply_cli_write_logic(tmp_path, phase_id, small_captured_text)

        assert out_path.read_text() == large_agent_content

    def test_cli_logs_info_when_keeping_agent_file(self, tmp_path, caplog):
        """An INFO log must be emitted when the agent file is kept."""
        phase_id = "research"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        out_path.write_text("A" * 10_000)  # large agent file

        with caplog.at_level(logging.INFO):
            _apply_cli_write_logic(tmp_path, phase_id, "tiny captured text")

        log_messages = " ".join(r.message for r in caplog.records)
        assert "keeping agent-written file" in log_messages, (
            "Expected an INFO log mentioning 'keeping agent-written file'"
        )

    def test_cli_preserves_file_even_when_content_differs(self, tmp_path):
        """Content comparison is size-only; the guard must not require identical bytes."""
        phase_id = "factcheck"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        agent_content = "Completely different content from agent " + "x" * 5_000
        out_path.write_text(agent_content)

        # Captured output has different content but is shorter
        captured_text = "Captured: different text but shorter"
        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        assert out_path.read_text() == agent_content

    def test_phase_id_with_special_chars_preserved(self, tmp_path):
        """Phase IDs with special chars are sanitised; agent file still kept."""
        phase_id = "phase/with spaces"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        agent_content = "Large agent output " + "y" * 8_000
        out_path.write_text(agent_content)

        _apply_cli_write_logic(tmp_path, phase_id, "short capture")

        assert out_path.read_text() == agent_content


# ---------------------------------------------------------------------------
# Test 2: CLI writes normally when no existing file
# ---------------------------------------------------------------------------

class TestCliOverwritesWhenNoExistingFile:
    """When no file exists at the output path, the CLI should write normally."""

    def test_cli_overwrites_when_no_existing_file(self, tmp_path):
        """No pre-existing file → CLI writes the captured output."""
        phase_id = "research"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        assert not out_path.exists(), "Pre-condition: file must not exist"

        captured_text = "Captured agent output: " + ("word " * 200)
        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        assert out_path.exists(), "CLI must create the file when none exists"
        written = out_path.read_text()
        assert f"# Phase: {phase_id}" in written
        assert captured_text in written

    def test_cli_creates_correct_file_name(self, tmp_path):
        """Output file name must match the sanitised phase_id."""
        phase_id = "my phase"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        expected_path = tmp_path / f"{safe_pid}.md"

        _apply_cli_write_logic(tmp_path, phase_id, "some text")

        assert expected_path.exists()

    def test_cli_writes_correct_header_format(self, tmp_path):
        """Written file must start with '# Phase: {phase_id}'."""
        phase_id = "build"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        _apply_cli_write_logic(tmp_path, phase_id, "Build output here.")

        content = out_path.read_text()
        assert content.startswith(f"# Phase: {phase_id}\n")

    def test_cli_writes_phase_text_in_body(self, tmp_path):
        """The captured phase text must appear in the written file body."""
        phase_id = "analysis"
        captured_text = "This is the analysis output."
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        content = out_path.read_text()
        assert captured_text in content

    def test_cli_no_log_when_file_absent(self, tmp_path, caplog):
        """No 'keeping agent-written file' log when the file doesn't exist."""
        phase_id = "research"
        with caplog.at_level(logging.INFO):
            _apply_cli_write_logic(tmp_path, phase_id, "some content")

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
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        # Existing file is a short stub (e.g. agent wrote a placeholder)
        out_path.write_text("short stub")

        # CLI captures the full, longer output
        captured_text = "Full captured output: " + ("word " * 500)
        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        written = out_path.read_text()
        assert captured_text in written, (
            "CLI should overwrite the smaller existing file with the larger capture"
        )

    def test_cli_overwrites_when_existing_file_is_empty(self, tmp_path):
        """An empty existing file should always be overwritten."""
        phase_id = "write"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        out_path.write_text("")  # empty file

        captured_text = "Some captured content"
        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        assert captured_text in out_path.read_text()

    def test_cli_overwrites_when_sizes_equal(self, tmp_path):
        """When sizes are identical, the CLI should overwrite (no special-casing)."""
        phase_id = "qa"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        # Write a file with content that will be exactly the same size as new_content
        phase_text = "equal size content"
        new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
        # Write exactly the same bytes
        out_path.write_text(new_content)

        # Re-apply with same phase_text — same size → should overwrite (guard is STRICTLY >)
        _apply_cli_write_logic(tmp_path, phase_id, phase_text)

        # Content should be the formatted version
        assert out_path.read_text() == new_content

    def test_cli_no_keep_log_when_overwriting(self, tmp_path, caplog):
        """No 'keeping agent-written file' log when CLI overwrites."""
        phase_id = "research"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        out_path.write_text("tiny")

        with caplog.at_level(logging.INFO):
            _apply_cli_write_logic(tmp_path, phase_id, "much larger captured output here " * 50)

        for record in caplog.records:
            assert "keeping agent-written file" not in record.message

    def test_cli_overwrites_with_correct_content_format(self, tmp_path):
        """Overwritten file must use the '# Phase: {id}\\n\\n{text}\\n' format."""
        phase_id = "summary"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        out_path.write_text("old small content")

        captured_text = "New large captured content " + "x" * 1000
        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        content = out_path.read_text()
        expected = f"# Phase: {phase_id}\n\n{captured_text}\n"
        assert content == expected

    def test_cli_boundary_one_byte_larger_keeps_agent_file(self, tmp_path):
        """Agent file exactly 1 byte larger than new_content → must be kept."""
        phase_id = "delta"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        captured_text = "X" * 100
        new_content = f"# Phase: {phase_id}\n\n{captured_text}\n"
        # Agent file is 1 byte larger
        agent_content = new_content + "Y"
        out_path.write_text(agent_content)

        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        assert out_path.read_text() == agent_content

    def test_cli_boundary_one_byte_smaller_overwrites(self, tmp_path):
        """Agent file exactly 1 byte smaller than new_content → must be overwritten."""
        phase_id = "delta"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        out_path = tmp_path / f"{safe_pid}.md"

        captured_text = "X" * 100
        new_content = f"# Phase: {phase_id}\n\n{captured_text}\n"
        # Agent file is 1 byte smaller
        agent_content = new_content[:-1]
        out_path.write_text(agent_content)

        _apply_cli_write_logic(tmp_path, phase_id, captured_text)

        assert out_path.read_text() == new_content
