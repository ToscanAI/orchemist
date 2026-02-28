"""
tests/test_file_write_action.py — Tests for Issue #189: File Write Action

Covers:
    PhaseDefinition — new write_files / working_dir / base_dir fields
    YAML parsing   — new fields round-trip through known_fields
    Sequencer      — _handle_file_write() logic (all branches)
    Integration    — end-to-end: phase output → files on disk
    Dry-run        — parse but don't write
    Safety         — refuse writes when working_dir escapes base_dir
    Metadata       — files_written always present in result metadata
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.templates import PhaseDefinition, PipelineTemplate, TemplateEngine
from orchestration_engine.sequencer import PhaseSequencer
from orchestration_engine.schemas import TaskResult, TaskState, TaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_FILE_OUTPUT = (
    "=== FILE: hello.py ===\n"
    "print('hello')\n"
    "=== END FILE ===\n"
)

_MULTI_FILE_OUTPUT = (
    "=== FILE: src/foo.py ===\n"
    "x = 1\n"
    "=== END FILE ===\n"
    "=== FILE: README.md ===\n"
    "# Readme\n"
    "=== END FILE ===\n"
)


def _make_phase(
    phase_id: str = "write_phase",
    write_files: bool = True,
    working_dir: str = ".",
    base_dir: str = "",
    prompt: str = "Hello",
    depends_on: Optional[List[str]] = None,
) -> PhaseDefinition:
    return PhaseDefinition(
        id=phase_id,
        name=phase_id,
        prompt_template=prompt,
        write_files=write_files,
        working_dir=working_dir,
        base_dir=base_dir,
        depends_on=depends_on or [],
    )


def _make_result(
    text: str = _SIMPLE_FILE_OUTPUT,
    state: str = "success",
) -> dict:
    """Build a minimal result dict mirroring TaskResult.model_dump()."""
    return {
        "task_id": "t-001",
        "task_type": "content",
        "state": state,
        "confidence": 0.9,
        "result": {"text": text},
        "metadata": {},
        "errors": [],
    }


def _make_runner(executor_result: TaskResult) -> MagicMock:
    """Build a minimal mock runner that returns *executor_result* from execute()."""
    mock_queue = MagicMock()
    mock_queue.get_task.return_value = MagicMock(
        id="t-001",
        type=TaskType.CONTENT,
    )
    mock_queue.submit_task.return_value = "t-001"
    mock_queue.complete_task.return_value = None
    mock_queue.fail_task.return_value = None

    mock_executor = MagicMock()
    mock_executor.can_handle.return_value = True
    mock_executor.execute.return_value = executor_result

    runner = MagicMock()
    runner.queue = mock_queue
    runner.executors = [mock_executor]
    return runner


def _make_task_result(
    text: str = _SIMPLE_FILE_OUTPUT,
    state: TaskState = TaskState.SUCCESS,
) -> TaskResult:
    return TaskResult(
        task_id="t-001",
        task_type=TaskType.CONTENT,
        state=state,
        confidence=0.9,
        result={"text": text},
        metadata={},
    )


# ---------------------------------------------------------------------------
# 1. PhaseDefinition — new fields exist with correct defaults
# ---------------------------------------------------------------------------

class TestPhaseDefinitionNewFields:
    def test_write_files_default_false(self):
        p = PhaseDefinition(id="p", name="P")
        assert p.write_files is False

    def test_working_dir_default_dot(self):
        p = PhaseDefinition(id="p", name="P")
        assert p.working_dir == "."

    def test_base_dir_default_empty(self):
        p = PhaseDefinition(id="p", name="P")
        assert p.base_dir == ""

    def test_write_files_can_be_set_true(self):
        p = PhaseDefinition(id="p", name="P", write_files=True)
        assert p.write_files is True

    def test_working_dir_custom(self):
        p = PhaseDefinition(id="p", name="P", working_dir="/tmp/out")
        assert p.working_dir == "/tmp/out"

    def test_base_dir_custom(self):
        p = PhaseDefinition(id="p", name="P", base_dir="/tmp")
        assert p.base_dir == "/tmp"

    def test_none_coercion_write_files(self):
        p = PhaseDefinition(id="p", name="P", write_files=None)
        assert p.write_files is False

    def test_none_coercion_working_dir(self):
        p = PhaseDefinition(id="p", name="P", working_dir=None)
        assert p.working_dir == "."

    def test_none_coercion_base_dir(self):
        p = PhaseDefinition(id="p", name="P", base_dir=None)
        assert p.base_dir == ""

    def test_existing_fields_unaffected(self):
        """Existing fields still work after adding new ones."""
        p = PhaseDefinition(id="p", name="P", retries=2, retry_delay_seconds=10)
        assert p.retries == 2
        assert p.retry_delay_seconds == 10
        assert p.write_files is False


# ---------------------------------------------------------------------------
# Helpers: load a template from a YAML string via TemplateEngine
# ---------------------------------------------------------------------------

def _template_from_yaml(yaml_str: str, tmp_path: Path) -> PipelineTemplate:
    """Write *yaml_str* to a temp file and parse it with TemplateEngine."""
    tpl_file = tmp_path / "template.yaml"
    tpl_file.write_text(yaml_str)
    engine = TemplateEngine(templates_dir=tmp_path)
    return engine.load_template(tpl_file)


# ---------------------------------------------------------------------------
# 2. YAML parsing — new fields flow through known_fields
# ---------------------------------------------------------------------------

class TestYAMLParsing:
    _TEMPLATE_YAML = """
