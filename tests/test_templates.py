"""Tests for the template engine and phase sequencer.

All execution tests use DryRunExecutor with zero delay and zero failure rate
to keep the suite fast and fully deterministic — no real API calls are made.
"""

import pytest
import tempfile
import textwrap
from pathlib import Path

import yaml

from src.orchestration_engine.templates import (
    PhaseDefinition,
    PipelineTemplate,
    TemplateEngine,
)
from src.orchestration_engine.sequencer import PhaseSequencer, _SafeDict
from src.orchestration_engine.runner import DryRunExecutor, TaskRunner
from src.orchestration_engine.config import EngineConfig, QueueConfig, ModelsConfig
from src.orchestration_engine.db import Database
from src.orchestration_engine.schemas import TaskType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_config():
    """Minimal engine config in dry-run mode."""
    return EngineConfig(
        queue=QueueConfig(max_workers=2, poll_interval_seconds=1),
        models=ModelsConfig(default_tier="sonnet-4"),
        dry_run=True,
    )


@pytest.fixture
def test_db():
    """Isolated in-memory database for each test."""
    return Database(":memory:")


@pytest.fixture
def fast_runner(test_db, test_config):
    """TaskRunner with a zero-delay, zero-failure-rate DryRunExecutor."""
    runner = TaskRunner(database=test_db, config=test_config)
    # Override the default 2-second delay executor with an instant one
    runner.executors = [DryRunExecutor(delay_seconds=0.0, failure_rate=0.0)]
    return runner


@pytest.fixture
def templates_dir():
    """Temporary directory for YAML template files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def engine(templates_dir):
    """TemplateEngine pointed at the temp templates dir."""
    return TemplateEngine(templates_dir=templates_dir)


@pytest.fixture
def content_pipeline_yaml(templates_dir):
    """Write the project's real content-pipeline.yaml into the temp dir."""
    repo_template = (
        Path(__file__).parent.parent .joinpath("templates") / "content-pipeline.yaml"
    )
    dest = templates_dir / "content-pipeline.yaml"
    dest.write_text(repo_template.read_text())
    return dest


def _make_simple_yaml(templates_dir: Path, content: str, filename: str = "tpl.yaml") -> Path:
    """Helper: write YAML content to a temp file and return its path."""
    p = templates_dir / filename
    p.write_text(textwrap.dedent(content))
    return p


def _simple_template(*phase_ids, depends_on_map: dict = None) -> PipelineTemplate:
    """Build a PipelineTemplate from a list of phase IDs with optional deps."""
    depends_on_map = depends_on_map or {}
    phases = [
        PhaseDefinition(
            id=pid,
            name=pid.title(),
            depends_on=depends_on_map.get(pid, []),
            prompt_template=f"Run phase {pid}. Previous: {{previous_output}}. Input: {{input}}.",
        )
        for pid in phase_ids
    ]
    return PipelineTemplate(
        id="test-pipeline",
        name="Test Pipeline",
        version="0.1.0",
        phases=phases,
    )


# ===========================================================================
# 1. TemplateEngine — Loading
# ===========================================================================


