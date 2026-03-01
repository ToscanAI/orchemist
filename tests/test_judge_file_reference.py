"""Tests for Issue #286 — Judge file-reference handoff.

Pass .md file paths (not raw dict content) to the LLM judge when output_dir
is available.  Tests cover:

  JFR-1:  Happy path — _final_output.md read when output_dir is given
  JFR-2:  output_field resolves to a per-phase .md file
  JFR-3:  Hyphenated output_field uses underscores in filename
  JFR-4:  Falls back to _final_output.md when per-phase file missing
  JFR-5:  Falls back to dict extraction when no .md files exist
  JFR-6:  output_dir=None → backward-compatible dict extraction
  JFR-7:  ScenarioRunner.run_scenario() propagates output_dir to judge
  JFR-8:  scoring.run_scoring() passes output_dir to run_scenario()
  JFR-9:  Dry-run mode bypasses file reads entirely
  JFR-10: _read_output_file() returns None on OSError (permission denied)
  JFR-11: File content stripped of leading/trailing whitespace
  JFR-12: Empty .md file → fallback, not empty string to judge
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scenario_runner.graders.llm_judge import LLMJudgeGrader
from scenario_runner.models import GradeResult
from scenario_runner.runner import ScenarioRunner


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_executor(response_text: str = "Score: 0.75\nGood.") -> MagicMock:
    """Return a mock executor that captures the text passed to it and returns a score."""
    from orchestration_engine.schemas import TaskState

    task_result = MagicMock()
    task_result.state = TaskState.SUCCESS
    task_result.result = {"text": response_text}
    task_result.errors = []

    executor = MagicMock()
    executor.execute.return_value = task_result
    return executor


def _captured_text_from_executor(executor: MagicMock) -> str:
    """Extract the article text sent to the executor from the TaskSpec payload."""
    call_args = executor.execute.call_args
    task_spec = call_args[0][0]
    prompt: str = task_spec.payload["prompt"]
    # The prompt is formatted as "## Rubric\n\n{rubric}\n\n## Article to Evaluate\n\n{text}"
    marker = "## Article to Evaluate\n\n"
    idx = prompt.find(marker)
    if idx == -1:
        return prompt
    return prompt[idx + len(marker):]


# ---------------------------------------------------------------------------
# _read_output_file unit tests
# ---------------------------------------------------------------------------


class TestReadOutputFile:
    """Direct unit tests for LLMJudgeGrader._read_output_file()."""

    def test_jfr11_content_stripped_of_whitespace(self, tmp_path: Path):
        """JFR-11: File content is stripped of leading/trailing whitespace."""
        (tmp_path / "_final_output.md").write_text(
            "\n\n  article text  \n\n", encoding="utf-8"
        )
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, None)
        assert result == "article text"

    def test_returns_none_when_no_files(self, tmp_path: Path):
        """Returns None when neither per-phase nor _final_output.md exists."""
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, None)
        assert result is None

    def test_jfr10_returns_none_on_oserror(self, tmp_path: Path):
        """JFR-10: Returns None on OSError (e.g., permission denied)."""
        final_md = tmp_path / "_final_output.md"
        final_md.write_text("secret", encoding="utf-8")
        # Remove read permission
        final_md.chmod(0o000)
        try:
            grader = LLMJudgeGrader()
            result = grader._read_output_file(tmp_path, None)
            assert result is None
        finally:
            # Restore permissions so tmp_path cleanup can proceed
            final_md.chmod(0o644)

    def test_jfr12_empty_file_returns_none(self, tmp_path: Path):
        """JFR-12: Empty (zero-byte or whitespace-only) .md file returns None."""
        (tmp_path / "_final_output.md").write_text("   \n  ", encoding="utf-8")
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, None)
        assert result is None

    def test_jfr3_hyphen_converted_to_underscore(self, tmp_path: Path):
        """JFR-3: Hyphenated output_field uses underscores in filename lookup."""
        (tmp_path / "fact_check.md").write_text("checked content", encoding="utf-8")
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, "fact-check")
        assert result == "checked content"

    def test_output_field_exact_match(self, tmp_path: Path):
        """output_field without hyphens resolves directly."""
        (tmp_path / "write.md").write_text("written article", encoding="utf-8")
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, "write")
        assert result == "written article"

    def test_jfr4_falls_back_to_final_output_when_phase_missing(self, tmp_path: Path):
        """JFR-4: Falls back to _final_output.md when per-phase file is missing."""
        (tmp_path / "_final_output.md").write_text("final content", encoding="utf-8")
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, "nonexistent")
        assert result == "final content"

    def test_per_phase_file_takes_priority_over_final(self, tmp_path: Path):
        """Per-phase .md file is preferred over _final_output.md."""
        (tmp_path / "write.md").write_text("write phase content", encoding="utf-8")
        (tmp_path / "_final_output.md").write_text("final content", encoding="utf-8")
        grader = LLMJudgeGrader()
        result = grader._read_output_file(tmp_path, "write")
        assert result == "write phase content"


# ---------------------------------------------------------------------------
# grade() with output_dir — integration tests
# ---------------------------------------------------------------------------


class TestGradeWithOutputDir:
    """grade() correctly uses output_dir for file-based text extraction."""

    def test_jfr1_reads_final_output_md(self, tmp_path: Path):
        """JFR-1: grade() reads _final_output.md and sends its content to judge."""
        (tmp_path / "_final_output.md").write_text(
            "clean article text", encoding="utf-8"
        )
        executor = _make_executor("Score: 0.75\nGood article.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade(
            output={"phases": {"p1": "something"}, "final": {}},
            rubric="Rate the article.",
            judge_model="claude-haiku-4-5-20241022",
            output_dir=tmp_path,
        )

        assert result.score == pytest.approx(0.75)
        sent_text = _captured_text_from_executor(executor)
        assert "clean article text" in sent_text
        # The large JSON blob should NOT be sent
        assert '"phases"' not in sent_text

    def test_jfr2_output_field_resolves_per_phase_md(self, tmp_path: Path):
        """JFR-2: output_field resolves to a per-phase .md file."""
        (tmp_path / "write.md").write_text("the written article", encoding="utf-8")
        (tmp_path / "_final_output.md").write_text("final content", encoding="utf-8")
        executor = _make_executor("Score: 0.80\nGreat.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade(
            output={"article": "old dict content"},
            rubric="Rate quality.",
            judge_model="claude-haiku-4-5-20241022",
            output_field="write",
            output_dir=tmp_path,
        )

        sent_text = _captured_text_from_executor(executor)
        assert "the written article" in sent_text
        assert "old dict content" not in sent_text

    def test_jfr3_hyphenated_field_uses_underscore_filename(self, tmp_path: Path):
        """JFR-3: output_field='fact-check' resolves to fact_check.md."""
        (tmp_path / "fact_check.md").write_text("checked content", encoding="utf-8")
        executor = _make_executor("Score: 0.70\nFact-checked.")
        grader = LLMJudgeGrader(executor=executor)

        grader.grade(
            output={"article": "irrelevant"},
            rubric="Rate accuracy.",
            judge_model="claude-haiku-4-5-20241022",
            output_field="fact-check",
            output_dir=tmp_path,
        )

        sent_text = _captured_text_from_executor(executor)
        assert "checked content" in sent_text

    def test_jfr4_falls_back_to_final_output_when_phase_missing(self, tmp_path: Path):
        """JFR-4: Falls back to _final_output.md when per-phase file doesn't exist."""
        (tmp_path / "_final_output.md").write_text("final content", encoding="utf-8")
        # nonexistent.md is NOT created
        executor = _make_executor("Score: 0.65\nOK.")
        grader = LLMJudgeGrader(executor=executor)

        grader.grade(
            output={"article": "dict article"},
            rubric="Rate quality.",
            judge_model="claude-haiku-4-5-20241022",
            output_field="nonexistent",
            output_dir=tmp_path,
        )

        sent_text = _captured_text_from_executor(executor)
        assert "final content" in sent_text
        assert "dict article" not in sent_text

    def test_jfr5_falls_back_to_dict_when_no_md_files(self, tmp_path: Path):
        """JFR-5: Falls back to dict extraction when no .md files exist."""
        executor = _make_executor("Score: 0.60\nOK.")
        grader = LLMJudgeGrader(executor=executor)

        grader.grade(
            output={"article": "dict fallback content"},
            rubric="Rate quality.",
            judge_model="claude-haiku-4-5-20241022",
            output_dir=tmp_path,  # directory exists but has no .md files
        )

        sent_text = _captured_text_from_executor(executor)
        assert "dict fallback content" in sent_text

    def test_jfr6_output_dir_none_uses_dict_extraction(self, tmp_path: Path):
        """JFR-6: output_dir=None → backward-compatible dict extraction, no file I/O."""
        executor = _make_executor("Score: 0.85\nGood.")
        grader = LLMJudgeGrader(executor=executor)

        # Even if a .md file exists in cwd, it must NOT be read
        grader.grade(
            output={"article": "article text"},
            rubric="Rate quality.",
            judge_model="claude-haiku-4-5-20241022",
            # output_dir intentionally omitted
        )

        sent_text = _captured_text_from_executor(executor)
        assert "article text" in sent_text

    def test_jfr9_dry_run_bypasses_file_reads(self, tmp_path: Path):
        """JFR-9: ORCH_DRY_RUN=1 returns stub without reading any files."""
        (tmp_path / "_final_output.md").write_text("file content", encoding="utf-8")
        executor = _make_executor("Score: 0.99\nPerfect.")
        grader = LLMJudgeGrader(executor=executor)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={},
                rubric="rubric",
                judge_model="model",
                output_dir=tmp_path,
            )

        # Dry-run stub returned
        assert result.score == pytest.approx(0.8)
        # Executor should not be called
        executor.execute.assert_not_called()

    def test_jfr12_empty_md_falls_back_to_dict(self, tmp_path: Path):
        """JFR-12: Empty .md file falls back to dict extraction."""
        (tmp_path / "_final_output.md").write_text("   \n  ", encoding="utf-8")
        executor = _make_executor("Score: 0.72\nOK.")
        grader = LLMJudgeGrader(executor=executor)

        grader.grade(
            output={"article": "dict article text"},
            rubric="Rate quality.",
            judge_model="claude-haiku-4-5-20241022",
            output_dir=tmp_path,
        )

        sent_text = _captured_text_from_executor(executor)
        assert "dict article text" in sent_text

    def test_grade_result_score_correct_from_file_path(self, tmp_path: Path):
        """Grade result score is parsed from executor response when file read used."""
        (tmp_path / "_final_output.md").write_text("great article", encoding="utf-8")
        executor = _make_executor("Score: 0.92\nExcellent quality.")
        grader = LLMJudgeGrader(executor=executor)

        result = grader.grade(
            output={},
            rubric="Rate quality.",
            judge_model="claude-haiku-4-5-20241022",
            output_dir=tmp_path,
        )

        assert result.score == pytest.approx(0.92)
        assert result.passed is True
        assert result.grader_type == "llm_judge"

    def test_jfr10_oserror_falls_back_to_dict(self, tmp_path: Path):
        """JFR-10: OSError on file read falls back to dict extraction."""
        final_md = tmp_path / "_final_output.md"
        final_md.write_text("file content", encoding="utf-8")
        final_md.chmod(0o000)

        executor = _make_executor("Score: 0.68\nOK.")
        grader = LLMJudgeGrader(executor=executor)

        try:
            grader.grade(
                output={"article": "dict fallback"},
                rubric="Rate quality.",
                judge_model="claude-haiku-4-5-20241022",
                output_dir=tmp_path,
            )
            sent_text = _captured_text_from_executor(executor)
            assert "dict fallback" in sent_text
        finally:
            final_md.chmod(0o644)

    def test_output_dir_with_api_key_reads_file(self, tmp_path: Path):
        """output_dir works with the api_key path too (not just executor)."""
        from io import BytesIO
        import json as _json

        (tmp_path / "_final_output.md").write_text("file based content", encoding="utf-8")

        body = _json.dumps({"content": [{"text": "Score: 0.80\nGood."}]}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured_requests: list = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            return mock_resp

        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ORCH_DRY_RUN")}
        with patch.dict("os.environ", env, clear=True):
            grader = LLMJudgeGrader(api_key="sk-test")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = grader.grade(
                output={"final": {"big": "json blob that should not be sent"}},
                rubric="Rate quality.",
                judge_model="claude-haiku-4-5-20241022",
                output_dir=tmp_path,
            )

        assert result.score == pytest.approx(0.80)
        # Verify file content sent, not the JSON blob
        assert len(captured_requests) == 1
        import json as _j
        payload = _j.loads(captured_requests[0].data.decode())
        prompt_text = payload["messages"][0]["content"]
        assert "file based content" in prompt_text
        assert "big" not in prompt_text


# ---------------------------------------------------------------------------
# ScenarioRunner.run_scenario() output_dir propagation
# ---------------------------------------------------------------------------


class TestScenarioRunnerOutputDirPropagation:
    """ScenarioRunner.run_scenario() passes output_dir through to the judge."""

    def test_jfr7_output_dir_propagated_to_judge(self, tmp_path: Path):
        """JFR-7: run_scenario() passes output_dir to the LLM judge grader."""
        (tmp_path / "_final_output.md").write_text(
            "clean pipeline output", encoding="utf-8"
        )
        executor = _make_executor("Score: 0.78\nGood.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "file-ref-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate the article quality. Score 0.0–1.0.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        result = runner.run_scenario(
            scenario,
            pipeline_output={"article": "old dict content that must not be sent"},
            output_dir=tmp_path,
        )

        executor.execute.assert_called_once()
        sent_text = _captured_text_from_executor(executor)
        assert "clean pipeline output" in sent_text
        assert "old dict content" not in sent_text
        assert result.criterion_results[0].grade.score == pytest.approx(0.78)

    def test_run_scenario_without_output_dir_is_backward_compatible(self, tmp_path: Path):
        """run_scenario() without output_dir uses dict-based extraction."""
        executor = _make_executor("Score: 0.82\nGood.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "backward-compat-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate the article quality.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        result = runner.run_scenario(
            scenario,
            pipeline_output={"article": "article via dict"},
            # output_dir intentionally omitted
        )

        sent_text = _captured_text_from_executor(executor)
        assert "article via dict" in sent_text

    def test_output_field_in_criterion_forwarded_to_judge(self, tmp_path: Path):
        """output_field from criterion YAML is forwarded to grade() via _grade_criterion()."""
        (tmp_path / "write.md").write_text("write phase text", encoding="utf-8")
        executor = _make_executor("Score: 0.77\nOK.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "output-field-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate quality.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "output_field": "write",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        runner.run_scenario(
            scenario,
            pipeline_output={"article": "dict content"},
            output_dir=tmp_path,
        )

        sent_text = _captured_text_from_executor(executor)
        assert "write phase text" in sent_text
        assert "dict content" not in sent_text

    def test_non_llm_judge_criteria_unaffected_by_output_dir(self, tmp_path: Path):
        """Non-llm_judge criteria (assertion, keyword) are unaffected by output_dir."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)

        scenario = {
            "id": "assertion-test",
            "acceptance": [
                {
                    "id": "not_empty",
                    "type": "assertion",
                    "check": "True",
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        result = runner.run_scenario(
            scenario,
            pipeline_output={},
            output_dir=tmp_path,
        )

        assert result.criterion_results[0].grade.passed is True


# ---------------------------------------------------------------------------
# scoring.run_scoring() passes output_dir to run_scenario()
# ---------------------------------------------------------------------------


class TestRunScoringOutputDirPropagation:
    """scoring.run_scoring() forwards output_dir to ScenarioRunner.run_scenario()."""

    def test_jfr8_run_scoring_passes_output_dir_to_run_scenario(
        self, tmp_path: Path
    ):
        """JFR-8: run_scoring() calls run_scenario() with the correct output_dir."""
        from orchestration_engine.scoring import run_scoring
        from orchestration_engine.templates import PipelineTemplate

        # Create scenario file
        scenario_path = tmp_path / "test-scenario.yaml"
        scenario_path.write_text(
            "id: test-scenario\n"
            "acceptance:\n"
            "  - id: c1\n"
            "    type: assertion\n"
            "    check: 'True'\n"
            "    weight: 1\n"
            "scoring:\n"
            "  pass_threshold: 0.5\n",
            encoding="utf-8",
        )

        # Fake output dir
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        (out_dir / "_final_output.md").write_text("pipeline output", encoding="utf-8")
        (out_dir / "_final_output.json").write_text("{}", encoding="utf-8")

        template = PipelineTemplate(id="t", name="T", scenario="test-scenario.yaml")
        template.template_path = tmp_path / "t.yaml"

        mock_score_result = MagicMock()
        mock_score_result.passed = True
        mock_score_result.weighted_score = 0.9
        mock_score_result.gates_passed = True
        mock_score_result.scenario_id = "test-scenario"
        mock_score_result.criterion_results = []

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            mock_instance.load_scenario.return_value = {
                "id": "test-scenario",
                "acceptance": [],
                "scoring": {"pass_threshold": 0.5},
            }
            mock_instance.run_scenario.return_value = mock_score_result

            from rich.console import Console
            console = Console(file=open(os.devnull, "w"))
            run_scoring(
                template,
                output_dir=out_dir,
                console=console,
                exit_on_failure=False,
            )

        # Verify run_scenario was called with output_dir kwarg set to out_dir
        call_kwargs = mock_instance.run_scenario.call_args[1]
        assert "output_dir" in call_kwargs
        assert call_kwargs["output_dir"] == out_dir

    def test_run_scoring_output_dir_not_none(self, tmp_path: Path):
        """run_scenario is NOT called with output_dir=None from run_scoring()."""
        from orchestration_engine.scoring import run_scoring
        from orchestration_engine.templates import PipelineTemplate

        scenario_path = tmp_path / "s.yaml"
        scenario_path.write_text(
            "id: s\nacceptance: []\nscoring:\n  pass_threshold: 0.5\n",
            encoding="utf-8",
        )

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "_final_output.json").write_text("{}", encoding="utf-8")

        template = PipelineTemplate(id="t", name="T", scenario="s.yaml")
        template.template_path = tmp_path / "t.yaml"

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.weighted_score = 1.0
        mock_result.gates_passed = True
        mock_result.scenario_id = "s"
        mock_result.criterion_results = []

        with patch("scenario_runner.runner.ScenarioRunner") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            mock_instance.load_scenario.return_value = {
                "id": "s",
                "acceptance": [],
                "scoring": {"pass_threshold": 0.5},
            }
            mock_instance.run_scenario.return_value = mock_result

            from rich.console import Console
            console = Console(file=open(os.devnull, "w"))
            run_scoring(
                template,
                output_dir=out_dir,
                console=console,
                exit_on_failure=False,
            )

        call_kwargs = mock_instance.run_scenario.call_args[1]
        assert call_kwargs.get("output_dir") is not None
        assert call_kwargs.get("output_dir") == out_dir
