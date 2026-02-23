"""Tests for Enhanced orch validate linting (Issue #74).

Covers:
- YAML syntax error reporting (line/column)
- Variable reference to nonexistent phase (warning)
- Unknown model tier with suggestion
- Unknown thinking level
- Invalid config_schema structure
- --fix corrects model tier casing
- --fix adds missing version
- Valid template passes all checks (exit 0)
- Invalid template fails (exit 1)
- Colored output contains ✓ and ✗
"""

import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from src.orchestration_engine.cli import main, _check_yaml_syntax, _apply_fixes
from src.orchestration_engine.templates import TemplateEngine, PipelineTemplate, PhaseDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, content: str) -> Path:
    """Write *content* to a temp YAML file and return its path."""
    p = tmp_path / "tpl.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def _minimal(
    *,
    extra_top: str = "",
    extra_phases: str = "",
    model_tier: str = "sonnet",
    thinking_level: str = "low",
    prompt_template: str = "Hello {input}",
) -> str:
    """Return a minimal valid template YAML string."""
    return f"""\
id: test-tpl
name: Test Template
version: "1.0.0"
description: "A test template"
{extra_top}
phases:
  - id: phase_a
    name: Phase A
    model_tier: {model_tier}
    thinking_level: {thinking_level}
    depends_on: []
    prompt_template: "{prompt_template}"
{extra_phases}
"""


# ---------------------------------------------------------------------------
# 1. YAML syntax error reporting
# ---------------------------------------------------------------------------

class TestYamlSyntaxCheck:
    def test_valid_yaml_returns_none(self, tmp_path):
        p = _write(tmp_path, _minimal())
        assert _check_yaml_syntax(p) is None

    def test_bad_yaml_returns_error_string(self, tmp_path):
        p = _write(tmp_path, "key: [\nbad yaml\n")
        result = _check_yaml_syntax(p)
        assert result is not None
        assert "YAML syntax error" in result

    def test_bad_yaml_includes_line_number(self, tmp_path):
        # Deliberately invalid YAML (unmatched bracket on line 1)
        p = _write(tmp_path, "key: [\nbad\n")
        result = _check_yaml_syntax(p)
        # Should contain line:col info
        assert result is not None
        # Line number should be present as digit in result
        import re
        assert re.search(r"\d+:\d+", result), f"Expected line:col in: {result!r}"

    def test_cli_reports_yaml_error_and_exits_1(self, tmp_path):
        p = _write(tmp_path, "key: [\nbad yaml\n")
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 1
        assert "YAML syntax error" in (result.output + (result.output or ""))


# ---------------------------------------------------------------------------
# 2. Variable reference checking
# ---------------------------------------------------------------------------

class TestVariableReferenceCheck:
    def test_valid_reference_no_warning(self, tmp_path):
        """Referencing an existing phase generates no warning."""
        content = _minimal(
            extra_phases=(
                "  - id: phase_b\n"
                "    name: Phase B\n"
                "    model_tier: sonnet\n"
                "    thinking_level: low\n"
                "    depends_on: [phase_a]\n"
                '    prompt_template: "Use {phase_a.output}"\n'
            )
        )
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        ref_warnings = [w for w in warnings if "phase_a" in w]
        assert ref_warnings == [], f"Unexpected warnings: {ref_warnings}"

    def test_unknown_phase_reference_generates_warning(self, tmp_path):
        """Referencing a nonexistent phase ID emits a warning."""
        content = _minimal(prompt_template="{nonexistent_phase.output}")
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("nonexistent_phase" in w for w in warnings), f"Warnings: {warnings}"

    def test_builtin_refs_not_flagged(self, tmp_path):
        """Built-in {input} and {previous_output} must NOT generate warnings."""
        content = _minimal(prompt_template="{input} and {previous_output}")
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        # No warnings about input or previous_output
        var_warnings = [w for w in warnings if "input" in w or "previous_output" in w]
        assert var_warnings == [], f"Unexpected warnings: {var_warnings}"


# ---------------------------------------------------------------------------
# 3. Model tier validation
# ---------------------------------------------------------------------------

class TestModelTierValidation:
    @pytest.mark.parametrize("tier", ["haiku", "sonnet", "opus"])
    def test_known_tiers_no_warning(self, tmp_path, tier):
        p = _write(tmp_path, _minimal(model_tier=tier))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        tier_warnings = [w for w in warnings if "model_tier" in w]
        assert tier_warnings == []

    def test_unknown_tier_warning(self, tmp_path):
        p = _write(tmp_path, _minimal(model_tier='"gpt4"'))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("model_tier" in w for w in warnings), f"Warnings: {warnings}"

    def test_close_match_suggestion(self, tmp_path):
        """'sonnett' is close to 'sonnet' — should get a 'did you mean?' hint."""
        p = _write(tmp_path, _minimal(model_tier="sonnett"))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("sonnet" in w for w in warnings), f"Warnings: {warnings}"

    def test_uppercase_tier_warning(self, tmp_path):
        """'Sonnet' (capitalized) should warn since model_tier is case-sensitive."""
        p = _write(tmp_path, _minimal(model_tier="Sonnet"))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("Sonnet" in w for w in warnings)


# ---------------------------------------------------------------------------
# 4. Thinking level validation
# ---------------------------------------------------------------------------