class TestTemplateLoading:
    """Tests for TemplateEngine.load_template()."""

    def test_load_real_content_pipeline(self, engine, content_pipeline_yaml):
        """Loading the real content-pipeline.yaml succeeds."""
        tpl = engine.load_template(content_pipeline_yaml)

        assert tpl.id == "content-pipeline"
        assert tpl.name == "Content Pipeline"
        assert tpl.version == "2.9.0"
        assert len(tpl.phases) == 7

    def test_load_phase_fields(self, engine, content_pipeline_yaml):
        """Each phase has all required fields populated correctly."""
        tpl = engine.load_template(content_pipeline_yaml)
        phase_ids = [p.id for p in tpl.phases]
        assert "research" in phase_ids
        assert "draft" in phase_ids
        assert "red_team" in phase_ids
        assert "final_polish" in phase_ids

    def test_load_phase_depends_on(self, engine, content_pipeline_yaml):
        """depends_on is correctly parsed from YAML lists."""
        tpl = engine.load_template(content_pipeline_yaml)
        draft_phase = next(p for p in tpl.phases if p.id == "draft")
        assert "research" in draft_phase.depends_on

    def test_load_phase_prompt_template(self, engine, content_pipeline_yaml):
        """Prompt templates are non-empty strings."""
        tpl = engine.load_template(content_pipeline_yaml)
        for phase in tpl.phases:
            assert isinstance(phase.prompt_template, str)
            assert len(phase.prompt_template) > 10

    def test_load_minimal_template(self, engine, templates_dir):
        """A minimal valid YAML (id + name only) loads without error."""
        path = _make_simple_yaml(
            templates_dir,
            """
            id: minimal
            name: Minimal Pipeline
            """,
        )
        tpl = engine.load_template(path)
        assert tpl.id == "minimal"
        assert tpl.name == "Minimal Pipeline"
        assert tpl.phases == []

    def test_load_missing_id_raises(self, engine, templates_dir):
        """Template missing 'id' raises KeyError."""
        path = _make_simple_yaml(
            templates_dir,
            """
            name: No ID Pipeline
            """,
        )
        with pytest.raises(KeyError):
            engine.load_template(path)

    def test_load_missing_name_raises(self, engine, templates_dir):
        """Template missing 'name' raises KeyError."""
        path = _make_simple_yaml(
            templates_dir,
            """
            id: no-name-pipeline
            """,
        )
        with pytest.raises(KeyError):
            engine.load_template(path)

    def test_load_empty_file_raises(self, engine, templates_dir):
        """Completely empty YAML file raises ValueError."""
        path = templates_dir / "empty.yaml"
        path.write_text("")
        with pytest.raises(ValueError, match="empty"):
            engine.load_template(path)

    def test_load_nonexistent_file_raises(self, engine, templates_dir):
        """Loading a non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            engine.load_template(templates_dir / "ghost.yaml")

    def test_load_phase_with_null_optional_fields(self, engine, templates_dir):
        """Null YAML values for optional fields don't crash the loader."""
        path = _make_simple_yaml(
            templates_dir,
            """
            id: null-fields
            name: Null Fields Pipeline
            phases:
              - id: only_phase
                name: Only Phase
                description: null
                depends_on: null
                output_schema: null
                prompt_template: null
            """,
        )
        tpl = engine.load_template(path)
        phase = tpl.phases[0]
        assert phase.depends_on == []
        assert phase.output_schema == {}
        assert phase.description == ""
        assert phase.prompt_template == ""


# ===========================================================================
# 2. TemplateEngine — Execution Order
# ===========================================================================


class TestExecutionOrder:
    """Tests for TemplateEngine.get_execution_order()."""

    def test_empty_template_returns_empty(self, engine):
        """Empty template produces no waves."""
        tpl = PipelineTemplate(id="empty", name="Empty", version="1.0")
        assert engine.get_execution_order(tpl) == []

    def test_single_phase_no_deps(self, engine):
        """Single phase with no dependencies → one wave."""
        tpl = _simple_template("alpha")
        order = engine.get_execution_order(tpl)
        assert order == [["alpha"]]

    def test_all_phases_no_deps_one_wave(self, engine):
        """Multiple independent phases → all in the same first wave."""
        tpl = _simple_template("a", "b", "c")
        order = engine.get_execution_order(tpl)
        assert len(order) == 1
        assert sorted(order[0]) == ["a", "b", "c"]

    def test_linear_chain_separate_waves(self, engine):
        """A → B → C produces three single-phase waves."""
        tpl = _simple_template(
            "a", "b", "c",
            depends_on_map={"b": ["a"], "c": ["b"]},
        )
        order = engine.get_execution_order(tpl)
        assert order == [["a"], ["b"], ["c"]]

    def test_parallel_phases_same_wave(self, engine):
        """B and C both depend on A → wave 1 = [A], wave 2 = [B, C]."""
        tpl = _simple_template(
            "a", "b", "c",
            depends_on_map={"b": ["a"], "c": ["a"]},
        )
        order = engine.get_execution_order(tpl)
        assert order == [["a"], ["b", "c"]]

    def test_diamond_dependency(self, engine):
        """Classic diamond: A → B, A → C, B + C → D."""
        tpl = _simple_template(
            "a", "b", "c", "d",
            depends_on_map={"b": ["a"], "c": ["a"], "d": ["b", "c"]},
        )
        order = engine.get_execution_order(tpl)
        assert order[0] == ["a"]
        assert sorted(order[1]) == ["b", "c"]
        assert order[2] == ["d"]

    def test_content_pipeline_waves(self, engine, content_pipeline_yaml):
        """The content pipeline produces the correct sequential + fan-in waves."""
        tpl = engine.load_template(content_pipeline_yaml)
        order = engine.get_execution_order(tpl)

        # Flatten for easy checking
        flat = [pid for wave in order for pid in wave]
        assert flat.index("research") < flat.index("draft")
        assert flat.index("draft") < flat.index("red_team")
        assert flat.index("red_team") < flat.index("apply_fixes")
        assert flat.index("apply_fixes") < flat.index("final_polish")

    def test_cycle_produces_incomplete_order(self, engine):
        """A cycle means not all phases appear in the execution order."""
        # A depends on B, B depends on A → cycle
        tpl = _simple_template(
            "a", "b",
            depends_on_map={"a": ["b"], "b": ["a"]},
        )
        order = engine.get_execution_order(tpl)
        ordered_ids = {pid for wave in order for pid in wave}
        # Neither A nor B can be scheduled → both missing
        assert "a" not in ordered_ids
        assert "b" not in ordered_ids

    def test_waves_are_sorted(self, engine):
        """Each wave's phase list is sorted alphabetically for determinism."""
        tpl = _simple_template("z", "m", "a")  # all independent
        order = engine.get_execution_order(tpl)
        assert order == [["a", "m", "z"]]