id: test-pipeline
name: Test Pipeline
phases:
  - id: gen_code
    name: Generate Code
    write_files: true
    working_dir: /tmp/output
    base_dir: /tmp
    prompt_template: "Write code"
"""

    def test_write_files_parsed_from_yaml(self, tmp_path):
        template = _template_from_yaml(self._TEMPLATE_YAML, tmp_path)
        phase = template.phases[0]
        assert phase.write_files is True

    def test_working_dir_parsed_from_yaml(self, tmp_path):
        template = _template_from_yaml(self._TEMPLATE_YAML, tmp_path)
        phase = template.phases[0]
        assert phase.working_dir == "/tmp/output"

    def test_base_dir_parsed_from_yaml(self, tmp_path):
        template = _template_from_yaml(self._TEMPLATE_YAML, tmp_path)
        phase = template.phases[0]
        assert phase.base_dir == "/tmp"

    def test_defaults_when_absent_from_yaml(self, tmp_path):
        yaml_str = """
id: test-pipeline
name: Test Pipeline
phases:
  - id: gen_code
    name: Generate Code
    prompt_template: "Write code"
"""
        template = _template_from_yaml(yaml_str, tmp_path)
        phase = template.phases[0]
        assert phase.write_files is False
        assert phase.working_dir == "."
        assert phase.base_dir == ""

    def test_write_files_false_explicit(self, tmp_path):
        yaml_str = """
id: test-pipeline
name: Test Pipeline
phases:
  - id: gen_code
    name: Generate Code
    write_files: false
    prompt_template: "Write code"
