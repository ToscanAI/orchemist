"""Acceptance tests for Issue #681 — Result-dict enrichment from disk.

Covers all behavioral contracts specified in the issue:

Core behavior:
- Given disk file larger than chat summary → result["result"]["text"] updated
- Given disk file smaller → unchanged
- Given disk file same size → unchanged (chat wins on tie)
- Given no disk file → no crash, no change
- Given disk file empty (0 bytes) → unchanged
- Given output_dir is None → enrichment skipped, no crash

Downstream effects:
- _extract_phase_text(result) returns disk content after enrichment
- phase_summary contains full disk content after enrichment fires for spec phase
- git handoff gets full content (enrichment fires before commit_phase_output)

Edge cases:
- output_dir exists but phase file does not → no change
- result has no "result" key → setdefault creates it safely
- result["result"]["text"] is None → comparison handles gracefully
- disk file larger but output_dir is None → no enrichment
- non-spec-loop phase writes no file → no regression
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.sequencer import PhaseSequencer, _extract_phase_text
from orchestration_engine.schemas import TaskResult, TaskState
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_phase(phase_id: str = "spec") -> PhaseDefinition:
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template="Hello {input}",
        depends_on=[],
    )


def _make_template(phases: List[PhaseDefinition], template_id: str = "test-pipeline") -> PipelineTemplate:
    return PipelineTemplate(id=template_id, name="Test Pipeline", phases=phases)


def _make_runner(result_text: str = "short summary") -> MagicMock:
    """Build a mock TaskRunner that returns a success result with the given text."""
    runner = MagicMock()
    _task_store: Dict[str, Any] = {}

    def submit_task(spec):
        _task_store[spec.id] = spec
        return spec.id

    def get_task(task_id):
        return _task_store.get(task_id)

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = get_task
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()

    def execute(task_spec, **kw):
        return TaskResult(
            task_id=task_spec.id,
            task_type=task_spec.type,
            state=TaskState.SUCCESS,
            confidence=0.9,
            result={"text": result_text},
        )

    executor = MagicMock()
    executor.can_handle.return_value = True
    executor.execute.side_effect = execute
    runner.executors = [executor]
    return runner


def _make_sequencer(
    phase_id: str = "spec",
    output_dir: Optional[Path] = None,
    chat_text: str = "short summary",
) -> PhaseSequencer:
    """Build a minimal PhaseSequencer with optional output_dir."""
    phase = _make_phase(phase_id)
    template = _make_template([phase])
    runner = _make_runner(result_text=chat_text)
    return PhaseSequencer(template, runner, output_dir=str(output_dir) if output_dir else None)


def _make_result(text: Optional[str] = "short summary") -> dict:
    """Build a minimal phase result dict matching TaskResult.model_dump() shape."""
    if text is None:
        return {"result": {"text": None}, "state": "success"}
    return {"result": {"text": text}, "state": "success"}


# ---------------------------------------------------------------------------
# TestResultEnrichmentCore — happy path and non-enrichment cases
# ---------------------------------------------------------------------------

class TestResultEnrichmentCore:
    """Core behavioral contracts for the result enrichment logic."""

    def test_disk_file_larger_than_chat_updates_result_text(self, tmp_path: Path) -> None:
        """Given disk file larger than chat summary → result["result"]["text"] updated."""
        seq = _make_sequencer(phase_id="spec", output_dir=tmp_path)
        disk_file = tmp_path / "spec.md"
        disk_file.write_text("This is a much longer disk file content that exceeds the chat summary by a lot.")

        result = _make_result("short summary")
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == disk_file.read_text()

    def test_disk_file_smaller_than_chat_leaves_result_unchanged(self, tmp_path: Path) -> None:
        """Given disk file smaller than chat summary → result["result"]["text"] unchanged."""
        seq = _make_sequencer(phase_id="spec", output_dir=tmp_path)
        disk_file = tmp_path / "spec.md"
        disk_file.write_text("tiny")

        original_text = "This is a long chat summary that is definitely longer than the disk file."
        result = _make_result(original_text)
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == original_text

    def test_disk_file_same_size_as_chat_leaves_result_unchanged(self, tmp_path: Path) -> None:
        """Given disk file same size as chat summary → chat wins on tie, unchanged."""
        seq = _make_sequencer(phase_id="spec", output_dir=tmp_path)
        chat_text = "exactly twelve"  # 14 chars
        disk_file = tmp_path / "spec.md"
        disk_file.write_text(chat_text)  # same content, same length

        result = _make_result(chat_text)
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == chat_text

    def test_no_disk_file_leaves_result_unchanged(self, tmp_path: Path) -> None:
        """Given no disk file exists → result dict unchanged, no crash."""
        seq = _make_sequencer(phase_id="spec", output_dir=tmp_path)
        # No file written to disk

        original_text = "chat summary"
        result = _make_result(original_text)
        original_result = dict(result)
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == original_text
        assert result == original_result

    def test_empty_disk_file_leaves_result_unchanged(self, tmp_path: Path) -> None:
        """Given disk file exists but is empty (0 bytes) → result unchanged."""
        seq = _make_sequencer(phase_id="spec", output_dir=tmp_path)
        disk_file = tmp_path / "spec.md"
        disk_file.write_text("")  # 0 bytes

        original_text = "chat summary"
        result = _make_result(original_text)
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == original_text

    def test_output_dir_none_skips_enrichment_no_crash(self) -> None:
        """Given self.output_dir is None → enrichment skipped entirely, no crash."""
        seq = _make_sequencer(output_dir=None)
        result = _make_result("chat summary")

        # Must not raise, must not change anything
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == "chat summary"


# ---------------------------------------------------------------------------
# TestResultEnrichmentSkipped — cases where enrichment must NOT fire
# ---------------------------------------------------------------------------

class TestResultEnrichmentSkipped:
    """Cases where enrichment should be silently skipped."""

    def test_output_dir_none_with_large_disk_content_skips(self, tmp_path: Path) -> None:
        """Given disk file is larger but output_dir is None → no enrichment."""
        # We build the sequencer without output_dir, but note the file separately
        disk_file = tmp_path / "spec.md"
        disk_file.write_text("A very large content that would win any size comparison easily.")

        seq = _make_sequencer(output_dir=None)
        result = _make_result("short")
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == "short"

    def test_output_dir_set_but_different_phase_file_missing(self, tmp_path: Path) -> None:
        """Given output_dir exists but the specific phase file does not → no change."""
        seq = _make_sequencer(phase_id="spec", output_dir=tmp_path)
        # Write a file for a *different* phase
        (tmp_path / "other_phase.md").write_text("Large content for a different phase entirely.")

        original_text = "chat summary for spec"
        result = _make_result(original_text)
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == original_text

    def test_non_spec_phase_writes_no_file_no_regression(self, tmp_path: Path) -> None:
        """Given non-spec-loop phase writes no file → exists() returns False, no change."""
        seq = _make_sequencer(phase_id="build", output_dir=tmp_path)
        # No file for "build" phase

        original_text = "build phase output"
        result = _make_result(original_text)
        seq._enrich_result_from_disk("build", result)

        assert result["result"]["text"] == original_text

    def test_disk_file_equal_length_not_strictly_greater(self, tmp_path: Path) -> None:
        """Enrichment requires len(disk) > len(chat); equal length must NOT trigger it."""
        seq = _make_sequencer(output_dir=tmp_path)
        chat_text = "abc"
        (tmp_path / "spec.md").write_text("xyz")  # same length, different content

        result = _make_result(chat_text)
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == chat_text  # chat text preserved


# ---------------------------------------------------------------------------
# TestResultEnrichmentEdgeCases — None inputs, missing keys, type robustness
# ---------------------------------------------------------------------------

class TestResultEnrichmentEdgeCases:
    """Edge cases: None inputs, missing keys, empty/malformed dicts."""

    def test_result_has_no_result_key_setdefault_creates_it(self, tmp_path: Path) -> None:
        """Given result has no "result" key → setdefault creates it safely, no crash."""
        seq = _make_sequencer(output_dir=tmp_path)
        disk_content = "Full disk content that is definitely larger than nothing."
        (tmp_path / "spec.md").write_text(disk_content)

        result: dict = {}  # No "result" key at all
        seq._enrich_result_from_disk("spec", result)

        assert result.get("result", {}).get("text") == disk_content

    def test_result_text_is_none_handled_gracefully(self, tmp_path: Path) -> None:
        """Given result["result"]["text"] is None → disk comparison handles gracefully, no crash."""
        seq = _make_sequencer(output_dir=tmp_path)
        disk_content = "Disk content that is non-empty and larger than None."
        (tmp_path / "spec.md").write_text(disk_content)

        result = {"result": {"text": None}, "state": "success"}
        # Must not raise TypeError on len(None)
        seq._enrich_result_from_disk("spec", result)

        # Disk file is non-empty (> 0), None treated as empty → enrichment fires
        assert result["result"]["text"] == disk_content

    def test_output_dir_is_none_and_large_disk_file_independent(self, tmp_path: Path) -> None:
        """Explicit guard: output_dir=None must prevent enrichment even if file would win."""
        large_content = "x" * 1000
        (tmp_path / "spec.md").write_text(large_content)

        seq = _make_sequencer(output_dir=None)
        result = _make_result("short")
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == "short"

    def test_enrichment_idempotent_on_second_call(self, tmp_path: Path) -> None:
        """Calling enrichment twice with same file should produce the same result."""
        seq = _make_sequencer(output_dir=tmp_path)
        disk_content = "Full disk content written once."
        (tmp_path / "spec.md").write_text(disk_content)

        result = _make_result("short")
        seq._enrich_result_from_disk("spec", result)
        seq._enrich_result_from_disk("spec", result)  # second call

        assert result["result"]["text"] == disk_content

    def test_result_with_empty_string_text_enriched_by_nonempty_disk(self, tmp_path: Path) -> None:
        """Given result["result"]["text"] is empty string → any non-empty disk file wins."""
        seq = _make_sequencer(output_dir=tmp_path)
        disk_content = "non-empty"
        (tmp_path / "spec.md").write_text(disk_content)

        result = _make_result("")
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == disk_content

    def test_phase_id_used_as_filename_stem(self, tmp_path: Path) -> None:
        """Enrichment uses {phase_id}.md as the filename, not any other convention."""
        seq = _make_sequencer(output_dir=tmp_path)
        # Write file for wrong name to confirm it's NOT picked up
        (tmp_path / "spec.txt").write_text("wrong extension, should be ignored")
        (tmp_path / "spec.md").write_text("correct file with more content than chat")

        result = _make_result("short chat")
        seq._enrich_result_from_disk("spec", result)

        assert result["result"]["text"] == "correct file with more content than chat"


# ---------------------------------------------------------------------------
# TestResultEnrichmentDownstreamEffects — downstream consumer behaviour
# ---------------------------------------------------------------------------

class TestResultEnrichmentDownstreamEffects:
    """Downstream consumers get the enriched content automatically."""

    def test_extract_phase_text_returns_disk_content_after_enrichment(self, tmp_path: Path) -> None:
        """Given enrichment fires, _extract_phase_text(result) returns the disk content."""
        seq = _make_sequencer(output_dir=tmp_path)
        disk_content = "Full disk content — much larger than the chat summary."
        (tmp_path / "spec.md").write_text(disk_content)

        result = _make_result("short summary")
        seq._enrich_result_from_disk("spec", result)

        assert _extract_phase_text(result) == disk_content

    def test_extract_phase_text_unchanged_when_enrichment_skipped(self, tmp_path: Path) -> None:
        """Given enrichment does NOT fire, _extract_phase_text returns original chat text."""
        seq = _make_sequencer(output_dir=tmp_path)
        # No disk file → enrichment skipped

        chat_text = "original chat text"
        result = _make_result(chat_text)
        seq._enrich_result_from_disk("spec", result)

        assert _extract_phase_text(result) == chat_text

    def test_phase_outputs_contains_enriched_text_after_execute(self, tmp_path: Path) -> None:
        """Integration: phase_outputs[phase_id] contains disk content after execute()."""
        phase_id = "spec"
        short_chat = "short"
        long_disk = "This is the full disk output, much longer than the short chat summary."

        phase = _make_phase(phase_id)
        template = _make_template([phase])
        runner = _make_runner(result_text=short_chat)
        seq = PhaseSequencer(template, runner, output_dir=str(tmp_path))

        # Write disk file before execute() runs
        (tmp_path / f"{phase_id}.md").write_text(long_disk)

        seq.execute({})

        stored = seq.phase_outputs.get(phase_id, {})
        assert _extract_phase_text(stored) == long_disk, (
            "phase_outputs should contain the enriched disk content"
        )

    def test_phase_outputs_unchanged_when_no_disk_file(self, tmp_path: Path) -> None:
        """Integration: phase_outputs gets original chat text when no disk file exists."""
        phase_id = "spec"
        chat_text = "original chat text from agent"

        phase = _make_phase(phase_id)
        template = _make_template([phase])
        runner = _make_runner(result_text=chat_text)
        seq = PhaseSequencer(template, runner, output_dir=str(tmp_path))

        # No disk file written
        seq.execute({})

        stored = seq.phase_outputs.get(phase_id, {})
        assert _extract_phase_text(stored) == chat_text

    def test_git_handoff_receives_disk_content_when_enrichment_fires(self, tmp_path: Path) -> None:
        """Integration: git handoff commit_phase_output receives full disk content."""
        phase_id = "spec"
        short_chat = "short chat"
        long_disk = "Full specification content from disk. Much longer than the chat summary output."

        phase = _make_phase(phase_id)
        template = _make_template([phase])
        runner = _make_runner(result_text=short_chat)
        seq = PhaseSequencer(template, runner, output_dir=str(tmp_path))

        # Wire up a mock git handoff
        mock_handoff = MagicMock()
        mock_handoff.is_active.return_value = True
        seq._git_handoff = mock_handoff
        seq._loop_groups = {phase_id}  # mark phase as part of loop group

        (tmp_path / f"{phase_id}.md").write_text(long_disk)

        seq.execute({})

        # commit_phase_output should have been called with the disk content
        calls = mock_handoff.commit_phase_output.call_args_list
        assert len(calls) >= 1, "commit_phase_output should have been called"
        _, _, committed_text = calls[0][0]  # positional: (phase_id, iter, text)
        assert committed_text == long_disk, (
            f"Git handoff received '{committed_text}' instead of full disk content"
        )