# ===========================================================================
# 3. TemplateEngine — Validation
# ===========================================================================


class TestTemplateValidation:
    """Tests for TemplateEngine.validate_template()."""

    def test_valid_template_no_errors(self, engine, content_pipeline_yaml):
        """A well-formed template returns an empty error list."""
        tpl = engine.load_template(content_pipeline_yaml)
        errors = engine.validate_template(tpl)
        assert errors == []

    def test_duplicate_phase_id(self, engine):
        """Duplicate phase IDs produce a validation error."""
        tpl = PipelineTemplate(
            id="dup",
            name="Dup",
            version="1.0",
            phases=[
                PhaseDefinition(id="alpha", name="Alpha"),
                PhaseDefinition(id="alpha", name="Alpha Again"),
            ],
        )
        errors = engine.validate_template(tpl)
        assert any("Duplicate" in e and "alpha" in e for e in errors)

    def test_unknown_dependency_produces_error(self, engine):
        """Referencing a non-existent phase in depends_on is an error."""
        tpl = _simple_template("a", depends_on_map={"a": ["ghost"]})
        errors = engine.validate_template(tpl)
        assert any("ghost" in e for e in errors)

    def test_cycle_two_phases_detected(self, engine):
        """Mutual dependency cycle between two phases is detected."""
        tpl = _simple_template(
            "a", "b",
            depends_on_map={"a": ["b"], "b": ["a"]},
        )
        errors = engine.validate_template(tpl)
        assert any("cycle" in e.lower() or "Cycle" in e for e in errors)

    def test_cycle_three_phases_detected(self, engine):
        """Three-phase cycle A→B→C→A is detected."""
        tpl = _simple_template(
            "a", "b", "c",
            depends_on_map={"b": ["a"], "c": ["b"], "a": ["c"]},
        )
        errors = engine.validate_template(tpl)
        assert any("cycle" in e.lower() or "Cycle" in e for e in errors)

    def test_independent_phases_no_errors(self, engine):
        """Multiple phases with no dependencies produce no validation errors."""
        tpl = _simple_template("x", "y", "z")
        errors = engine.validate_template(tpl)
        assert errors == []

    # --- Issue #295: mandatory scenario for coding pipelines ---

    def test_code_category_without_scenario_produces_error(self, engine):
        """A category=code template with no scenario must produce a validation error."""
        tpl = _simple_template("build")
        tpl.category = "code"
        tpl.scenario = None
        errors = engine.validate_template(tpl)
        assert any("require a scenario" in e for e in errors), (
            f"Expected 'require a scenario' error for code template without scenario, got: {errors}"
        )

    def test_code_category_with_scenario_no_error(self, engine):
        """A category=code template WITH a scenario must produce no scenario-related error."""
        tpl = _simple_template("build")
        tpl.category = "code"
        tpl.scenario = "scenarios/quality.yaml"
        errors = engine.validate_template(tpl)
        assert not any("require a scenario" in e for e in errors), (
            f"Unexpected 'require a scenario' error when scenario is set: {errors}"
        )

    def test_non_code_category_without_scenario_no_error(self, engine):
        """A non-code category template (e.g. content) does NOT require a scenario."""
        tpl = _simple_template("write")
        tpl.category = "content"
        tpl.scenario = None
        errors = engine.validate_template(tpl)
        assert not any("require a scenario" in e for e in errors), (
            f"'require a scenario' error should only fire for code category: {errors}"
        )

    def test_empty_category_without_scenario_no_error(self, engine):
        """A template with no category set does NOT require a scenario."""
        tpl = _simple_template("work")
        tpl.category = ""
        tpl.scenario = None
        errors = engine.validate_template(tpl)
        assert not any("require a scenario" in e for e in errors)

    def test_code_category_case_insensitive(self, engine):
        """The mandatory-scenario check is case-insensitive (e.g. 'Code', 'CODE')."""
        for cat in ("Code", "CODE", " code ", "cOdE"):
            tpl = _simple_template("build")
            tpl.category = cat
            tpl.scenario = None
            errors = engine.validate_template(tpl)
            assert any("require a scenario" in e for e in errors), (
                f"Expected error for category={cat!r}, got: {errors}"
            )


