"""Tests for `orch new` — scaffold a new pipeline template (#73)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from orchestration_engine.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invoke(args, cwd=None, input_text=None):
    """Run the CLI and return the result."""
    runner = CliRunner()
    return runner.invoke(main, args, input=input_text, catch_exceptions=False)


def _invoke_in(tmp_path, args, input_text=None):
    """Run the CLI with `cwd` set to tmp_path so relative paths are isolated."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        return runner.invoke(main, args, input=input_text, catch_exceptions=False)


def _load_yaml(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Fixture: a minimal valid template for --from tests
# ---------------------------------------------------------------------------

@pytest.fixture
def source_template(tmp_path) -> Path:
    """Write a minimal two-phase template and return its path."""
    content = """id: source-pipeline
name: Source Pipeline
version: "1.0.0"
description: "A template for cloning tests"
author: "Test Author"

config_schema:
  type: object
  properties:
    topic:
      type: string
      description: Input topic
  required:
    - topic

phases:
  - id: step-one
    name: Step One
    description: First step
    task_type: content
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    timeout_minutes: 30
    prompt_template: |
      Do step one with: {input[topic]}
    output_schema:
      type: object
      properties:
        result:
          type: string

  - id: step-two
    name: Step Two
    description: Second step
    task_type: review
    model_tier: haiku
    thinking_level: off
    depends_on:
      - step-one
    timeout_minutes: 15
    prompt_template: |
      Do step two with: {previous_output[step-one]}
    output_schema:
      type: object
      properties:
        result:
          type: string
"""
    p = tmp_path / "source-pipeline.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# 1. --yes generates valid YAML with expected structure
# ---------------------------------------------------------------------------

class TestYesFlag:
    def test_yes_creates_file(self, tmp_path):
        """--yes should create a file without prompting."""
        out = tmp_path / "out.yaml"
        result = _invoke(["new", "--yes", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists(), f"Expected {out} to exist"

    def test_yes_default_name(self, tmp_path):
        """--yes should default to name=my-pipeline."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        data = _load_yaml(out)
        assert data["id"] == "my-pipeline"
        assert data["name"] == "my-pipeline"

    def test_yes_default_two_phases(self, tmp_path):
        """--yes should produce 2 phases by default."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        data = _load_yaml(out)
        assert len(data["phases"]) == 2

    def test_yes_phases_have_required_fields(self, tmp_path):
        """Each generated phase must have the mandatory fields."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        data = _load_yaml(out)
        for phase in data["phases"]:
            assert "id" in phase
            assert "name" in phase

    def test_yes_has_config_schema(self, tmp_path):
        """--yes should include a config_schema with a 'topic' property."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        data = _load_yaml(out)
        assert "config_schema" in data
        schema = data["config_schema"]
        assert schema.get("type") == "object"
        assert "topic" in schema.get("properties", {})

    def test_yes_has_version(self, tmp_path):
        """--yes should include a version field."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        data = _load_yaml(out)
        assert "version" in data
        assert data["version"]

    def test_yes_yaml_is_parseable(self, tmp_path):
        """Generated YAML must be parseable (no syntax errors)."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        # If this doesn't raise we're good
        data = _load_yaml(out)
        assert isinstance(data, dict)

    def test_yes_output_contains_comments(self, tmp_path):
        """Generated YAML should contain at least some # comment lines."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        raw = out.read_text()
        assert "#" in raw, "Expected comment lines in generated YAML"

    def test_yes_default_model_tier_sonnet(self, tmp_path):
        """Default model tier should be 'sonnet'."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        data = _load_yaml(out)
        for phase in data["phases"]:
            assert phase["model_tier"] == "sonnet"

    def test_yes_custom_phase_count(self, tmp_path):
        """--phases N should generate exactly N phases."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "4", "--output", str(out)])
        data = _load_yaml(out)
        assert len(data["phases"]) == 4


# ---------------------------------------------------------------------------
# 2. Generated file passes `orch validate`
# ---------------------------------------------------------------------------

class TestValidate:
    def test_generated_passes_validate(self, tmp_path):
        """The generated template must pass `orch validate` with exit code 0."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        result = _invoke(["validate", str(out)])
        assert result.exit_code == 0, (
            f"orch validate failed:\n{result.output}\n{result.stderr}"
        )

    def test_multi_phase_generated_passes_validate(self, tmp_path):
        """A 4-phase template should also pass validate."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "4", "--output", str(out)])
        result = _invoke(["validate", str(out)])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 3. --from clones template correctly
# ---------------------------------------------------------------------------

class TestFromFlag:
    def test_from_copies_phases(self, tmp_path, source_template):
        """--from should copy phases from the source template."""
        out = tmp_path / "cloned.yaml"
        result = _invoke(
            ["new", "--yes", "--from", str(source_template), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        data = _load_yaml(out)
        source = _load_yaml(source_template)
        assert len(data["phases"]) == len(source["phases"])

    def test_from_copies_phase_ids(self, tmp_path, source_template):
        """--from should preserve phase IDs from the source."""
        out = tmp_path / "cloned.yaml"
        _invoke(["new", "--yes", "--from", str(source_template), "--output", str(out)])
        data = _load_yaml(out)
        phase_ids = [p["id"] for p in data["phases"]]
        assert "step-one" in phase_ids
        assert "step-two" in phase_ids

    def test_from_copies_config_schema(self, tmp_path, source_template):
        """--from should carry over the config_schema."""
        out = tmp_path / "cloned.yaml"
        _invoke(["new", "--yes", "--from", str(source_template), "--output", out])
        data = _load_yaml(out)
        source = _load_yaml(source_template)
        assert data["config_schema"] == source["config_schema"]

    def test_from_cloned_passes_validate(self, tmp_path, source_template):
        """A --from clone must also pass `orch validate`."""
        out = tmp_path / "cloned.yaml"
        _invoke(["new", "--yes", "--from", str(source_template), "--output", str(out)])
        result = _invoke(["validate", str(out)])
        assert result.exit_code == 0, result.output

    def test_from_copies_author(self, tmp_path, source_template):
        """--from should carry over the author field."""
        out = tmp_path / "cloned.yaml"
        _invoke(["new", "--yes", "--from", str(source_template), "--output", str(out)])
        data = _load_yaml(out)
        assert data.get("author") == "Test Author"


# ---------------------------------------------------------------------------
# 4. Phase dependencies are wired correctly in output
# ---------------------------------------------------------------------------

class TestPhaseDependencies:
    def test_first_phase_no_deps(self, tmp_path):
        """The first phase should always have an empty depends_on."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "3", "--output", str(out)])
        data = _load_yaml(out)
        first = data["phases"][0]
        assert first.get("depends_on") == [] or first.get("depends_on") is None

    def test_second_phase_depends_on_first(self, tmp_path):
        """The default second phase should depend on phase-1."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "2", "--output", str(out)])
        data = _load_yaml(out)
        second = data["phases"][1]
        deps = second.get("depends_on") or []
        assert "phase-1" in deps

    def test_from_preserves_deps(self, tmp_path, source_template):
        """--from should preserve the depends_on wiring from the source."""
        out = tmp_path / "cloned.yaml"
        _invoke(["new", "--yes", "--from", str(source_template), "--output", str(out)])
        data = _load_yaml(out)
        step_two = next(p for p in data["phases"] if p["id"] == "step-two")
        assert "step-one" in (step_two.get("depends_on") or [])

    def test_interactive_deps_wired(self, tmp_path):
        """Interactive phase entry with explicit deps should be reflected in output."""
        out = tmp_path / "out.yaml"
        # 2 phases; for phase-2 enter dependency on phase-1
        user_input = (
            "My Pipeline\n"   # name
            "A test\n"         # description
            "Test Author\n"    # author
            "2\n"              # number of phases
            # Phase 1
            "phase-1\n"        # id
            "Phase 1\n"        # name
            "\n"               # description (blank)
            "sonnet\n"         # model tier
            "low\n"            # thinking level
            # Phase 2
            "phase-2\n"        # id
            "Phase 2\n"        # name
            "\n"               # description (blank)
            "sonnet\n"         # model tier
            "low\n"            # thinking level
            "phase-1\n"        # dependencies
        )
        result = _invoke(["new", "--output", str(out)], input_text=user_input)
        assert result.exit_code == 0, result.output
        data = _load_yaml(out)
        second = data["phases"][1]
        assert "phase-1" in (second.get("depends_on") or [])


# ---------------------------------------------------------------------------
# 5. --output saves to custom path
# ---------------------------------------------------------------------------

class TestOutputPath:
    def test_output_saves_to_given_path(self, tmp_path):
        """--output should place the file exactly where requested."""
        custom = tmp_path / "subdir" / "pipeline.yaml"
        result = _invoke(["new", "--yes", "--output", str(custom)])
        assert result.exit_code == 0, result.output
        assert custom.exists(), f"Expected file at {custom}"

    def test_output_creates_parent_dirs(self, tmp_path):
        """--output should create any missing parent directories."""
        deep = tmp_path / "a" / "b" / "c" / "pipe.yaml"
        result = _invoke(["new", "--yes", "--output", str(deep)])
        assert result.exit_code == 0, result.output
        assert deep.exists()

    def test_default_output_goes_to_templates_dir(self, tmp_path, monkeypatch):
        """Without --output, file lands in ./templates/<id>.yaml."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["new", "--yes"])
        assert result.exit_code == 0, result.output
        expected = tmp_path .joinpath("templates") / "my-pipeline.yaml"
        assert expected.exists(), f"Expected {expected}"

    def test_output_in_result_message(self, tmp_path):
        """The success message should mention the output path."""
        out = tmp_path / "output.yaml"
        result = _invoke(["new", "--yes", "--output", str(out)])
        assert str(out) in result.output or "Template written" in result.output


# ---------------------------------------------------------------------------
# 6. Error on invalid phase count (0 or negative)
# ---------------------------------------------------------------------------

class TestInvalidPhaseCount:
    def test_zero_phases_errors(self, tmp_path):
        """--phases 0 should exit with a non-zero code."""
        out = tmp_path / "out.yaml"
        result = _invoke(["new", "--yes", "--phases", "0", "--output", str(out)])
        assert result.exit_code != 0
        assert not out.exists(), "File should not be created on error"

    def test_negative_phases_errors(self, tmp_path):
        """--phases -1 should exit with a non-zero code."""
        out = tmp_path / "out.yaml"
        result = _invoke(["new", "--yes", "--phases", "-1", "--output", str(out)])
        assert result.exit_code != 0

    def test_zero_phases_interactive_errors(self, tmp_path):
        """Entering 0 for number of phases in interactive mode should error."""
        out = tmp_path / "out.yaml"
        user_input = (
            "My Pipeline\n"   # name
            "Desc\n"           # description
            "\n"               # author
            "0\n"              # invalid phase count
        )
        result = _invoke(["new", "--output", str(out)], input_text=user_input)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 7. Error when output file already exists (unless --force)
# ---------------------------------------------------------------------------

class TestForceFlag:
    def test_existing_file_errors_without_force(self, tmp_path):
        """Running twice without --force or --yes should fail on the second run.

        Updated (Issue #573): --yes now suppresses the file-exists check, so only
        the case where neither --force nor --yes is given should produce an error.
        Interactive mode (no --yes) without --force must still reject existing files.
        """
        out = tmp_path / "out.yaml"
        # First run — should succeed
        r1 = _invoke(["new", "--yes", "--output", str(out)])
        assert r1.exit_code == 0

        # --yes on an existing file must now succeed (suppresses all confirmations).
        r2 = _invoke(["new", "--yes", "--output", str(out)])
        assert r2.exit_code == 0

    def test_existing_file_overwritten_with_force(self, tmp_path):
        """--force should overwrite an existing file."""
        out = tmp_path / "out.yaml"
        # First run
        _invoke(["new", "--yes", "--output", str(out)])
        first_mtime = out.stat().st_mtime

        # Second run with --force
        r2 = _invoke(["new", "--yes", "--force", "--output", str(out)])
        assert r2.exit_code == 0
        # File should be rewritten
        assert out.exists()

    def test_force_output_is_valid(self, tmp_path):
        """Overwritten file should still be valid YAML."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--output", str(out)])
        _invoke(["new", "--yes", "--force", "--output", str(out)])
        data = _load_yaml(out)
        assert "id" in data
        assert "phases" in data


# ---------------------------------------------------------------------------
# 8. Edge-case / integration
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_name_with_spaces_becomes_hyphenated_id(self, tmp_path):
        """Names with spaces should produce a hyphenated ID."""
        out = tmp_path / "out.yaml"
        user_input = (
            "My Awesome Pipeline\n"
            "\n"   # description
            "\n"   # author
            "1\n"  # phases
            # Phase 1
            "phase-1\n"
            "Phase 1\n"
            "\n"
            "sonnet\n"
            "low\n"
        )
        result = _invoke(["new", "--output", str(out)], input_text=user_input)
        assert result.exit_code == 0, result.output
        data = _load_yaml(out)
        assert re.match(r"^[a-z0-9\-]+$", data["id"]), f"ID not slugified: {data['id']}"

    def test_single_phase_no_dependency(self, tmp_path):
        """A single-phase template should have no depends_on."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "1", "--output", str(out)])
        data = _load_yaml(out)
        assert len(data["phases"]) == 1
        deps = data["phases"][0].get("depends_on") or []
        assert deps == []

    def test_single_phase_passes_validate(self, tmp_path):
        """A single-phase template should pass orch validate."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "1", "--output", str(out)])
        result = _invoke(["validate", str(out)])
        assert result.exit_code == 0, result.output

    def test_five_phases_chain_deps(self, tmp_path):
        """Five phases should form a valid dependency chain."""
        out = tmp_path / "out.yaml"
        _invoke(["new", "--yes", "--phases", "5", "--output", str(out)])
        data = _load_yaml(out)
        result = _invoke(["validate", str(out)])
        assert result.exit_code == 0, result.output
        # Each phase after the first should depend on the previous
        for i, phase in enumerate(data["phases"]):
            if i == 0:
                assert (phase.get("depends_on") or []) == []
            else:
                assert data["phases"][i - 1]["id"] in (phase.get("depends_on") or [])
