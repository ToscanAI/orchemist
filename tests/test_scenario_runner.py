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
        """Run scenario where an llm_judge criterion uses rubric_file."""
        # Create rubric file in expected location
        rubric_dir = tmp_path / "shared" / "rubrics"
        rubric_dir.mkdir(parents=True)
        rubric_file = rubric_dir / "test-rubric.md"
        rubric_file.write_text("Score: 0.90\nThis rubric evaluates quality.")

        suite_dir = tmp_path / "suite"
        suite_dir.mkdir()

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