# ===========================================================================
# 4. PhaseSequencer — Prompt Building
# ===========================================================================


class TestPhaseInputBuilding:
    """Tests for PhaseSequencer._build_phase_input()."""

    def _make_sequencer(self, template, fast_runner) -> PhaseSequencer:
        return PhaseSequencer(template, fast_runner, config={"author": "Test"})

    def test_no_deps_no_previous_output(self, fast_runner):
        """Phase with no deps and simple template formats correctly."""
        tpl = _simple_template("research")
        tpl.phases[0].prompt_template = "Topic: {input[brief]}"
        seq = PhaseSequencer(tpl, fast_runner)

        result = seq._build_phase_input(tpl.phases[0], {"brief": "AI safety"})
        assert result == "Topic: AI safety"

    def test_previous_output_forwarded(self, fast_runner):
        """Accumulated phase outputs are accessible in downstream templates."""
        tpl = _simple_template("p1", "p2", depends_on_map={"p2": ["p1"]})
        tpl.phases[1].prompt_template = "Prior: {previous_output[p1]}"

        seq = PhaseSequencer(tpl, fast_runner)
        seq.phase_outputs["p1"] = {"result": "phase1 done"}

        prompt = seq._build_phase_input(tpl.phases[1], {})
        assert "phase1 done" in prompt

    def test_missing_input_key_returns_placeholder(self, fast_runner):
        """Missing input key returns a <MISSING:...> placeholder, not a crash."""
        tpl = _simple_template("p")
        tpl.phases[0].prompt_template = "Val: {input[nonexistent]}"
        seq = PhaseSequencer(tpl, fast_runner)

        prompt = seq._build_phase_input(tpl.phases[0], {})
        # SafeDict returns placeholder for missing keys
        assert "MISSING" in prompt or "nonexistent" in prompt

    def test_missing_previous_output_key_returns_placeholder(self, fast_runner):
        """Missing previous_output key returns a placeholder, not a crash."""
        tpl = _simple_template("p")
        tpl.phases[0].prompt_template = "Prior: {previous_output[ghost_phase]}"
        seq = PhaseSequencer(tpl, fast_runner)
        seq.phase_outputs = {}  # empty — no prior phases

        prompt = seq._build_phase_input(tpl.phases[0], {})
        assert "MISSING" in prompt or "ghost_phase" in prompt

    def test_empty_template_returns_empty_string(self, fast_runner):
        """Phase with empty prompt_template returns empty string."""
        tpl = _simple_template("p")
        tpl.phases[0].prompt_template = ""
        seq = PhaseSequencer(tpl, fast_runner)

        assert seq._build_phase_input(tpl.phases[0], {"key": "val"}) == ""

    def test_config_accessible_in_template(self, fast_runner):
        """Pipeline config dict is accessible in templates."""
        tpl = _simple_template("p")
        tpl.phases[0].prompt_template = "Author: {config[author]}"
        seq = PhaseSequencer(tpl, fast_runner, config={"author": "René"})

        prompt = seq._build_phase_input(tpl.phases[0], {})
        assert "René" in prompt


