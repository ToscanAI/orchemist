"""Tests for Template Documentation Standard (Issue #78).

Covers:
- PipelineTemplate dataclass has new fields (author, use_cases, example_input, tags, category)
- validate_template_extended ERROR on missing: description, author, version
- validate_template_extended WARN on missing recommended: use_cases, example_input
- validate_template_extended WARN on non-semver version
- orch templates info shows new documentation fields
- Bundled templates all pass extended validation
"""

import json
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from src.orchestration_engine.cli import main
from src.orchestration_engine.templates import (
    TemplateEngine,
    PipelineTemplate,
    PhaseDefinition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, content: str) -> Path:
    """Write *content* to a temp YAML file and return its path."""
    p = tmp_path / "tpl.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def _full_template_yaml() -> str:
    """Return a fully-documented template YAML string (all fields present)."""
    return """\
id: full-test-tpl
name: "Full Test Template"
version: "1.2.3"
description: "A fully documented template for testing purposes."
author: "Jane Developer"
category: "testing"
tags:
  - test
  - example
use_cases:
  - "Unit testing the documentation standard"
  - "Integration testing the validation pipeline"
example_input:
  topic: "AI safety"
  audience: "engineers"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Do something with {input[topic]}"
"""


def _minimal_yaml_with_author() -> str:
    """Return a minimal valid template with all required doc fields."""
    return """\
id: minimal-tpl
name: "Minimal Template"
version: "1.0.0"
description: "Minimal test template with required doc fields."
author: "Test Author"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello {input}"
"""


# ---------------------------------------------------------------------------
# 1. PipelineTemplate dataclass fields
# ---------------------------------------------------------------------------

class TestPipelineTemplateFields:
    def test_new_fields_exist_on_dataclass(self):
        """PipelineTemplate should have author, use_cases, example_input, tags, category."""
        tpl = PipelineTemplate(id="x", name="X")
        assert hasattr(tpl, "author")
        assert hasattr(tpl, "use_cases")
        assert hasattr(tpl, "example_input")
        assert hasattr(tpl, "tags")
        assert hasattr(tpl, "category")

    def test_new_fields_default_values(self):
        """New fields should default to empty string / empty list / empty dict."""
        tpl = PipelineTemplate(id="x", name="X")
        assert tpl.author == ""
        assert tpl.use_cases == []
        assert tpl.example_input == {}
        assert tpl.tags == []
        assert tpl.category == ""

    def test_load_template_populates_new_fields(self, tmp_path):
        """load_template() should populate author, use_cases, tags, category, example_input."""
        p = _write(tmp_path, _full_template_yaml())
        engine = TemplateEngine()
        template = engine.load_template(p)

        assert template.author == "Jane Developer"
        assert template.category == "testing"
        assert template.tags == ["test", "example"]
        assert template.use_cases == [
            "Unit testing the documentation standard",
            "Integration testing the validation pipeline",
        ]
        assert template.example_input == {"topic": "AI safety", "audience": "engineers"}

    def test_load_template_missing_optional_fields_uses_defaults(self, tmp_path):
        """load_template() on a template without new fields should use empty defaults."""
        p = _write(tmp_path, _minimal_yaml_with_author())
        engine = TemplateEngine()
        template = engine.load_template(p)

        assert template.author == "Test Author"
        assert template.use_cases == []
        assert template.example_input == {}
        assert template.tags == []
        assert template.category == ""

    def test_none_values_coerced_to_defaults(self, tmp_path):
        """YAML null values for new fields should be coerced to empty defaults."""
        content = """\
id: null-tpl
name: "Null Fields Template"
version: "1.0.0"
description: "Template with null new fields."
author: "Test Author"
use_cases: null
example_input: null
tags: null
category: null

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)

        assert template.use_cases == []
        assert template.example_input == {}
        assert template.tags == []
        assert template.category == ""


# ---------------------------------------------------------------------------
# 2. validate_template_extended — required field errors
# ---------------------------------------------------------------------------

class TestRequiredDocFieldErrors:
    def test_full_template_has_no_doc_errors(self, tmp_path):
        """A fully-documented template should produce no doc-related errors."""
        p = _write(tmp_path, _full_template_yaml())
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, warnings = engine.validate_template_extended(template, raw_data)

        doc_errors = [e for e in errors if "documentation" in e or "author" in e
                      or "description" in e or "version" in e.lower()]
        assert doc_errors == [], f"Unexpected doc errors: {doc_errors}"

    def test_missing_author_is_error(self, tmp_path):
        """Missing 'author' field should produce an ERROR."""
        content = """\
