"""Tests for fix/243 — file-path handoff as default when output_dir is set.

Covers:
- FH-1:  {previous_output} with output_dir → summaries, NOT full content
- FH-2:  {previous_output} without output_dir → full inline content (backward compat)
- FH-3:  {previous_output_inline} always gives full content regardless of output_dir
- FH-4:  {previous_output[phase_id]} with output_dir → prepends "Full output at:" note
- FH-5:  {previous_output[phase_id]} without output_dir → raw inline content (backward compat)
- FH-6:  Missing phase_id in {previous_output[phase_id]} → <MISSING:...> placeholder
- FH-7:  {previous_output} with output_dir and no prior phases → "No prior phases."
- FH-8:  File path uses safe_pid (hyphens replaced with underscores)
- FH-9:  {phase_summary} still works as before (unchanged)
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from orchestration_engine.schemas import TaskResult, TaskState
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(text: str) -> dict:
    """Build a TaskResult-like dict (as stored in phase_outputs)."""
    return {
        "result": {"text": text},
        "state": "SUCCESS",
    }


def _make_phase(phase_id: str, prompt: str) -> PhaseDefinition:
    return PhaseDefinition(id=phase_id, name=phase_id.replace("-", " ").title(), prompt_template=prompt)


def _make_template(*phases: PhaseDefinition) -> PipelineTemplate:
    return PipelineTemplate(id="test-pipeline", name="Test Pipeline", phases=list(phases))


def _make_runner() -> MagicMock:
    runner = MagicMock()
    task_store: Dict[str, Any] = {}

    def submit_task(spec):
        task_store[spec.id] = spec
        return spec.id

    def get_task(task_id):
        return task_store.get(task_id)

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = get_task
    runner.queue.complete_task = MagicMock()
    runner.executors = {}
    return runner


def _make_sequencer(output_dir=None) -> PhaseSequencer:
    phase = _make_phase("dummy", "dummy")
    template = _make_template(phase)
    return PhaseSequencer(template=template, runner=_make_runner(), output_dir=output_dir)


# ---------------------------------------------------------------------------
# FH-1: {previous_output} with output_dir → summaries, NOT full content
# ---------------------------------------------------------------------------

class TestPreviousOutputWithOutputDir:
    def test_previous_output_gives_summary_lines(self):
        """FH-1: With output_dir, {previous_output} should contain word-count summary."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("word " * 50)  # 50 words
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        # Should contain the file path reference
        assert "/tmp/run/research.md" in result
        # Should contain word count
        assert "50" in result
        # Should NOT contain the raw text "word word word ..."
        assert "word word word" not in result

    def test_previous_output_contains_phase_name(self):
        """FH-1: Summary line includes the phase name."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("some content here")
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        # Phase name "Research" should appear in summary
        assert "Research" in result or "research" in result

    def test_previous_output_multiple_phases(self):
        """FH-1: With multiple prior phases, summary lists each one."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("research content")
        seq.phase_outputs["outline"] = _make_result("outline content")
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        assert "/tmp/run/research.md" in result
        assert "/tmp/run/outline.md" in result
        # Full content should NOT be inline
        assert "research content" not in result
        assert "outline content" not in result

    def test_previous_output_arrow_format(self):
        """FH-1: Summary lines use → arrow character."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("content")
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        assert "→" in result


# ---------------------------------------------------------------------------
# FH-2: {previous_output} without output_dir → full inline (backward compat)
# ---------------------------------------------------------------------------

class TestPreviousOutputWithoutOutputDir:
    def test_previous_output_is_full_dict_repr(self):
        """FH-2: Without output_dir, {previous_output} returns str(dict) — backward compat."""
        seq = _make_sequencer(output_dir=None)
        seq.phase_outputs["research"] = _make_result("full research text here")
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        # The full text should appear somewhere in the result (inside dict repr)
        assert "full research text here" in result

    def test_no_file_path_references_without_output_dir(self):
        """FH-2: Without output_dir, no file path references appear."""
        seq = _make_sequencer(output_dir=None)
        seq.phase_outputs["research"] = _make_result("some content")
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        assert "Full output at:" not in result
        assert ".md" not in result


# ---------------------------------------------------------------------------
# FH-3: {previous_output_inline} always gives full content
# ---------------------------------------------------------------------------

class TestPreviousOutputInline:
    def test_inline_with_output_dir_gives_full_content(self):
        """FH-3: {previous_output_inline} gives full content even when output_dir set."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("the complete research text")
        phase = _make_phase("write", "{previous_output_inline}")
        result = seq._build_phase_input(phase, {})
        # Full text must appear inline
        assert "the complete research text" in result

    def test_inline_without_output_dir_gives_full_content(self):
        """FH-3: {previous_output_inline} works same way without output_dir."""
        seq = _make_sequencer(output_dir=None)
        seq.phase_outputs["research"] = _make_result("another research text")
        phase = _make_phase("write", "{previous_output_inline}")
        result = seq._build_phase_input(phase, {})
        assert "another research text" in result

    def test_inline_does_not_contain_file_paths(self):
        """FH-3: {previous_output_inline} should not inject file path summaries."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("research content")
        phase = _make_phase("write", "{previous_output_inline}")
        result = seq._build_phase_input(phase, {})
        assert "Full output at:" not in result
        assert "→" not in result


# ---------------------------------------------------------------------------
# FH-4 & FH-5: {previous_output[phase_id]} with and without output_dir
# ---------------------------------------------------------------------------

class TestPreviousOutputItemAccess:
    def test_item_access_with_output_dir_prepends_file_path(self):
        """FH-4: With output_dir, {previous_output[research]} prepends file note."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("detailed research output")
        phase = _make_phase("write", "{previous_output[research]}")
        result = seq._build_phase_input(phase, {})
        assert "Full output at: /tmp/run/research.md" in result
        # Inline content still present
        assert "detailed research output" in result

    def test_item_access_without_output_dir_no_file_note(self):
        """FH-5: Without output_dir, {previous_output[research]} has no file note."""
        seq = _make_sequencer(output_dir=None)
        seq.phase_outputs["research"] = _make_result("detailed research output")
        phase = _make_phase("write", "{previous_output[research]}")
        result = seq._build_phase_input(phase, {})
        assert "Full output at:" not in result

    def test_item_access_file_note_before_content(self):
        """FH-4: File path note comes BEFORE the inline content."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("the actual content")
        phase = _make_phase("write", "{previous_output[research]}")
        result = seq._build_phase_input(phase, {})
        file_note_pos = result.find("Full output at:")
        content_pos = result.find("the actual content")
        assert file_note_pos < content_pos


# ---------------------------------------------------------------------------
# FH-6: Missing phase_id → placeholder
# ---------------------------------------------------------------------------

class TestMissingPhaseId:
    def test_missing_phase_returns_placeholder(self):
        """FH-6: {previous_output[nonexistent]} returns <MISSING:...> placeholder."""
        seq = _make_sequencer(output_dir="/tmp/run")
        phase = _make_phase("write", "{previous_output[nonexistent]}")
        result = seq._build_phase_input(phase, {})
        assert "<MISSING:previous_output[nonexistent]>" in result

    def test_missing_phase_without_output_dir_returns_placeholder(self):
        """FH-6: Placeholder works without output_dir too."""
        seq = _make_sequencer(output_dir=None)
        phase = _make_phase("write", "{previous_output[nonexistent]}")
        result = seq._build_phase_input(phase, {})
        assert "<MISSING:previous_output[nonexistent]>" in result


# ---------------------------------------------------------------------------
# FH-7: No prior phases → "No prior phases."
# ---------------------------------------------------------------------------

class TestNoPriorPhases:
    def test_no_prior_phases_message(self):
        """FH-7: With output_dir but no prior phases, returns 'No prior phases.'"""
        seq = _make_sequencer(output_dir="/tmp/run")
        # phase_outputs is empty
        phase = _make_phase("research", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        assert "No prior phases." in result


# ---------------------------------------------------------------------------
# FH-8: Hyphen-to-underscore safe_pid conversion
# ---------------------------------------------------------------------------

class TestSafePidConversion:
    def test_hyphenated_phase_id_uses_underscore_in_path(self):
        """FH-8: Phase ID 'fact-check' → fact_check.md in file path."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["fact-check"] = _make_result("fact check content")
        phase = _make_phase("write", "{previous_output}")
        result = seq._build_phase_input(phase, {})
        assert "/tmp/run/fact_check.md" in result

    def test_item_access_hyphenated_phase_uses_underscore(self):
        """FH-8: {previous_output[fact-check]} → 'Full output at: .../fact_check.md'"""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["fact-check"] = _make_result("fact check content")
        phase = _make_phase("write", "{previous_output[fact-check]}")
        result = seq._build_phase_input(phase, {})
        assert "Full output at: /tmp/run/fact_check.md" in result


# ---------------------------------------------------------------------------
# FH-9: {phase_summary} still works (regression check)
# ---------------------------------------------------------------------------

class TestPhaseSummaryRegression:
    def test_phase_summary_with_output_dir(self):
        """FH-9: {phase_summary} variable still expands correctly."""
        seq = _make_sequencer(output_dir="/tmp/run")
        seq.phase_outputs["research"] = _make_result("some content " * 10)
        phase = _make_phase("write", "{phase_summary}")
        result = seq._build_phase_input(phase, {})
        assert "research" in result.lower()
        assert "/tmp/run/" in result

    def test_phase_summary_no_prior_phases(self):
        """FH-9: {phase_summary} with no prior phases gives first-phase message."""
        seq = _make_sequencer(output_dir="/tmp/run")
        phase = _make_phase("research", "{phase_summary}")
        result = seq._build_phase_input(phase, {})
        assert "first phase" in result.lower() or "no prior" in result.lower()