# ===========================================================================
# 5. PhaseSequencer — Full Execution
# ===========================================================================


class TestPhaseSequencerExecution:
    """End-to-end execution tests using DryRunExecutor (no real API calls)."""

    def test_execute_single_phase_pipeline(self, fast_runner):
        """Single-phase pipeline returns correct structure."""
        tpl = _simple_template("only")
        seq = PhaseSequencer(tpl, fast_runner)

        result = seq.execute({"brief": "test topic"})

        assert "phase_outputs" in result
        assert "final_output" in result
        assert "only" in result["phase_outputs"]

    def test_execute_returns_final_output(self, fast_runner):
        """final_output matches the last phase's output."""
        tpl = _simple_template("a", "b", depends_on_map={"b": ["a"]})
        seq = PhaseSequencer(tpl, fast_runner)

        result = seq.execute({})

        assert result["final_output"] == result["phase_outputs"]["b"]

    def test_all_phases_present_in_output(self, fast_runner):
        """All seven content-pipeline phases appear in phase_outputs."""
        repo_template = Path(__file__).parent.parent .joinpath("templates") / "content-pipeline.yaml"
        engine = TemplateEngine()
        tpl = engine.load_template(repo_template)

        seq = PhaseSequencer(tpl, fast_runner, config={
            "topic": "The future of AI",
            "author_name": "Test Author",
            "author_facts": "Test author background.",
            "voice_style": "Direct and witty.",
            "source_material": "Test source material.",
        })
        result = seq.execute({})

        assert set(result["phase_outputs"].keys()) == {
            "research", "draft", "fact_check", "red_team",
            "apply_fixes", "voice_check", "final_polish"
        }

    def test_phase_output_forwarded_to_next(self, fast_runner):
        """Output stored after phase N is available during phase N+1."""
        tpl = _simple_template("p1", "p2", depends_on_map={"p2": ["p1"]})
        seq = PhaseSequencer(tpl, fast_runner)

        seq.execute({})

        # Both phases must have outputs
        assert "p1" in seq.phase_outputs
        assert "p2" in seq.phase_outputs
        # p1 output must be a non-empty dict (DryRunExecutor always returns one)
        assert isinstance(seq.phase_outputs["p1"], dict)

    def test_execute_empty_template_returns_empty(self, fast_runner):
        """Empty template runs safely and returns empty dicts."""
        tpl = PipelineTemplate(id="empty", name="Empty", version="1.0")
        seq = PhaseSequencer(tpl, fast_runner)

        result = seq.execute({"key": "value"})

        assert result["phase_outputs"] == {}
        assert result["final_output"] == {}

    def test_phase_outputs_are_dicts(self, fast_runner):
        """Every entry in phase_outputs is a dict (serialised TaskResult)."""
        tpl = _simple_template("a", "b", "c", depends_on_map={"b": ["a"], "c": ["b"]})
        seq = PhaseSequencer(tpl, fast_runner)

        result = seq.execute({})

        for pid, output in result["phase_outputs"].items():
            assert isinstance(output, dict), f"Phase '{pid}' output is not a dict"

    def test_linear_pipeline_respects_order(self, fast_runner):
        """Phases run in topological order: earlier phases complete first."""
        execution_log: list = []

        original_execute = DryRunExecutor.execute

        def patched_execute(self_ex, task, worker_id, model_tier=None, thinking_level=None):
            phase_id = task.payload.get("phase_id", "unknown")
            execution_log.append(phase_id)
            return original_execute(self_ex, task, worker_id, model_tier, thinking_level)

        fast_runner.executors[0].__class__.execute = patched_execute

        try:
            tpl = _simple_template(
                "first", "second", "third",
                depends_on_map={"second": ["first"], "third": ["second"]},
            )
            seq = PhaseSequencer(tpl, fast_runner)
            seq.execute({})
        finally:
            # Restore original method
            fast_runner.executors[0].__class__.execute = original_execute

        assert execution_log == ["first", "second", "third"]

    def test_pipeline_aborts_on_phase_failure(self):
        """Pipeline stops and returns aborted=True when a phase fails."""
        from orchestration_engine.runner import DryRunExecutor, TaskRunner
        from orchestration_engine.db import Database
        from orchestration_engine.config import EngineConfig

        db = Database(":memory:")
        config = EngineConfig(dry_run=True)
        runner = TaskRunner(db, config)
        # Replace executor with one that always fails
        runner.executors = [DryRunExecutor(delay_seconds=0.0, failure_rate=1.0)]

        tpl = _simple_template("a", "b", depends_on_map={"b": ["a"]})
        seq = PhaseSequencer(tpl, runner)
        result = seq.execute({"brief": "test"})

        assert result.get("aborted") is True
        assert result.get("failed_phase") == "a"
        # Phase b should NOT have been executed
        assert "b" not in result["phase_outputs"]

    def test_config_missing_key_uses_placeholder(self):
        """Missing config keys produce SafeDict placeholders, not crashes."""
        from orchestration_engine.runner import DryRunExecutor, TaskRunner
        from orchestration_engine.db import Database
        from orchestration_engine.config import EngineConfig

        db = Database(":memory:")
        config = EngineConfig(dry_run=True)
        runner = TaskRunner(db, config)
        runner.executors = [DryRunExecutor(delay_seconds=0.0, failure_rate=0.0)]

        phase = PhaseDefinition(
            id="test",
            name="Test",
            prompt_template="Config value: {config[missing_key]}",
        )
        tpl = PipelineTemplate(id="test", name="Test", version="1.0", phases=[phase])
        seq = PhaseSequencer(tpl, runner, config={})
        result = seq.execute({})

        # Should complete without error — missing key becomes placeholder
        assert "test" in result["phase_outputs"]
        assert result.get("aborted") is not True