class TestThinkingLevelValidation:
    @pytest.mark.parametrize("level", ["off", "low", "medium", "high"])
    def test_known_levels_no_warning(self, tmp_path, level):
        p = _write(tmp_path, _minimal(thinking_level=level))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        level_warnings = [w for w in warnings if "thinking_level" in w]
        assert level_warnings == []

    def test_unknown_thinking_level_warning(self, tmp_path):
        p = _write(tmp_path, _minimal(thinking_level="ultra"))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("thinking_level" in w for w in warnings), f"Warnings: {warnings}"

    def test_thinking_level_suggestion(self, tmp_path):
        """'hig' is close to 'high'."""
        p = _write(tmp_path, _minimal(thinking_level="hig"))
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("high" in w for w in warnings), f"Warnings: {warnings}"


# ---------------------------------------------------------------------------
# 5. config_schema validation
# ---------------------------------------------------------------------------

class TestConfigSchemaValidation:
    def test_valid_schema_no_error(self, tmp_path):
        content = _minimal(extra_top=textwrap.dedent("""\
config_schema:
  type: object
  properties:
    brief:
      type: string
"""))
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, warnings = engine.validate_template_extended(template, raw_data)
        schema_errs = [e for e in errors if "config_schema" in e]
        assert schema_errs == []

    def test_schema_missing_type_is_error(self, tmp_path):
        content = _minimal(extra_top=textwrap.dedent("""\
config_schema:
  properties:
    brief:
      type: string
"""))
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, _ = engine.validate_template_extended(template, raw_data)
        assert any("config_schema" in e and "type" in e for e in errors), f"Errors: {errors}"

    def test_object_schema_without_properties_is_warning(self, tmp_path):
        content = _minimal(extra_top="config_schema:\n  type: object\n")
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)
        assert any("config_schema" in w for w in warnings), f"Warnings: {warnings}"

    def test_no_config_schema_no_error(self, tmp_path):
        p = _write(tmp_path, _minimal())
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, _ = engine.validate_template_extended(template, raw_data)
        assert errors == []


# ---------------------------------------------------------------------------
# 6. --fix flag
# ---------------------------------------------------------------------------

class TestFixFlag:
    def test_fix_adds_missing_version(self, tmp_path):
        content = """\
id: test-tpl
name: Test Template
description: ""
phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello {input}"
"""
        p = _write(tmp_path, content)
        raw = yaml.safe_load(p.read_text())
        assert "version" not in raw
        _apply_fixes(p, raw)
        updated = yaml.safe_load(p.read_text())
        assert updated["version"] == "1.0.0"

    def test_fix_adds_missing_description(self, tmp_path):
        content = """\
id: test-tpl
name: Test Template
version: "1.0.0"
phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        raw = yaml.safe_load(p.read_text())
        assert "description" not in raw
        _apply_fixes(p, raw)
        updated = yaml.safe_load(p.read_text())
        assert updated.get("description") == ""

    def test_fix_normalizes_model_tier_casing(self, tmp_path):
        content = """\
id: test-tpl
name: Test Template
version: "1.0.0"
description: ""
phases:
  - id: phase_a
    name: Phase A
    model_tier: Sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        raw = yaml.safe_load(p.read_text())
        _apply_fixes(p, raw)
        updated = yaml.safe_load(p.read_text())
        assert updated["phases"][0]["model_tier"] == "sonnet"

    def test_fix_via_cli_flag(self, tmp_path):
        """--fix via CLI also normalizes and rewrites the file."""
        content = """\
id: test-tpl
name: Test Template
description: ""
phases:
  - id: phase_a
    name: Phase A
    model_tier: Sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", "--fix", str(p)])
        # Should mention fix was applied
        assert "--fix applied" in result.output
        # File should be updated
        updated = yaml.safe_load(p.read_text())
        assert updated["version"] == "1.0.0"
        assert updated["phases"][0]["model_tier"] == "sonnet"


# ---------------------------------------------------------------------------
# 7. Exit codes
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_valid_template_exits_0(self, tmp_path):
        p = _write(tmp_path, _minimal())
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 0

    def test_structural_error_exits_1(self, tmp_path):
        """A template with a duplicate phase ID should exit 1."""
        content = """\
id: test-tpl
name: Test Template
version: "1.0.0"
description: ""
phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
  - id: phase_a
    name: Phase A duplicate
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 1

    def test_warnings_alone_exit_0(self, tmp_path):
        """Warnings (unknown model tier) should not cause exit 1."""
        p = _write(tmp_path, _minimal(model_tier='"gpt-4"'))
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 0

    def test_yaml_syntax_error_exits_1(self, tmp_path):
        p = _write(tmp_path, "key: [\nbad\n")
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 1

    def test_config_schema_error_exits_1(self, tmp_path):
        content = _minimal(extra_top="config_schema:\n  properties:\n    brief:\n      type: string\n")
        p = _write(tmp_path, content)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 8. Colored output
# ---------------------------------------------------------------------------

class TestColoredOutput:
    def test_valid_template_output_contains_check_mark(self, tmp_path):
        p = _write(tmp_path, _minimal())
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert "✓" in result.output

    def test_invalid_template_output_contains_cross(self, tmp_path):
        content = """\
id: test-tpl
name: Test Template
version: "1.0.0"
description: ""
phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: [nonexistent]
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert "✗" in result.output

    def test_warning_output_contains_warning_symbol(self, tmp_path):
        """Unknown model tier should produce ⚠ in output."""
        p = _write(tmp_path, _minimal(model_tier='"unknown-tier"'))
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert "⚠" in result.output

    def test_yaml_check_pass_in_output(self, tmp_path):
        p = _write(tmp_path, _minimal())
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert "YAML syntax" in result.output