"""
        template = _template_from_yaml(yaml_str, tmp_path)
        assert template.phases[0].write_files is False


# ---------------------------------------------------------------------------
# 3. _handle_file_write — unit tests for the helper method
# ---------------------------------------------------------------------------

class TestHandleFileWrite:
    """Unit-test PhaseSequencer._handle_file_write in isolation."""

    def _make_sequencer(self, config: dict | None = None) -> PhaseSequencer:
        template = PipelineTemplate(
            id="t", name="T",
            phases=[PhaseDefinition(id="p", name="P")],
        )
        runner = MagicMock()
        runner.executors = []
        runner.queue = MagicMock()
        return PhaseSequencer(template, runner, config=config or {})

    # --- Normal write -------------------------------------------------------

    def test_files_written_to_disk(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (tmp_path / "hello.py").exists()
        assert result["metadata"]["files_written"] == ["hello.py"]

    def test_multi_file_written(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_MULTI_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (tmp_path / "src" / "foo.py").exists()
        assert (tmp_path / "README.md").exists()
        assert sorted(result["metadata"]["files_written"]) == sorted(["src/foo.py", "README.md"])

    def test_file_content_correct(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (tmp_path / "hello.py").read_text() == "print('hello')\n"

    def test_files_written_empty_when_no_blocks(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result("No FILE blocks here.")
        seq._handle_file_write(phase, result)
        assert result["metadata"]["files_written"] == []

    # --- Skips on failure ---------------------------------------------------

    def test_skips_on_failed_state(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT, state="failed")
        seq._handle_file_write(phase, result)
        assert not (tmp_path / "hello.py").exists()
        assert "files_written" not in result.get("metadata", {})

    def test_skips_on_permanently_failed_state(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT, state="permanently_failed")
        seq._handle_file_write(phase, result)
        assert not (tmp_path / "hello.py").exists()

    # --- Empty text ---------------------------------------------------------

    def test_empty_text_sets_files_written_empty(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result("")
        seq._handle_file_write(phase, result)
        assert result["metadata"]["files_written"] == []

    # --- Dry-run ------------------------------------------------------------

    def test_dry_run_no_files_on_disk(self, tmp_path):
        seq = self._make_sequencer(config={"dry_run": True})
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert not (tmp_path / "hello.py").exists()

    def test_dry_run_files_written_is_empty(self, tmp_path):
        seq = self._make_sequencer(config={"dry_run": True})
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert result["metadata"]["files_written"] == []

    def test_dry_run_logs_would_write(self, tmp_path, caplog):
        seq = self._make_sequencer(config={"dry_run": True})
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        with caplog.at_level(logging.INFO):
            seq._handle_file_write(phase, result)
        assert any("dry-run" in record.message for record in caplog.records)
        assert any("hello.py" in record.message for record in caplog.records)

    def test_dry_run_false_writes_normally(self, tmp_path):
        seq = self._make_sequencer(config={"dry_run": False})
        phase = _make_phase(working_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (tmp_path / "hello.py").exists()

    # --- Safety: base_dir check ---------------------------------------------

    def test_safety_rejects_working_dir_outside_base_dir(self, tmp_path):
        base = tmp_path / "base"
        outside = tmp_path / "outside"
        base.mkdir()
        outside.mkdir()
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(outside), base_dir=str(base))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        # File must NOT be written
        assert not (outside / "hello.py").exists()
        assert result["metadata"]["files_written"] == []

    def test_safety_logs_error_for_escape(self, tmp_path, caplog):
        base = tmp_path / "base"
        outside = tmp_path / "outside"
        base.mkdir()
        outside.mkdir()
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(outside), base_dir=str(base))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        with caplog.at_level(logging.ERROR):
            seq._handle_file_write(phase, result)
        assert any("outside" in record.message or "base_dir" in record.message
                   for record in caplog.records)

    def test_safety_allows_working_dir_equal_base_dir(self, tmp_path):
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path), base_dir=str(tmp_path))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (tmp_path / "hello.py").exists()

    def test_safety_allows_working_dir_inside_base_dir(self, tmp_path):
        base = tmp_path
        working = tmp_path / "out"
        working.mkdir()
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(working), base_dir=str(base))
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (working / "hello.py").exists()

    def test_no_base_dir_working_dir_is_own_boundary(self, tmp_path):
        """When base_dir='', working_dir is used as both root and boundary."""
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path), base_dir="")
        result = _make_result(_SIMPLE_FILE_OUTPUT)
        seq._handle_file_write(phase, result)
        assert (tmp_path / "hello.py").exists()
        assert result["metadata"]["files_written"] == ["hello.py"]

    # --- Metadata always set ------------------------------------------------

    def test_metadata_files_written_key_always_set(self, tmp_path):
        """files_written key must be present regardless of outcome."""
        seq = self._make_sequencer()
        phase = _make_phase(working_dir=str(tmp_path))

        for text in ["", "no FILE blocks", _SIMPLE_FILE_OUTPUT]:
            result = _make_result(text)
            seq._handle_file_write(phase, result)
            assert "files_written" in result["metadata"]


# ---------------------------------------------------------------------------
# 4. Sequencer integration — write_files=False → no file writing called
# ---------------------------------------------------------------------------

class TestSequencerWriteFilesFlag:
    """Verify write_files=False skips _handle_file_write entirely."""

    def test_write_files_false_not_called(self, tmp_path):
        phase = _make_phase(write_files=False, working_dir=str(tmp_path))
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        seq.execute({"input": "go"})
        # File must NOT appear on disk
        assert not (tmp_path / "hello.py").exists()

    def test_write_files_true_writes_file(self, tmp_path):
        phase = _make_phase(write_files=True, working_dir=str(tmp_path))
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        seq.execute({"input": "go"})
        assert (tmp_path / "hello.py").exists()


# ---------------------------------------------------------------------------
# 5. End-to-end integration via execute()
# ---------------------------------------------------------------------------

class TestEndToEndFileWrite:
    """Full execute() path with real file I/O."""

    def test_single_phase_writes_file(self, tmp_path):
        phase = _make_phase(
            phase_id="codegen",
            write_files=True,
            working_dir=str(tmp_path),
        )
        template = PipelineTemplate(id="e2e", name="E2E", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        pipeline_result = seq.execute({"input": "go"})

        # File on disk
        assert (tmp_path / "hello.py").exists()
        assert (tmp_path / "hello.py").read_text() == "print('hello')\n"

        # Metadata in phase_outputs
        phase_out = pipeline_result["phase_outputs"]["codegen"]
        assert "files_written" in phase_out["metadata"]
        assert phase_out["metadata"]["files_written"] == ["hello.py"]

    def test_files_written_metadata_in_final_output(self, tmp_path):
        phase = _make_phase(
            phase_id="codegen",
            write_files=True,
            working_dir=str(tmp_path),
        )
        template = PipelineTemplate(id="e2e", name="E2E", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({"input": "go"})
        final = result["final_output"]
        assert "files_written" in final["metadata"]

    def test_multi_file_e2e(self, tmp_path):
        phase = _make_phase(
            phase_id="gen",
            write_files=True,
            working_dir=str(tmp_path),
        )
        template = PipelineTemplate(id="e2e", name="E2E", phases=[phase])
        runner = _make_runner(_make_task_result(_MULTI_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        seq.execute({"input": "go"})
        assert (tmp_path / "src" / "foo.py").exists()
        assert (tmp_path / "README.md").exists()

    def test_failed_phase_no_files(self, tmp_path):
        phase = _make_phase(
            phase_id="codegen",
            write_files=True,
            working_dir=str(tmp_path),
        )
        template = PipelineTemplate(id="e2e", name="E2E", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT, state=TaskState.FAILED))
        seq = PhaseSequencer(template, runner)
        seq.execute({"input": "go"})
        assert not (tmp_path / "hello.py").exists()


# ---------------------------------------------------------------------------
# 6. Dry-run end-to-end
# ---------------------------------------------------------------------------

class TestDryRunEndToEnd:
    def test_dry_run_via_config_no_files(self, tmp_path):
        phase = _make_phase(write_files=True, working_dir=str(tmp_path))
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner, config={"dry_run": True})
        seq.execute({"input": "go"})
        assert not (tmp_path / "hello.py").exists()

    def test_dry_run_via_config_files_written_empty(self, tmp_path):
        phase = _make_phase(phase_id="p", write_files=True, working_dir=str(tmp_path))
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner, config={"dry_run": True})
        result = seq.execute({"input": "go"})
        assert result["phase_outputs"]["p"]["metadata"]["files_written"] == []

    def test_dry_run_logs_info(self, tmp_path, caplog):
        phase = _make_phase(phase_id="p", write_files=True, working_dir=str(tmp_path))
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner, config={"dry_run": True})
        with caplog.at_level(logging.INFO):
            seq.execute({"input": "go"})
        assert any("dry-run" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 7. Safety — base_dir enforcement via execute()
# ---------------------------------------------------------------------------

class TestSafetyEndToEnd:
    def test_working_dir_outside_base_dir_no_write(self, tmp_path):
        base = tmp_path / "safe"
        unsafe = tmp_path / "unsafe"
        base.mkdir(); unsafe.mkdir()

        phase = _make_phase(
            phase_id="p",
            write_files=True,
            working_dir=str(unsafe),
            base_dir=str(base),
        )
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        seq.execute({"input": "go"})
        assert not (unsafe / "hello.py").exists()

    def test_working_dir_outside_base_dir_files_written_empty(self, tmp_path):
        base = tmp_path / "safe"
        unsafe = tmp_path / "unsafe"
        base.mkdir(); unsafe.mkdir()

        phase = _make_phase(
            phase_id="p",
            write_files=True,
            working_dir=str(unsafe),
            base_dir=str(base),
        )
        template = PipelineTemplate(id="t", name="T", phases=[phase])
        runner = _make_runner(_make_task_result(_SIMPLE_FILE_OUTPUT))
        seq = PhaseSequencer(template, runner)
        result = seq.execute({"input": "go"})
        assert result["phase_outputs"]["p"]["metadata"]["files_written"] == []