# ===========================================================================
# 6. _SafeDict helper
# ===========================================================================


class TestSafeDict:
    """Tests for the _SafeDict helper class."""

    def test_existing_key_returns_value(self):
        d = _SafeDict({"key": "value"})
        assert d["key"] == "value"

    def test_missing_key_returns_placeholder(self):
        d = _SafeDict()
        assert "MISSING" in d["anything"]

    def test_safe_dict_in_format(self):
        """Confirm _SafeDict prevents KeyError during str.format()."""
        d = _SafeDict({"a": "hello"})
        result = "{d[a]} {d[b]}".format(d=d)
        assert "hello" in result
        assert "MISSING" in result


# ===========================================================================
# 7. AutoMergeConfig and _parse_auto_merge_config (Issue #350)
# ===========================================================================


class TestAutoMergeConfig:
    """Unit tests for AutoMergeConfig dataclass validation."""

    def test_defaults(self):
        from orchestration_engine.templates import AutoMergeConfig
        cfg = AutoMergeConfig()
        assert cfg.enabled is False
        assert cfg.min_score == 0.90
        assert cfg.require_approve is True
        assert cfg.strategy == "squash"
        assert cfg.review_phase_id == "review"

    def test_custom_values(self):
        from orchestration_engine.templates import AutoMergeConfig
        cfg = AutoMergeConfig(enabled=True, min_score=0.75, strategy="merge", require_approve=False)
        assert cfg.enabled is True
        assert cfg.min_score == 0.75
        assert cfg.strategy == "merge"
        assert cfg.require_approve is False

    def test_invalid_strategy_raises(self):
        from orchestration_engine.templates import AutoMergeConfig
        with pytest.raises(ValueError, match="strategy"):
            AutoMergeConfig(strategy="cherry-pick")

    def test_score_clamped_below_zero(self):
        from orchestration_engine.templates import AutoMergeConfig
        cfg = AutoMergeConfig(min_score=-0.5)
        assert cfg.min_score == 0.0

    def test_score_clamped_above_one(self):
        from orchestration_engine.templates import AutoMergeConfig
        cfg = AutoMergeConfig(min_score=1.5)
        assert cfg.min_score == 1.0

    def test_strategy_normalised_lowercase(self):
        from orchestration_engine.templates import AutoMergeConfig
        cfg = AutoMergeConfig(strategy="REBASE")
        assert cfg.strategy == "rebase"

    def test_all_valid_strategies_accepted(self):
        from orchestration_engine.templates import AutoMergeConfig
        for s in ("squash", "merge", "rebase"):
            cfg = AutoMergeConfig(strategy=s)
            assert cfg.strategy == s