id: no-author-tpl
name: "No Author Template"
version: "1.0.0"
description: "Template without author."

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, _ = engine.validate_template_extended(template, raw_data)

        assert any("author" in e for e in errors), f"Expected author error, got: {errors}"

    def test_empty_author_is_error(self, tmp_path):
        """Empty 'author' field should also produce an ERROR."""
        content = """\
id: empty-author-tpl
name: "Empty Author Template"
version: "1.0.0"
description: "Template with empty author."
author: ""

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, _ = engine.validate_template_extended(template, raw_data)

        assert any("author" in e for e in errors), f"Expected author error, got: {errors}"

    def test_missing_description_is_error(self, tmp_path):
        """Missing 'description' field should produce an ERROR."""
        content = """\
id: no-desc-tpl
name: "No Description Template"
version: "1.0.0"
author: "Test Author"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, _ = engine.validate_template_extended(template, raw_data)

        assert any("description" in e for e in errors), f"Expected desc error, got: {errors}"

    def test_missing_version_is_error(self, tmp_path):
        """Missing 'version' field should produce an ERROR."""
        content = """\
id: no-version-tpl
name: "No Version Template"
description: "Template without version."
author: "Test Author"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, _ = engine.validate_template_extended(template, raw_data)

        assert any("version" in e.lower() for e in errors), f"Expected version error, got: {errors}"

    def test_missing_author_exits_1_via_cli(self, tmp_path):
        """CLI validate should exit 1 when author is missing."""
        content = """\
id: no-author-tpl
name: "No Author Template"
version: "1.0.0"
description: "Template without author."

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}\n{result.output}"


# ---------------------------------------------------------------------------
# 3. validate_template_extended — recommended field warnings
# ---------------------------------------------------------------------------

class TestRecommendedDocFieldWarnings:
    def test_missing_use_cases_is_warning(self, tmp_path):
        """Missing 'use_cases' should produce a WARNING (not error)."""
        p = _write(tmp_path, _minimal_yaml_with_author())
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, warnings = engine.validate_template_extended(template, raw_data)

        # Should be a warning, not an error
        use_case_errors = [e for e in errors if "use_cases" in e]
        use_case_warnings = [w for w in warnings if "use_cases" in w]
        assert use_case_errors == [], f"use_cases should not be an error: {use_case_errors}"
        assert use_case_warnings, f"Expected use_cases warning, got: {warnings}"

    def test_missing_example_input_is_warning(self, tmp_path):
        """Missing 'example_input' should produce a WARNING (not error)."""
        p = _write(tmp_path, _minimal_yaml_with_author())
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, warnings = engine.validate_template_extended(template, raw_data)

        ex_errors = [e for e in errors if "example_input" in e]
        ex_warnings = [w for w in warnings if "example_input" in w]
        assert ex_errors == [], f"example_input should not be an error: {ex_errors}"
        assert ex_warnings, f"Expected example_input warning, got: {warnings}"

    def test_missing_use_cases_does_not_exit_1(self, tmp_path):
        """CLI validate should exit 0 (warnings only) when use_cases is missing."""
        p = _write(tmp_path, _minimal_yaml_with_author())
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\n{result.output}"

    def test_full_template_no_doc_warnings(self, tmp_path):
        """A fully-documented template should have no doc-related warnings."""
        p = _write(tmp_path, _full_template_yaml())
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)

        doc_warnings = [w for w in warnings if "use_cases" in w or "example_input" in w]
        assert doc_warnings == [], f"Unexpected doc warnings: {doc_warnings}"


