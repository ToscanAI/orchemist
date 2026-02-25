"""Tests for the scenario runner system.

Covers:
- Scenario YAML loading (valid and invalid)
- AssertionGrader: passing, failing, malicious expressions blocked
- LLMJudgeGrader: no-key graceful degradation, mock API call, holdout principle
- URLCheckGrader: all reachable, partial failure, no URLs
- Full scenario grading with mixed criteria
- Weighted score calculation
- Gate mode (any gate fail → whole scenario fails)
- SuiteResult satisfaction rate
"""

from __future__ import annotations

import json
import tempfile
import textwrap
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Ensure the project root is importable (pytest rootdir already handles this
# for most setups, but be explicit for CI environments)
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scenario_runner.graders.assertion import AssertionGrader
from scenario_runner.graders.llm_judge import LLMJudgeGrader
from scenario_runner.graders.url_check import URLCheckGrader
from scenario_runner.models import GradeResult, ScenarioResult, SuiteResult
from scenario_runner.runner import ScenarioRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_scenarios_dir(tmp_path: Path) -> Path:
    """Return a temp directory with a minimal valid scenario YAML."""
    (tmp_path / "shared" / "rubrics").mkdir(parents=True)
    (tmp_path / "content-pipeline").mkdir()

    # Write a shared rubric file
    (tmp_path / "shared" / "rubrics" / "sample-rubric.md").write_text(
        "Score: 0.9\nThis is a sample rubric.\n"
    )

    # Write a minimal happy-path scenario
    happy = {
        "id": "test-happy-path-001",
        "version": 1,
        "pipeline": "content-pipeline",
        "name": "Happy path test scenario",
        "acceptance": [
            {
                "id": "not_empty",
                "type": "assertion",
                "check": "len(output.get('article', '')) > 10",
                "weight": 0,  # gate
            },
            {
                "id": "word_count",
                "type": "assertion",
                "check": "len(output.get('article', '').split()) >= 5",
                "weight": 0,  # gate
            },
            {
                "id": "quality_check",
                "type": "llm_judge",
                "judge_model": "claude-haiku-4-5-20241022",
                "rubric": "Does this article make sense? Score: [0.0-1.0]",
                "threshold": 0.6,
                "weight": 2,
            },
            {
                "id": "url_reachability",
                "type": "url_check",
                "threshold": 0.80,
                "weight": 1,
            },
        ],
        "scoring": {
            "method": "weighted_average",
            "pass_threshold": 0.70,
            "gate_mode": "all_or_nothing",
        },
    }
    scenario_path = tmp_path / "content-pipeline" / "happy-path-001.yaml"
    scenario_path.write_text(yaml.dump(happy))

    return tmp_path


@pytest.fixture()
def runner(tmp_scenarios_dir: Path) -> ScenarioRunner:
    return ScenarioRunner(scenarios_dir=tmp_scenarios_dir / "content-pipeline")


@pytest.fixture()
def assertion_grader() -> AssertionGrader:
    return AssertionGrader()


@pytest.fixture()
def llm_grader() -> LLMJudgeGrader:
    return LLMJudgeGrader(api_key=None)


@pytest.fixture()
def url_grader() -> URLCheckGrader:
    return URLCheckGrader()


# ===========================================================================
# 1 – Scenario YAML loading
# ===========================================================================


class TestLoadScenario:
    def test_load_valid_scenario(self, runner: ScenarioRunner, tmp_scenarios_dir: Path):
        """Load a well-formed scenario YAML without errors."""
        path = tmp_scenarios_dir / "content-pipeline" / "happy-path-001.yaml"
        scenario = runner.load_scenario(path)

        assert scenario["id"] == "test-happy-path-001"
        assert isinstance(scenario["acceptance"], list)
        assert len(scenario["acceptance"]) == 4

    def test_load_real_scenario_from_repo(self):
        """Load one of the actual repo scenarios to ensure they parse correctly."""
        repo_root = Path(__file__).parent.parent
        happy_path = repo_root / "scenarios" / "content-pipeline" / "happy-path-001.yaml"

        if not happy_path.exists():
            pytest.skip("Repo scenario file not found")

        runner = ScenarioRunner(scenarios_dir=repo_root / "scenarios" / "content-pipeline")
        scenario = runner.load_scenario(happy_path)
        assert scenario["id"] == "content-pipeline-happy-path-001"
        assert "acceptance" in scenario

    def test_load_scenario_missing_id(self, tmp_path: Path):
        """Scenario without 'id' key raises ValueError."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(yaml.dump({"acceptance": []}))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        with pytest.raises(ValueError, match="missing required key 'id'"):
            runner.load_scenario(bad_yaml)

    def test_load_scenario_missing_acceptance(self, tmp_path: Path):
        """Scenario without 'acceptance' key raises ValueError."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(yaml.dump({"id": "test-123"}))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        with pytest.raises(ValueError, match="missing required key 'acceptance'"):
            runner.load_scenario(bad_yaml)

    def test_load_scenario_acceptance_not_list(self, tmp_path: Path):
        """Scenario with 'acceptance' as a non-list raises ValueError."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(yaml.dump({"id": "test-123", "acceptance": "not a list"}))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        with pytest.raises(ValueError, match="'acceptance' must be a list"):
            runner.load_scenario(bad_yaml)

    def test_load_scenario_file_not_found(self, tmp_path: Path):
        """Missing file raises FileNotFoundError."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            runner.load_scenario(tmp_path / "nonexistent.yaml")


# ===========================================================================
# 2 – Assertion grader
# ===========================================================================


class TestAssertionGrader:
    def test_passing_check(self, assertion_grader: AssertionGrader):
        """Expression that evaluates to True gives score 1.0, passed=True."""
        output = {"article": "Hello world, this is a test article"}
        result = assertion_grader.grade(
            "len(output.get('article', '')) > 10", output
        )
        assert result.passed is True
        assert result.score == 1.0
        assert result.grader_type == "assertion"

    def test_failing_check(self, assertion_grader: AssertionGrader):
        """Expression that evaluates to False gives score 0.0, passed=False."""
        output = {"article": "Hi"}
        result = assertion_grader.grade(
            "len(output.get('article', '')) > 100", output
        )
        assert result.passed is False
        assert result.score == 0.0
        assert result.grader_type == "assertion"

    def test_missing_key_returns_false(self, assertion_grader: AssertionGrader):
        """Accessing a missing key via .get() returns default; expression still works."""
        output = {}
        result = assertion_grader.grade(
            "len(output.get('article', '')) > 0", output
        )
        assert result.passed is False
        assert result.score == 0.0

    def test_complex_expression(self, assertion_grader: AssertionGrader):
        """Multi-part boolean expression with split() works."""
        output = {"article": "word " * 50}
        result = assertion_grader.grade(
            "200 <= len(output.get('article', '').split()) * 4 <= 1000", output
        )
        assert result.passed is True

    def test_malicious_import_blocked(self, assertion_grader: AssertionGrader):
        """Expression containing '__import__' is blocked before eval."""
        output = {}
        result = assertion_grader.grade("__import__('os').system('id')", output)
        assert result.passed is False
        assert result.score == 0.0
        assert "Blocked" in result.details

    def test_malicious_exec_blocked(self, assertion_grader: AssertionGrader):
        """Expression containing 'exec(' is blocked — either by AST or namespace restriction."""
        output = {}
        result = assertion_grader.grade("exec('import os')", output)
        assert result.passed is False
        assert result.score == 0.0

    def test_malicious_builtins_attribute_blocked(self, assertion_grader: AssertionGrader):
        """Expression trying to access __builtins__ is blocked."""
        output = {}
        result = assertion_grader.grade("__builtins__['__import__']('os')", output)
        assert result.passed is False
        assert result.score == 0.0

    def test_safe_builtins_available(self, assertion_grader: AssertionGrader):
        """len, str, int, float, bool are all accessible in assertions."""
        output = {"value": "42"}
        result = assertion_grader.grade(
            "bool(int(float(str(len(output)))))", output
        )
        assert result.passed is True  # bool(int(float(str(1)))) == bool(1) == True

    def test_expression_runtime_error_returns_failed(self, assertion_grader: AssertionGrader):
        """Expression that raises at runtime returns passed=False with error detail."""
        output = {"article": None}
        result = assertion_grader.grade(
            "len(output['article']) > 0", output
        )
        assert result.passed is False
        assert "error" in result.details.lower()


# ===========================================================================
# 3 – LLM Judge grader
# ===========================================================================


class TestLLMJudgeGrader:
    def test_no_api_key_returns_zero(self):
        """Missing API key → score=0.0, details='No API key configured'."""
        grader = LLMJudgeGrader(api_key=None)
        # Remove any env variable that might be set
        with patch.dict("os.environ", {}, clear=True):
            grader2 = LLMJudgeGrader(api_key=None)
        result = grader2.grade(
            output={"article": "Some article text."},
            rubric="Rate this article 0.0-1.0.",
            judge_model="claude-haiku-4-5-20241022",
        )
        assert result.passed is False
        assert result.score == 0.0
        assert "No API key" in result.details
        assert result.grader_type == "llm_judge"

    def test_holdout_principle_only_article_and_rubric_sent(self):
        """Verify the API payload contains ONLY article text + rubric.

        No scenario metadata (id, threshold, pipeline, tags) should appear
        in the messages sent to the judge model.
        """
        article = "This is the article under evaluation."
        rubric = "Score this article between 0.0 and 1.0."
        scenario_metadata = {
            "scenario_id": "test-scenario-001",
            "pipeline": "content-pipeline",
            "threshold": 0.85,
            "tags": ["adversarial"],
        }

        mock_response_body = json.dumps({
            "content": [{"text": "Score: 0.85\nReasoning: Article is clear."}],
        }).encode("utf-8")

        mock_response = MagicMock()
        mock_response.read.return_value = mock_response_body
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        captured_requests = []

        def fake_urlopen(request, timeout=None):
            captured_requests.append(request)
            return mock_response

        grader = LLMJudgeGrader(api_key="sk-test-fake-key")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = grader.grade(
                output={"article": article, **scenario_metadata},
                rubric=rubric,
                judge_model="claude-haiku-4-5-20241022",
            )

        assert len(captured_requests) == 1, "Expected exactly one API call"
        request = captured_requests[0]
        payload = json.loads(request.data.decode("utf-8"))

        # The user message must contain the article and rubric
        user_content = payload["messages"][0]["content"]
        assert article in user_content, "Article text must be in the message"
        assert rubric in user_content, "Rubric text must be in the message"

        # Scenario metadata must NOT appear in the payload
        for forbidden in ("scenario_id", "threshold", "adversarial", "content-pipeline"):
            assert forbidden not in user_content, (
                f"Holdout violated: '{forbidden}' found in judge message"
            )

    def test_mock_api_parses_score_correctly(self):
        """Mock API call returns known response; verify score is parsed."""
        mock_response_body = json.dumps({
            "content": [{"text": "Score: 0.75\nThe article is mostly accurate."}],
        }).encode("utf-8")

        mock_response = MagicMock()
        mock_response.read.return_value = mock_response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        grader = LLMJudgeGrader(api_key="sk-test-fake")

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = grader.grade(
                output={"article": "Test article"},
                rubric="Rate 0.0-1.0.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.75)
        assert result.grader_type == "llm_judge"

    def test_mock_api_no_score_in_response(self):
        """When judge response has no 'Score: X.X', score defaults to 0.0."""
        mock_response_body = json.dumps({
            "content": [{"text": "I think the article is decent."}],
        }).encode("utf-8")

        mock_response = MagicMock()
        mock_response.read.return_value = mock_response_body
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        grader = LLMJudgeGrader(api_key="sk-test-fake")

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = grader.grade(
                output={"article": "Test article"},
                rubric="Rate 0.0-1.0.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == 0.0
        assert result.grader_type == "llm_judge"

    def test_api_http_error_handled_gracefully(self):
        """An HTTP error from the API returns score=0.0 with error details."""
        import urllib.error

        grader = LLMJudgeGrader(api_key="sk-test-fake")

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="https://api.anthropic.com/v1/messages",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=BytesIO(b"rate limit exceeded"),
            ),
        ):
            result = grader.grade(
                output={"article": "Test"},
                rubric="Rate.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.passed is False
        assert result.score == 0.0
        assert "429" in result.details


# ===========================================================================
# 4 – URL Check grader
# ===========================================================================


class TestURLCheckGrader:
    def test_no_urls_returns_perfect_score(self, url_grader: URLCheckGrader):
        """Article with no URLs → score=1.0 (vacuous truth)."""
        result = url_grader.grade("This article has no hyperlinks.")
        assert result.score == 1.0
        assert result.passed is True
        assert result.grader_type == "url_check"

    def test_all_urls_reachable(self, url_grader: URLCheckGrader):
        """All URLs return HTTP 200 → score=1.0."""
        article = "See https://example.com and https://example.org for more."

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = url_grader.grade(article)

        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_some_urls_fail(self, url_grader: URLCheckGrader):
        """One of two URLs fails → score=0.5, passed=False (below 0.9 threshold)."""
        article = "See https://good.example.com and https://bad.example.com"

        call_count = [0]

        def mock_urlopen(request, timeout=None):
            url = request.full_url
            call_count[0] += 1
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            if "bad" in url:
                import urllib.error
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            resp.status = 200
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = url_grader.grade(article)

        assert result.score == pytest.approx(0.5)
        assert result.passed is False

    def test_url_extraction_deduplicates(self, url_grader: URLCheckGrader):
        """Duplicate URLs are only checked once."""
        article = "Visit https://example.com and again https://example.com"

        call_count = [0]

        def mock_urlopen(request, timeout=None):
            call_count[0] += 1
            resp = MagicMock()
            resp.status = 200
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = url_grader.grade(article)

        assert call_count[0] == 1, "Duplicate URL should only be checked once"
        assert result.score == 1.0


# ===========================================================================
# 5 – Full scenario grading
# ===========================================================================


class TestRunScenario:
    """Integration-style tests for ScenarioRunner.run_scenario."""

    def _make_scenario(
        self,
        gates: list[dict] | None = None,
        scored: list[dict] | None = None,
        pass_threshold: float = 0.70,
        gate_mode: str = "all_or_nothing",
    ) -> dict:
        acceptance = []
        for g in gates or []:
            acceptance.append({**g, "weight": 0})
        for s in scored or []:
            acceptance.append(s)
        return {
            "id": "test-scenario",
            "acceptance": acceptance,
            "scoring": {
                "method": "weighted_average",
                "pass_threshold": pass_threshold,
                "gate_mode": gate_mode,
            },
        }

    def test_all_pass_scenario(self, tmp_path: Path):
        """All gates pass + score above threshold → scenario passes."""
        scenario = self._make_scenario(
            gates=[
                {"id": "g1", "type": "assertion", "check": "True"},
            ],
            scored=[
                {"id": "s1", "type": "assertion", "check": "True", "weight": 1, "threshold": 0.5},
            ],
            pass_threshold=0.5,
        )
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Hello world!"})

        assert result.passed is True
        assert result.gates_passed is True
        assert result.weighted_score == pytest.approx(1.0)

    def test_gate_fails_scenario_fails(self, tmp_path: Path):
        """One failing gate → scenario fails regardless of scored criteria."""
        scenario = self._make_scenario(
            gates=[
                {"id": "gate_fails", "type": "assertion", "check": "False"},
            ],
            scored=[
                {"id": "s1", "type": "assertion", "check": "True", "weight": 1, "threshold": 0.5},
                {"id": "s2", "type": "assertion", "check": "True", "weight": 1, "threshold": 0.5},
            ],
            pass_threshold=0.5,
            gate_mode="all_or_nothing",
        )
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Some content"})

        assert result.passed is False
        assert result.gates_passed is False
        # Weighted score can be high but scenario still fails
        assert result.weighted_score == pytest.approx(1.0)

    def test_gate_passes_but_score_below_threshold(self, tmp_path: Path):
        """Gates pass but weighted score < threshold → scenario fails."""
        scenario = self._make_scenario(
            gates=[
                {"id": "g1", "type": "assertion", "check": "True"},
            ],
            scored=[
                {"id": "s1", "type": "assertion", "check": "False", "weight": 1, "threshold": 0.5},
            ],
            pass_threshold=0.70,
        )
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Content"})

        assert result.passed is False
        assert result.gates_passed is True
        assert result.weighted_score == pytest.approx(0.0)

    def test_weighted_score_calculation(self, tmp_path: Path):
        """Verify weighted average: (0.0*1 + 1.0*3) / 4 = 0.75."""
        scenario = self._make_scenario(
            scored=[
                # weight 1, failing → score 0.0
                {"id": "low", "type": "assertion", "check": "False", "weight": 1, "threshold": 0.5},
                # weight 3, passing → score 1.0
                {"id": "high", "type": "assertion", "check": "True", "weight": 3, "threshold": 0.5},
            ],
            pass_threshold=0.70,
        )
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Content"})

        expected_score = (0.0 * 1 + 1.0 * 3) / (1 + 3)
        assert result.weighted_score == pytest.approx(expected_score)
        assert result.passed is True  # 0.75 >= 0.70

    def test_no_scored_criteria_gates_only_passes(self, tmp_path: Path):
        """When all criteria are gates and all pass, scenario passes with score 1.0."""
        scenario = self._make_scenario(
            gates=[
                {"id": "g1", "type": "assertion", "check": "True"},
            ],
            pass_threshold=0.75,
        )
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Content"})

        assert result.gates_passed is True
        assert result.weighted_score == pytest.approx(1.0)
        assert result.passed is True

    def test_observations_collected(self, tmp_path: Path):
        """ScenarioResult.observations contains keys from scenario['observations']."""
        scenario = {
            "id": "obs-test",
            "acceptance": [
                {"id": "g1", "type": "assertion", "check": "True", "weight": 0},
            ],
            "observations": [
                {"id": "cost_tracking", "measure": "total cost in USD"},
                {"id": "execution_time", "measure": "seconds"},
            ],
            "scoring": {"pass_threshold": 0.5, "gate_mode": "all_or_nothing"},
        }
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Content"})

        assert "cost_tracking" in result.observations
        assert "execution_time" in result.observations

    def test_unknown_criterion_type_fails_gracefully(self, tmp_path: Path):
        """Unknown criterion type returns a failed GradeResult, not an exception."""
        scenario = self._make_scenario(
            scored=[
                {"id": "mystery", "type": "quantum_entanglement", "weight": 1, "threshold": 0.5},
            ],
        )
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        result = runner.run_scenario(scenario, {"article": "Content"})

        cr = result.criterion_results[0]
        assert cr.grade.passed is False
        assert "Unknown" in cr.grade.details

    def test_llm_judge_with_rubric_file(self, tmp_path: Path):
        """Run scenario where an llm_judge criterion uses rubric_file.

        rubric_file paths are relative to scenarios_dir itself (not its
        parent), so the rubric file must live inside scenarios_dir.
        """
        # Create scenarios_dir first so we can put the rubric inside it.
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()

        # Create rubric file inside scenarios_dir (the correct containment root).
        rubric_dir = suite_dir / "shared" / "rubrics"
        rubric_dir.mkdir(parents=True)
        rubric_file = rubric_dir / "test-rubric.md"
        rubric_file.write_text("Score: 0.90\nThis rubric evaluates quality.")

        scenario = {
            "id": "rubric-file-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric_file": "shared/rubrics/test-rubric.md",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.80,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.70},
        }

        mock_response_body = json.dumps({
            "content": [{"text": "Score: 0.90\nGood article."}],
        }).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        runner = ScenarioRunner(scenarios_dir=suite_dir)
        runner._llm_grader = LLMJudgeGrader(api_key="sk-fake")

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = runner.run_scenario(scenario, {"article": "An article about AI."})

        assert result.criterion_results[0].grade.score == pytest.approx(0.90)


# ===========================================================================
# 6 – Suite runner
# ===========================================================================


class TestRunSuite:
    def test_suite_satisfaction_rate_all_pass(self, tmp_path: Path):
        """All scenarios pass → satisfaction_rate = 1.0."""
        for i in range(3):
            scenario = {
                "id": f"suite-scenario-{i}",
                "acceptance": [
                    {"id": "g", "type": "assertion", "check": "True", "weight": 0}
                ],
                "scoring": {"pass_threshold": 0.0},
            }
            (tmp_path / f"scenario-{i}.yaml").write_text(yaml.dump(scenario))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(
            suite_dir=tmp_path,
            pipeline_outputs={
                "suite-scenario-0": {"article": "a"},
                "suite-scenario-1": {"article": "b"},
                "suite-scenario-2": {"article": "c"},
            },
        )

        assert suite.total_scenarios == 3
        assert suite.satisfaction_rate == pytest.approx(1.0)

    def test_suite_satisfaction_rate_partial(self, tmp_path: Path):
        """2 of 4 scenarios pass → satisfaction_rate = 0.5."""
        for i in range(4):
            check = "True" if i < 2 else "False"
            scenario = {
                "id": f"suite-mixed-{i}",
                "acceptance": [
                    {"id": "s", "type": "assertion", "check": check, "weight": 1, "threshold": 0.5}
                ],
                "scoring": {"pass_threshold": 0.5},
            }
            (tmp_path / f"scenario-{i}.yaml").write_text(yaml.dump(scenario))

        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(
            suite_dir=tmp_path,
            pipeline_outputs={f"suite-mixed-{i}": {"article": "x"} for i in range(4)},
        )

        assert suite.total_scenarios == 4
        assert suite.satisfaction_rate == pytest.approx(0.5)

    def test_suite_empty_dir(self, tmp_path: Path):
        """Empty directory → SuiteResult with 0 scenarios and satisfaction_rate=0.0."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        suite = runner.run_suite(suite_dir=tmp_path, pipeline_outputs={})

        assert suite.total_scenarios == 0
        assert suite.satisfaction_rate == pytest.approx(0.0)
        assert suite.scenarios == []


# ===========================================================================
# 7 – LLMJudgeGrader: executor routing mode  (Issue #171)
# ===========================================================================


def _make_mock_task_result(
    response_text: str = "Score: 0.85\nGood article.",
    success: bool = True,
) -> MagicMock:
    """Build a mock TaskResult whose interface matches orchestration_engine.schemas."""
    from orchestration_engine.schemas import TaskState

    mock_result = MagicMock()
    mock_result.state = TaskState.SUCCESS if success else TaskState.FAILED
    mock_result.result = {"text": response_text} if success else {}
    mock_result.errors = [] if success else [
        MagicMock(message="Executor failed for test"),
    ]
    return mock_result


def _make_mock_executor(
    response_text: str = "Score: 0.85\nGood article.",
    success: bool = True,
    side_effect=None,
) -> MagicMock:
    """Build a mock executor that returns a controlled TaskResult."""
    executor = MagicMock()
    if side_effect is not None:
        executor.execute.side_effect = side_effect
    else:
        executor.execute.return_value = _make_mock_task_result(response_text, success)
    return executor


class TestLLMJudgeGraderExecutorMode:
    """Tests for executor-routing mode (Issue #171).

    When an executor is provided to LLMJudgeGrader, the grade() method must
    route through executor.execute() instead of making raw urllib calls.
    """

    def test_executor_is_called_instead_of_urllib(self):
        """When executor is set, executor.execute() is called — not urllib."""
        executor = _make_mock_executor("Score: 0.90\nExcellent.")

        grader = LLMJudgeGrader(executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = grader.grade(
                output={"article": "Some article text."},
                rubric="Rate between 0.0 and 1.0.",
                judge_model="claude-haiku-4-5-20241022",
            )

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result.grader_type == "llm_judge"

    def test_executor_score_parsed_correctly(self):
        """Score extracted from executor response text is parsed correctly."""
        executor = _make_mock_executor("Score: 0.72\nDecent article.")

        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade(
            output={"article": "Article text."},
            rubric="Quality rubric.",
            judge_model="claude-haiku-4-5-20241022",
        )

        assert result.score == pytest.approx(0.72)
        assert result.passed is True  # 0.72 >= 0.5 baseline
        assert result.grader_type == "llm_judge"

    def test_executor_takes_priority_over_api_key(self):
        """When both executor AND api_key are set, executor path wins."""
        executor = _make_mock_executor("Score: 0.60\nOK.")

        grader = LLMJudgeGrader(api_key="sk-test-should-not-be-used", executor=executor)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = grader.grade(
                output={"article": "Article."},
                rubric="Rubric.",
                judge_model="claude-haiku-4-5-20241022",
            )

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result.score == pytest.approx(0.60)

    def test_executor_failure_state_returns_zero_score(self):
        """Non-SUCCESS state from executor → score=0.0, passed=False."""
        executor = _make_mock_executor(success=False)

        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade(
            output={"article": "Article."},
            rubric="Rubric.",
            judge_model="claude-haiku-4-5-20241022",
        )

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert result.grader_type == "llm_judge"
        assert "non-success state" in result.details.lower()

    def test_executor_raises_exception_returns_error_result(self):
        """RuntimeError from executor.execute() → score=0.0 with error details."""
        executor = _make_mock_executor(side_effect=RuntimeError("gateway unreachable"))

        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade(
            output={"article": "Article."},
            rubric="Rubric.",
            judge_model="claude-haiku-4-5-20241022",
        )

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert "RuntimeError" in result.details or "gateway unreachable" in result.details

    def test_executor_empty_response_returns_zero_score(self):
        """Empty text in executor result → score=0.0."""
        executor = _make_mock_executor(response_text="")

        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade(
            output={"article": "Article."},
            rubric="Rubric.",
            judge_model="claude-haiku-4-5-20241022",
        )

        assert result.passed is False
        assert result.score == pytest.approx(0.0)
        assert "empty" in result.details.lower()

    def test_executor_no_score_in_response_defaults_to_zero(self):
        """Executor response without 'Score: X' pattern → score=0.0."""
        executor = _make_mock_executor("This article looks pretty good to me.")

        grader = LLMJudgeGrader(executor=executor)
        result = grader.grade(
            output={"article": "Article."},
            rubric="Rubric.",
            judge_model="claude-haiku-4-5-20241022",
        )

        assert result.score == pytest.approx(0.0)
        assert "No score found" in result.details

    def test_executor_prompt_contains_article_and_rubric(self):
        """The TaskSpec payload sent to executor contains article text and rubric."""
        from orchestration_engine.schemas import TaskSpec

        captured_tasks: list[TaskSpec] = []

        def capture_execute(task: TaskSpec):
            captured_tasks.append(task)
            return _make_mock_task_result("Score: 0.80\nGood.")

        executor = MagicMock()
        executor.execute.side_effect = capture_execute

        article = "The quantum computer operates at near-absolute zero."
        rubric = "Evaluate technical accuracy. Score between 0.0 and 1.0."

        grader = LLMJudgeGrader(executor=executor)
        grader.grade(
            output={"article": article},
            rubric=rubric,
            judge_model="claude-sonnet-4-6",
        )

        assert len(captured_tasks) == 1
        prompt = captured_tasks[0].payload.get("prompt", "")
        assert article in prompt, "Article text must be in the executor prompt"
        assert rubric in prompt, "Rubric text must be in the executor prompt"

    def test_executor_holdout_metadata_not_in_prompt(self):
        """Scenario metadata must NOT appear in the prompt sent to the executor."""
        from orchestration_engine.schemas import TaskSpec

        captured_tasks: list[TaskSpec] = []

        def capture_execute(task: TaskSpec):
            captured_tasks.append(task)
            return _make_mock_task_result("Score: 0.75\nGood.")

        executor = MagicMock()
        executor.execute.side_effect = capture_execute

        article = "AI is transforming the world."
        metadata_that_should_not_leak = {
            "scenario_id": "secret-scenario-999",
            "pipeline": "ultra-secret-pipeline",
            "threshold": 0.95,
        }

        grader = LLMJudgeGrader(executor=executor)
        grader.grade(
            output={"article": article, **metadata_that_should_not_leak},
            rubric="Rate the article.",
            judge_model="claude-haiku-4-5-20241022",
        )

        prompt = captured_tasks[0].payload.get("prompt", "")
        for forbidden in ("secret-scenario-999", "ultra-secret-pipeline", "0.95"):
            assert forbidden not in prompt, (
                f"Holdout violated: '{forbidden}' found in executor prompt"
            )

    def test_executor_model_tier_mapped_from_judge_model(self):
        """judge_model string is mapped to appropriate ModelTier in TaskSpec."""
        from orchestration_engine.schemas import ModelTier, TaskSpec

        captured_tasks: list[TaskSpec] = []

        def capture_execute(task: TaskSpec):
            captured_tasks.append(task)
            return _make_mock_task_result("Score: 0.80\nGood.")

        executor = MagicMock()
        executor.execute.side_effect = capture_execute

        grader = LLMJudgeGrader(executor=executor)

        # Haiku judge model → ModelTier.HAIKU
        captured_tasks.clear()
        grader.grade(
            output={"article": "Text."},
            rubric="Rubric.",
            judge_model="claude-haiku-4-5-20241022",
        )
        assert captured_tasks[0].preferred_model == ModelTier.HAIKU

        # Sonnet judge model → ModelTier.SONNET
        captured_tasks.clear()
        grader.grade(
            output={"article": "Text."},
            rubric="Rubric.",
            judge_model="claude-sonnet-4-6",
        )
        assert captured_tasks[0].preferred_model == ModelTier.SONNET

        # Opus judge model → ModelTier.OPUS
        captured_tasks.clear()
        grader.grade(
            output={"article": "Text."},
            rubric="Rubric.",
            judge_model="claude-opus-4-6",
        )
        assert captured_tasks[0].preferred_model == ModelTier.OPUS


# ===========================================================================
# 8 – LLMJudgeGrader: dry-run mode
# ===========================================================================


class TestLLMJudgeGraderDryRunMode:
    """Tests for the ORCH_DRY_RUN=1 short-circuit path.

    Dry-run mode must take absolute priority over both executor and api-key
    paths — no external calls are made when the env var is set.
    """

    def test_dry_run_returns_default_stub_score(self):
        """ORCH_DRY_RUN=1 → score=0.8 (default stub), no API call."""
        grader = LLMJudgeGrader(api_key=None)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"article": "Some text."},
                rubric="Rate it.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.8)
        assert result.passed is True  # 0.8 >= 0.5
        assert result.grader_type == "llm_judge"
        assert "dry-run" in result.details.lower() or "ORCH_DRY_RUN" in result.details

    def test_dry_run_custom_stub_score(self):
        """dry_run_stub_score parameter controls the returned score."""
        grader = LLMJudgeGrader(api_key=None, dry_run_stub_score=0.3)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"article": "Some text."},
                rubric="Rate it.",
                judge_model="claude-haiku-4-5-20241022",
            )

        assert result.score == pytest.approx(0.3)
        assert result.passed is False  # 0.3 < 0.5

    def test_dry_run_skips_executor(self):
        """ORCH_DRY_RUN=1 → executor.execute() is never called."""
        executor = _make_mock_executor("Score: 0.90\nExcellent.")
        grader = LLMJudgeGrader(executor=executor)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = grader.grade(
                output={"article": "Some text."},
                rubric="Rate it.",
                judge_model="claude-haiku-4-5-20241022",
            )

        executor.execute.assert_not_called()
        assert result.score == pytest.approx(0.8)

    def test_dry_run_skips_urllib(self):
        """ORCH_DRY_RUN=1 → urllib.request.urlopen is never called."""
        grader = LLMJudgeGrader(api_key="sk-test-fake-key")

        with patch("urllib.request.urlopen") as mock_urlopen:
            with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
                grader.grade(
                    output={"article": "Some text."},
                    rubric="Rate it.",
                    judge_model="claude-haiku-4-5-20241022",
                )

        mock_urlopen.assert_not_called()

    def test_dry_run_stub_score_clamped_to_valid_range(self):
        """dry_run_stub_score > 1.0 is clamped to 1.0; < 0.0 clamped to 0.0."""
        grader_high = LLMJudgeGrader(dry_run_stub_score=1.5)
        grader_low = LLMJudgeGrader(dry_run_stub_score=-0.5)

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result_high = grader_high.grade({}, "rubric", "model")
            result_low = grader_low.grade({}, "rubric", "model")

        assert result_high.score == pytest.approx(1.0)
        assert result_low.score == pytest.approx(0.0)


# ===========================================================================
# 9 – ScenarioRunner: executor forwarding
# ===========================================================================


class TestScenarioRunnerExecutorForwarding:
    """Tests that ScenarioRunner accepts and forwards the executor parameter."""

    def test_runner_accepts_executor_parameter(self, tmp_path: Path):
        """ScenarioRunner(executor=...) constructs without error."""
        executor = _make_mock_executor("Score: 0.85\nGood.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)
        # Executor is forwarded to the internal LLMJudgeGrader
        assert runner._llm_grader.executor is executor

    def test_runner_without_executor_uses_none(self, tmp_path: Path):
        """ScenarioRunner() without executor → grader.executor is None (backward compat)."""
        runner = ScenarioRunner(scenarios_dir=tmp_path)
        assert runner._llm_grader.executor is None

    def test_runner_routes_llm_judge_through_executor(self, tmp_path: Path):
        """run_scenario dispatches llm_judge criteria through the executor."""
        executor = _make_mock_executor("Score: 0.88\nWell written.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "executor-routing-test",
            "acceptance": [
                {
                    "id": "quality",
                    "type": "llm_judge",
                    "rubric": "Rate the article quality. Score: [0.0-1.0]",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = runner.run_scenario(scenario, {"article": "A well-crafted article."})

        executor.execute.assert_called_once()
        mock_urlopen.assert_not_called()
        assert result.criterion_results[0].grade.score == pytest.approx(0.88)

    def test_runner_dry_run_overrides_executor(self, tmp_path: Path):
        """ORCH_DRY_RUN=1 takes priority even when executor is provided."""
        executor = _make_mock_executor("Score: 0.99\nPerfect.")
        runner = ScenarioRunner(scenarios_dir=tmp_path, executor=executor)

        scenario = {
            "id": "dry-run-override-test",
            "acceptance": [
                {
                    "id": "judge",
                    "type": "llm_judge",
                    "rubric": "Rate quality.",
                    "judge_model": "claude-haiku-4-5-20241022",
                    "threshold": 0.5,
                    "weight": 1,
                }
            ],
            "scoring": {"pass_threshold": 0.5},
        }

        with patch.dict("os.environ", {"ORCH_DRY_RUN": "1"}):
            result = runner.run_scenario(scenario, {"article": "Some text."})

        executor.execute.assert_not_called()
        # Stub score (0.8 default) used
        assert result.criterion_results[0].grade.score == pytest.approx(0.8)