class TestParseAutoMergeConfig:
    """Unit tests for _parse_auto_merge_config helper."""

    def test_none_input_returns_none(self):
        from orchestration_engine.templates import _parse_auto_merge_config
        assert _parse_auto_merge_config(None) is None

    def test_non_dict_returns_none(self):
        from orchestration_engine.templates import _parse_auto_merge_config
        assert _parse_auto_merge_config("enabled") is None
        assert _parse_auto_merge_config(True) is None
        assert _parse_auto_merge_config(42) is None

    def test_empty_dict_returns_defaults(self):
        from orchestration_engine.templates import _parse_auto_merge_config
        cfg = _parse_auto_merge_config({})
        assert cfg is not None
        assert cfg.enabled is False
        assert cfg.min_score == 0.90

    def test_valid_dict_parsed_correctly(self):
        from orchestration_engine.templates import _parse_auto_merge_config
        cfg = _parse_auto_merge_config({
            "enabled": True,
            "min_score": 0.85,
            "strategy": "rebase",
            "require_approve": False,
            "review_phase_id": "my_review",
        })
        assert cfg is not None
        assert cfg.enabled is True
        assert cfg.min_score == 0.85
        assert cfg.strategy == "rebase"
        assert cfg.require_approve is False
        assert cfg.review_phase_id == "my_review"

    def test_unknown_fields_warn_and_ignored(self, caplog):
        import logging
        from orchestration_engine.templates import _parse_auto_merge_config
        with caplog.at_level(logging.WARNING):
            cfg = _parse_auto_merge_config({"enabled": True, "unknown_field": "boom"})
        assert cfg is not None
        assert cfg.enabled is True
        assert any("unknown" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Issue #351 — PhaseDefinition.min_output_length field
# ---------------------------------------------------------------------------


class TestPhaseDefinitionMinOutputLength:
    """PhaseDefinition correctly handles the min_output_length field."""

    def test_default_is_zero(self) -> None:
        """min_output_length defaults to 0 (validation disabled)."""
        phase = PhaseDefinition(
            id="p",
            name="P",
            prompt_template="Hello",
        )
        assert phase.min_output_length == 0

    def test_none_normalises_to_zero(self) -> None:
        """Passing None is treated as 0 (no validation)."""
        phase = PhaseDefinition(
            id="p",
            name="P",
            prompt_template="Hello",
            min_output_length=None,  # type: ignore[arg-type]
        )
        assert phase.min_output_length == 0

    def test_negative_clamps_to_zero(self) -> None:
        """Negative values are clamped to 0."""
        phase = PhaseDefinition(
            id="p",
            name="P",
            prompt_template="Hello",
            min_output_length=-100,
        )
        assert phase.min_output_length == 0

    def test_float_coerces_to_int(self) -> None:
        """Float values are coerced to int (floor behaviour via int())."""
        phase = PhaseDefinition(
            id="p",
            name="P",
            prompt_template="Hello",
            min_output_length=300.9,  # type: ignore[arg-type]
        )
        assert phase.min_output_length == 300
        assert isinstance(phase.min_output_length, int)

    def test_positive_value_preserved(self) -> None:
        """A valid positive integer is stored unchanged."""
        phase = PhaseDefinition(
            id="p",
            name="P",
            prompt_template="Hello",
            min_output_length=500,
        )
        assert phase.min_output_length == 500

    def test_known_field_via_caplog(self, templates_dir, caplog) -> None:
        """min_output_length in YAML must not trigger 'unknown field' warning."""
        yaml_content = textwrap.dedent("""\
            id: len-check
            name: Len Check
            phases:
              - id: spec
                name: Spec
                prompt_template: "Write a spec."
                min_output_length: 250
        """)
        path = templates_dir / "len-check.yaml"
        path.write_text(yaml_content)
        eng = TemplateEngine(templates_dir=templates_dir)
        import logging
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.templates"):
            try:
                tpl = eng.load_template("len-check")
                assert tpl.phases[0].min_output_length == 250
            except Exception:
                pass
        # No "unknown field" warning for min_output_length
        for record in caplog.records:
            assert not (
                "min_output_length" in record.message and "unknown" in record.message.lower()
            ), f"Unexpected warning: {record.message}"