# ---------------------------------------------------------------------------
# 4. Semver version validation
# ---------------------------------------------------------------------------

class TestSemverValidation:
    @pytest.mark.parametrize("version", ["1.0.0", "2.3.4", "0.1.0", "10.20.30"])
    def test_valid_semver_no_warning(self, tmp_path, version):
        """Valid semver versions should not produce a version warning."""
        content = f"""\
id: semver-tpl
name: "Semver Test Template"
version: "{version}"
description: "Testing semver validation."
author: "Test Author"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        _, warnings = engine.validate_template_extended(template, raw_data)

        version_warnings = [w for w in warnings if "version" in w.lower() and "semver" in w.lower()]
        assert version_warnings == [], f"Unexpected version warnings: {version_warnings}"

    @pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-alpha", "latest", "1"])
    def test_invalid_semver_is_warning(self, tmp_path, version):
        """Non-semver versions should produce a WARNING (not error)."""
        content = f"""\
id: bad-semver-tpl
name: "Bad Semver Template"
version: "{version}"
description: "Testing semver validation."
author: "Test Author"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        engine = TemplateEngine()
        template = engine.load_template(p)
        raw_data = yaml.safe_load(p.read_text())
        errors, warnings = engine.validate_template_extended(template, raw_data)

        # Should be warning, not error
        version_errors = [e for e in errors if "version" in e.lower() and "semver" in e.lower()]
        version_warnings = [w for w in warnings if "version" in w.lower() and "semver" in w.lower()]
        assert version_errors == [], f"Semver mismatch should be warning, not error: {version_errors}"
        assert version_warnings, f"Expected semver warning for version={version!r}, got: {warnings}"

    def test_invalid_semver_exits_0_via_cli(self, tmp_path):
        """CLI validate with non-semver version should exit 0 (warning only)."""
        content = """\
id: bad-semver-tpl
name: "Bad Semver Template"
version: "1.0"
description: "Testing semver validation."
author: "Test Author"

phases:
  - id: phase_a
    name: Phase A
    model_tier: sonnet
    thinking_level: low
    depends_on: []
    prompt_template: "Hello"
"""
        p = _write(tmp_path, content)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(p)])
        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\n{result.output}"
        assert "⚠" in result.output or "semver" in result.output.lower()


# ---------------------------------------------------------------------------
# 5. orch templates info — displays new documentation fields
# ---------------------------------------------------------------------------

class TestTemplatesInfoDisplaysDocFields:
    def test_info_displays_author(self, tmp_path, monkeypatch):
        """orch templates info should display the author field."""
        p = _write(tmp_path, _full_template_yaml())
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "info", str(p)])

        assert "Jane Developer" in result.output, f"Author not in output:\n{result.output}"

    def test_info_displays_category(self, tmp_path, monkeypatch):
        """orch templates info should display the category field."""
        p = _write(tmp_path, _full_template_yaml())
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "info", str(p)])

        assert "testing" in result.output, f"Category not in output:\n{result.output}"

    def test_info_displays_tags(self, tmp_path, monkeypatch):
        """orch templates info should display tags."""
        p = _write(tmp_path, _full_template_yaml())
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "info", str(p)])

        assert "test" in result.output and "example" in result.output, \
            f"Tags not in output:\n{result.output}"

    def test_info_displays_use_cases(self, tmp_path, monkeypatch):
        """orch templates info should display use_cases."""
        p = _write(tmp_path, _full_template_yaml())
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "info", str(p)])

        assert "Unit testing" in result.output, f"Use case not in output:\n{result.output}"

    def test_info_displays_example_input(self, tmp_path, monkeypatch):
        """orch templates info should display example_input."""
        p = _write(tmp_path, _full_template_yaml())
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "info", str(p)])

        assert "AI safety" in result.output, f"Example input not in output:\n{result.output}"

    def test_info_omits_doc_section_when_no_doc_fields(self, tmp_path, monkeypatch):
        """orch templates info should not crash if no doc fields are set."""
        p = _write(tmp_path, _minimal_yaml_with_author())
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "info", str(p)])

        # Should succeed and show template name at minimum
        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        assert "Minimal Template" in result.output


