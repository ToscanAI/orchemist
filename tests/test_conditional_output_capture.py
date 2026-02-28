"""Tests for conditional OUTPUT_CAPTURE_INSTRUCTION / OUTPUT_FILE_WRITE_INSTRUCTION.

Issue #245: make the output instruction appended to sub-agent prompts
conditional on whether ``output_dir`` is present in the task payload.

When output_dir is NOT set  → use OUTPUT_CAPTURE_INSTRUCTION  (old behaviour)
When output_dir IS  set     → use OUTPUT_FILE_WRITE_INSTRUCTION (v2.7+ behaviour)
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.openclaw_executor import (
    OUTPUT_CAPTURE_INSTRUCTION,
    OUTPUT_FILE_WRITE_INSTRUCTION,
    OpenClawExecutor,
)
from orchestration_engine.schemas import (
    Priority,
    TaskSpec,
    TaskState,
    TaskType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(prompt: str = "Do something.", output_dir: str | None = None) -> TaskSpec:
    """Build a minimal TaskSpec, optionally with an output_dir in the payload."""
    payload: dict = {"prompt": prompt}
    if output_dir is not None:
        payload["output_dir"] = output_dir
    return TaskSpec(
        type=TaskType.CONTENT,
        payload=payload,
        priority=Priority.NORMAL,
    )


def _dry_executor() -> OpenClawExecutor:
    """Return an executor in dry-run mode so no HTTP calls are made."""
    return OpenClawExecutor(
        gateway_url="http://localhost:18789",
        gateway_token="test-token",
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# Constant sanity checks
# ---------------------------------------------------------------------------

class TestConstants:
    """Both instruction constants must be non-empty strings and differ from each other."""

    def test_capture_instruction_is_nonempty(self):
        assert isinstance(OUTPUT_CAPTURE_INSTRUCTION, str)
        assert len(OUTPUT_CAPTURE_INSTRUCTION.strip()) > 0

    def test_file_write_instruction_is_nonempty(self):
        assert isinstance(OUTPUT_FILE_WRITE_INSTRUCTION, str)
        assert len(OUTPUT_FILE_WRITE_INSTRUCTION.strip()) > 0

    def test_instructions_are_different(self):
        assert OUTPUT_CAPTURE_INSTRUCTION != OUTPUT_FILE_WRITE_INSTRUCTION

    def test_capture_instruction_forbids_file_writes(self):
        """The capture instruction must tell agents NOT to write files."""
        assert "Do NOT write output to workspace files" in OUTPUT_CAPTURE_INSTRUCTION

    def test_file_write_instruction_requests_file_write(self):
        """The file-write instruction must tell agents TO write to a file."""
        assert "Write your COMPLETE output to the file path" in OUTPUT_FILE_WRITE_INSTRUCTION

    def test_file_write_instruction_requests_summary(self):
        """The file-write instruction must ask for a brief summary reply."""
        assert "summary" in OUTPUT_FILE_WRITE_INSTRUCTION.lower()


# ---------------------------------------------------------------------------
# Core behaviour: which instruction is chosen
# ---------------------------------------------------------------------------

class TestCaptureInstructionWithoutOutputDir:
    """When no output_dir is in the payload, OUTPUT_CAPTURE_INSTRUCTION is used."""

    def test_capture_instruction_used_in_dry_run(self):
        """Dry-run executor with no output_dir → capture instruction path taken."""
        executor = _dry_executor()
        task = _make_task(prompt="Write a haiku.", output_dir=None)

        # We inspect the prompt that would be sent by monkey-patching _run_session.
        # Because dry_run=True skips _run_session, we test via the prompt-building
        # logic by temporarily disabling dry_run and capturing what _run_session
        # would receive.
        captured_prompt: list[str] = []

        def fake_run_session(prompt, model, thinking, timeout=None):
            captured_prompt.append(prompt)
            return "mock output", 0

        executor.dry_run = False  # force real path
        with patch.object(executor, "_run_session", side_effect=fake_run_session):
            executor.execute(task)

        assert captured_prompt, "Expected _run_session to be called"
        prompt_sent = captured_prompt[0]
        assert OUTPUT_CAPTURE_INSTRUCTION in prompt_sent

    def test_file_write_instruction_absent_without_output_dir(self):
        """No output_dir → file-write instruction must NOT appear in the prompt."""
        executor = _dry_executor()
        task = _make_task(prompt="Summarise this.", output_dir=None)

        captured_prompt: list[str] = []

        def fake_run_session(prompt, model, thinking, timeout=None):
            captured_prompt.append(prompt)
            return "mock output", 0

        executor.dry_run = False
        with patch.object(executor, "_run_session", side_effect=fake_run_session):
            executor.execute(task)

        assert OUTPUT_FILE_WRITE_INSTRUCTION not in captured_prompt[0]


class TestCaptureInstructionWithOutputDir:
    """When output_dir IS in the payload, OUTPUT_FILE_WRITE_INSTRUCTION is used."""

    def test_file_write_instruction_used_when_output_dir_set(self):
        """output_dir set → file-write instruction appears in the prompt."""
        executor = _dry_executor()
        task = _make_task(prompt="Research this topic.", output_dir="/tmp/pipeline-out")

        captured_prompt: list[str] = []

        def fake_run_session(prompt, model, thinking, timeout=None):
            captured_prompt.append(prompt)
            return "mock output", 0

        executor.dry_run = False
        with patch.object(executor, "_run_session", side_effect=fake_run_session):
            executor.execute(task)

        assert captured_prompt, "Expected _run_session to be called"
        assert OUTPUT_FILE_WRITE_INSTRUCTION in captured_prompt[0]

    def test_capture_instruction_absent_when_output_dir_set(self):
        """output_dir set → the old capture instruction must NOT appear."""
        executor = _dry_executor()
        task = _make_task(prompt="Research this topic.", output_dir="/tmp/pipeline-out")

        captured_prompt: list[str] = []

        def fake_run_session(prompt, model, thinking, timeout=None):
            captured_prompt.append(prompt)
            return "mock output", 0

        executor.dry_run = False
        with patch.object(executor, "_run_session", side_effect=fake_run_session):
            executor.execute(task)

        assert OUTPUT_CAPTURE_INSTRUCTION not in captured_prompt[0]


# ---------------------------------------------------------------------------
# Prompt content verification (combined)
# ---------------------------------------------------------------------------

class TestPromptContainsCorrectInstruction:
    """Verify the full prompt content for both cases."""

    def _capture_prompt(self, task: TaskSpec) -> str:
        """Run execute() with a patched _run_session and return the prompt sent."""
        executor = OpenClawExecutor(
            gateway_url="http://localhost:18789",
            gateway_token="test-token",
            dry_run=False,
        )
        captured: list[str] = []

        def fake_run_session(prompt, model, thinking, timeout=None):
            captured.append(prompt)
            return "mock output", 0

        with patch.object(executor, "_run_session", side_effect=fake_run_session):
            executor.execute(task)

        assert captured, "Expected _run_session to be called"
        return captured[0]

    def test_prompt_without_output_dir_contains_original_prompt_text(self):
        """The original prompt text is present in the final prompt (no output_dir)."""
        user_prompt = "Explain quantum entanglement."
        task = _make_task(prompt=user_prompt, output_dir=None)
        full_prompt = self._capture_prompt(task)
        assert user_prompt in full_prompt

    def test_prompt_without_output_dir_ends_with_capture_instruction(self):
        """Without output_dir, the prompt ends with the capture instruction block."""
        task = _make_task(prompt="Write a haiku.", output_dir=None)
        full_prompt = self._capture_prompt(task)
        assert full_prompt.endswith(OUTPUT_CAPTURE_INSTRUCTION)

    def test_prompt_with_output_dir_contains_original_prompt_text(self):
        """The original prompt text is present in the final prompt (with output_dir)."""
        user_prompt = "Research AI safety."
        task = _make_task(prompt=user_prompt, output_dir="/tmp/out")
        full_prompt = self._capture_prompt(task)
        assert user_prompt in full_prompt

    def test_prompt_with_output_dir_ends_with_file_write_instruction(self):
        """With output_dir set, the prompt ends with the file-write instruction block."""
        task = _make_task(prompt="Research AI safety.", output_dir="/tmp/out")
        full_prompt = self._capture_prompt(task)
        assert full_prompt.endswith(OUTPUT_FILE_WRITE_INSTRUCTION)

    def test_empty_string_output_dir_treated_as_falsy(self):
        """An empty string output_dir is falsy — should use capture instruction."""
        task = _make_task(prompt="Do something.", output_dir="")
        # Payload now has output_dir="", which is falsy
        full_prompt = self._capture_prompt(task)
        # Empty string is falsy → capture instruction
        assert OUTPUT_CAPTURE_INSTRUCTION in full_prompt
        assert OUTPUT_FILE_WRITE_INSTRUCTION not in full_prompt

    def test_nonempty_output_dir_triggers_file_write_instruction(self):
        """Any non-empty output_dir string triggers the file-write instruction."""
        for dir_path in ["/tmp/run-1", "output/phase", "."]:
            task = _make_task(prompt="Work.", output_dir=dir_path)
            full_prompt = self._capture_prompt(task)
            assert OUTPUT_FILE_WRITE_INSTRUCTION in full_prompt, (
                f"Expected file-write instruction for output_dir={dir_path!r}"
            )