# ---------------------------------------------------------------------------
# 6. Bundled templates pass extended validation
# ---------------------------------------------------------------------------

class TestBundledTemplatesCompliance:
    """Ensure all bundled templates comply with the documentation standard."""

    def _get_bundled_template_paths(self) -> list:
        """Find all bundled template YAML files."""
        engine = TemplateEngine()
        # Check project templates/ and examples/ directories
        repo_root = Path(__file__).parent.parent
        paths = []
        for subdir in ("templates", "examples"):
            d = repo_root / subdir
            if d.exists():
                paths.extend(sorted(d.glob("*.yaml")))
                paths.extend(sorted(d.glob("*.yml")))
        return paths

    def test_bundled_templates_exist(self):
        """There should be at least one bundled template to validate."""
        paths = self._get_bundled_template_paths()
        assert paths, "No bundled templates found in templates/ or examples/"

    def test_bundled_templates_have_required_doc_fields(self):
        """All bundled templates should have description, author, and semver version."""
        engine = TemplateEngine()
        paths = self._get_bundled_template_paths()

        failures = []
        for path in paths:
            raw_data = yaml.safe_load(path.read_text())
            template = engine.load_template(path)
            errors, warnings = engine.validate_template_extended(template, raw_data)

            doc_errors = [e for e in errors if "documentation" in e
                          or "author" in e or "description" in e]
            if doc_errors:
                failures.append(f"{path.name}: {doc_errors}")

        assert not failures, "Bundled templates have doc field errors:\n" + "\n".join(failures)

    def test_bundled_templates_pass_validate_command(self):
        """All bundled templates should pass orch validate (exit 0 = warnings OK)."""
        repo_root = Path(__file__).parent.parent
        runner = CliRunner()

        paths = self._get_bundled_template_paths()
        failures = []
        for path in paths:
            result = runner.invoke(main, ["validate", str(path)])
            if result.exit_code != 0:
                failures.append(
                    f"{path.name}: exit_code={result.exit_code}\n{result.output}"
                )

        assert not failures, "Bundled templates failed validation:\n\n".join(failures)

    @pytest.mark.parametrize("template_name", ["content-pipeline.yaml", "hello-pipeline.yaml"])
    def test_specific_bundled_template_has_author(self, template_name):
        """Specific bundled templates must have a non-empty author field."""
        repo_root = Path(__file__).parent.parent
        # Check both templates/ and examples/
        found = None
        for subdir in ("templates", "examples"):
            candidate = repo_root / subdir / template_name
            if candidate.exists():
                found = candidate
                break

        if found is None:
            pytest.skip(f"{template_name} not found in templates/ or examples/")

        engine = TemplateEngine()
        template = engine.load_template(found)
        assert template.author, f"{template_name} must have a non-empty 'author' field"

    @pytest.mark.parametrize("template_name", ["content-pipeline.yaml", "hello-pipeline.yaml"])
    def test_specific_bundled_template_has_use_cases(self, template_name):
        """Specific bundled templates must have at least one use case."""
        repo_root = Path(__file__).parent.parent
        found = None
        for subdir in ("templates", "examples"):
            candidate = repo_root / subdir / template_name
            if candidate.exists():
                found = candidate
                break

        if found is None:
            pytest.skip(f"{template_name} not found in templates/ or examples/")

        engine = TemplateEngine()
        template = engine.load_template(found)
        assert template.use_cases, f"{template_name} must have at least one 'use_cases' entry"
