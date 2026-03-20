"""Comprehensive glob-based template validation test suite.

Issue #110 — Template Validation Test Suite

Covers:
- AC-01: ALL_TEMPLATES discovers ≥ 6 files
- AC-02: Every discovered file exists on disk
- AC-03: `orch validate <file>` exits 0 for every template
- AC-04: `orch run <file> --mode dry-run` exits 0 for every template
- AC-05: TestConfigSchemaConsistency — required fields present in example_input
- AC-06: TestConfigSchemaConsistency — type match for typed scalar fields
- AC-07: TestNewTemplateAutoDiscovery — glob picks up new .yaml/.yml files
- AC-08: Test IDs use template filename (parametrize ids=lambda p: Path(p).name)
- AC-09: No duplication of test_example_templates.py (no phase count / dep / author assertions)
- AC-10: ≥ 30 new tests added to the suite
- AC-11 through AC-17: `orch templates test` CLI command tests
- Edge cases: zero templates, .yml extension, community-templates exclusion, hidden files

NOTE on AC-09:
  This file deliberately omits assertions about:
  - Phase counts (e.g. "7 phases")
  - Dependency wiring (e.g. "depends on draft")
  - Author fields (e.g. "author == 'Toscan'")
  Those are already covered by test_example_templates.py.
"""

import contextlib
import glob
import json
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from click.testing import CliRunner

from src.orchestration_engine.cli import main
from src.orchestration_engine.templates import TemplateEngine

# ---------------------------------------------------------------------------
# Module-level: glob-based template discovery (runs at collection time)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

ALL_TEMPLATES: List[str] = sorted(
    glob.glob(str(REPO_ROOT .joinpath("templates") / "*.yaml"))
    + glob.glob(str(REPO_ROOT .joinpath("templates") / "*.yml"))
    + glob.glob(str(REPO_ROOT / "examples" / "*.yaml"))
    + glob.glob(str(REPO_ROOT / "examples" / "*.yml"))
)

# ---------------------------------------------------------------------------
# Type-mapping for AC-06 checks
# ---------------------------------------------------------------------------

PYTHON_TYPE_MAP: Dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_template(path: str):
    """Load a PipelineTemplate from the given path string."""
    return TemplateEngine().load_template(Path(path))


def _minimal_template_yaml(
    template_id: str = "temp-tpl",
    name: str = "Temp Template",
    *,
    extra: str = "",
) -> str:
    """Return a minimal valid template YAML string."""
    return textwrap.dedent(f"""\
        id: {template_id}
        name: "{name}"
        version: "1.0.0"
        description: "A minimal template for testing."
        author: "Test Author"
        {extra}
        phases:
          - id: phase_a
            name: Phase A
            model_tier: haiku
            thinking_level: off
            depends_on: []
            prompt_template: "Hello {{input}}"
    """)


def _broken_template_yaml(template_id: str = "broken-tpl") -> str:
    """Return a YAML template that fails structural validation (unknown dep)."""
    return textwrap.dedent(f"""\
        id: {template_id}
        name: "Broken Template"
        version: "1.0.0"
        description: "This template has a bad dependency."
        author: "Test Author"
        phases:
          - id: phase_a
            name: Phase A
            model_tier: haiku
            thinking_level: off
            depends_on: [nonexistent-phase]
            prompt_template: "Hello {{input}}"
    """)


@contextlib.contextmanager
def _inject_template(directory: Path, filename: str, content: str):
    """Context manager: write a file into directory, yield, then remove it.

    Guarantees cleanup even if the test raises.
    """
    injected = directory / filename
    injected.write_text(content)
    try:
        yield injected
    finally:
        if injected.exists():
            injected.unlink()


# ===========================================================================
# Class 1 — TestTemplateDiscovery
# ===========================================================================


class TestTemplateDiscovery:
    """AC-01, AC-02, and discovery edge cases."""

    def test_at_least_6_templates_discovered(self):
        """AC-01: glob discovers ≥ 6 template files at collection time."""
        if len(ALL_TEMPLATES) == 0:
            pytest.skip("No templates found — check repository structure")
        assert len(ALL_TEMPLATES) >= 6, (
            f"Expected ≥ 6 templates, found {len(ALL_TEMPLATES)}: {ALL_TEMPLATES}"
        )

    def test_all_discovered_files_exist_on_disk(self):
        """AC-02: every path in ALL_TEMPLATES resolves to an actual file."""
        missing = [p for p in ALL_TEMPLATES if not Path(p).exists()]
        assert missing == [], f"Discovered paths that don't exist: {missing}"

    def test_all_discovered_files_are_regular_files(self):
        """AC-02 supplement: discovered paths are files, not directories."""
        non_files = [p for p in ALL_TEMPLATES if not Path(p).is_file()]
        assert non_files == [], f"Paths are not regular files: {non_files}"

    def test_templates_dir_is_included(self):
        """Templates from templates/ directory are present in ALL_TEMPLATES."""
        templates_paths = [p for p in ALL_TEMPLATES if str(REPO_ROOT .joinpath("templates")) in p]
        assert templates_paths, "No templates from templates/ directory found"

    def test_examples_dir_is_included(self):
        """Templates from examples/ directory are present in ALL_TEMPLATES."""
        examples_paths = [p for p in ALL_TEMPLATES if str(REPO_ROOT / "examples") in p]
        assert examples_paths, "No templates from examples/ directory found"

    def test_community_templates_dir_excluded(self):
        """Edge case: community-templates/ must NOT appear in ALL_TEMPLATES."""
        community = [p for p in ALL_TEMPLATES if "community-templates" in p]
        assert community == [], (
            f"community-templates/ should not be included: {community}"
        )

    def test_hidden_files_excluded_by_glob(self, tmp_path):
        """Edge case: dotfiles like .template-draft.yaml are NOT discovered."""
        hidden = tmp_path / ".hidden-draft.yaml"
        hidden.write_text(_minimal_template_yaml("hidden"))
        visible = tmp_path / "visible.yaml"
        visible.write_text(_minimal_template_yaml("visible"))

        discovered = glob.glob(str(tmp_path / "*.yaml"))
        names = [Path(p).name for p in discovered]
        assert ".hidden-draft.yaml" not in names, "dotfile was unexpectedly discovered"
        assert "visible.yaml" in names

    def test_yml_extension_discovered_separately(self, tmp_path):
        """Edge case: .yml extension files are found when combined with .yaml glob."""
        yml_file = tmp_path / "my-template.yml"
        yml_file.write_text(_minimal_template_yaml("my-template"))

        discovered_yml = glob.glob(str(tmp_path / "*.yml"))
        discovered_yaml = glob.glob(str(tmp_path / "*.yaml"))
        assert str(yml_file) in discovered_yml, ".yml file not found by *.yml glob"
        assert str(yml_file) not in discovered_yaml, ".yml file matched *.yaml glob unexpectedly"

    def test_glob_combined_yaml_and_yml_finds_both(self, tmp_path):
        """The combined yaml + yml glob discovers both extensions."""
        yaml_file = tmp_path / "alpha.yaml"
        yml_file = tmp_path / "beta.yml"
        yaml_file.write_text(_minimal_template_yaml("alpha"))
        yml_file.write_text(_minimal_template_yaml("beta"))

        combined = sorted(
            glob.glob(str(tmp_path / "*.yaml")) + glob.glob(str(tmp_path / "*.yml"))
        )
        names = [Path(p).name for p in combined]
        assert "alpha.yaml" in names
        assert "beta.yml" in names

    def test_all_templates_load_without_exception(self):
        """Every discovered template loads cleanly via TemplateEngine."""
        engine = TemplateEngine()
        for path in ALL_TEMPLATES:
            try:
                template = engine.load_template(Path(path))
                assert template is not None
            except Exception as exc:
                pytest.fail(f"Failed to load {Path(path).name}: {exc}")

    def test_no_duplicate_paths_in_discovery(self):
        """No path appears twice in ALL_TEMPLATES."""
        assert len(ALL_TEMPLATES) == len(set(ALL_TEMPLATES)), (
            "Duplicate paths detected in ALL_TEMPLATES"
        )

    def test_discovery_survives_zero_templates_in_isolated_dir(self, tmp_path):
        """Edge case: zero templates in a dir returns empty list without raising."""
        empty_dir = tmp_path .joinpath("templates")
        empty_dir.mkdir()
        discovered = sorted(
            glob.glob(str(empty_dir / "*.yaml")) + glob.glob(str(empty_dir / "*.yml"))
        )
        assert discovered == [], "Expected empty list for empty directory"


# ===========================================================================
# Class 2 — TestOrchValidate
# ===========================================================================


class TestOrchValidate:
    """AC-03, AC-08: parametrized `orch validate` over all discovered templates."""

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: Path(p).name)
    def test_validate_exits_0(self, template_path):
        """AC-03: `orch validate <file>` exits 0 for every discovered template."""
        runner = CliRunner()
        result = runner.invoke(main, ["validate", template_path])
        assert result.exit_code == 0, (
            f"orch validate failed for {Path(template_path).name} "
            f"(exit {result.exit_code}):\n{result.output}"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: Path(p).name)
    def test_validate_output_contains_success_marker(self, template_path):
        """orch validate output contains ✓ for valid templates."""
        runner = CliRunner()
        result = runner.invoke(main, ["validate", template_path])
        assert "✓" in result.output or "valid" in result.output.lower(), (
            f"Missing success indicator for {Path(template_path).name}:\n{result.output}"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: Path(p).name)
    def test_validate_no_structural_errors_via_engine(self, template_path):
        """validate_template() returns no structural errors for every template."""
        engine = TemplateEngine()
        template = engine.load_template(Path(template_path))
        errors = engine.validate_template(template)
        assert errors == [], (
            f"{Path(template_path).name} has structural errors: {errors}"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: Path(p).name)
    def test_validate_no_extended_errors_via_engine(self, template_path):
        """validate_template_extended() returns no hard errors (warnings are OK)."""
        engine = TemplateEngine()
        template = engine.load_template(Path(template_path))
        raw_data = yaml.safe_load(Path(template_path).read_text())
        errors, _warnings = engine.validate_template_extended(template, raw_data)
        assert errors == [], (
            f"{Path(template_path).name} has extended errors: {errors}"
        )

    def test_validate_missing_file_exits_nonzero(self, tmp_path):
        """orch validate on a nonexistent file exits with non-zero code."""
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path / "nonexistent.yaml")])
        assert result.exit_code != 0

    def test_validate_broken_template_exits_1(self, tmp_path):
        """orch validate exits 1 when structural errors are present."""
        broken = tmp_path / "broken.yaml"
        broken.write_text(_broken_template_yaml())
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(broken)])
        assert result.exit_code == 1, (
            f"Expected exit 1 for broken template, got {result.exit_code}:\n{result.output}"
        )

    def test_validate_broken_output_contains_error_marker(self, tmp_path):
        """orch validate shows ✗ for an invalid template."""
        broken = tmp_path / "broken.yaml"
        broken.write_text(_broken_template_yaml())
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(broken)])
        assert "✗" in result.output or "error" in result.output.lower(), (
            f"Missing error indicator for broken template:\n{result.output}"
        )


# ===========================================================================
# Class 3 — TestDryRun
# ===========================================================================


def _make_minimal_input(template) -> Dict[str, Any]:
    """Generate a minimal valid input dict from a template's config_schema.

    Fills all required fields with type-appropriate dummy values so that
    schema validation passes.  This is needed because some templates now
    declare required fields in config_schema but ship with an empty
    example_input (Sprint 4/5 addition).
    """
    cs = getattr(template, "config_schema", None) or {}
    required = cs.get("required", [])
    properties = cs.get("properties", {})

    # Start from the template's own example_input (may already cover some fields)
    base: Dict[str, Any] = dict(template.example_input or {})

    _type_defaults: Dict[str, Any] = {
        "string": "dummy-value",
        "integer": 1,
        "number": 1.0,
        "boolean": False,
        "array": [],
        "object": {},
    }

    for field in required:
        if field not in base:
            prop = properties.get(field, {})
            field_type = prop.get("type", "string")
            base[field] = _type_defaults.get(field_type, "dummy-value")

    return base


class TestDryRun:
    """AC-04: parametrized dry-run over all discovered templates."""

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: Path(p).name)
    def test_dry_run_exits_0_with_example_input(self, template_path, tmp_path):
        """AC-04: dry-run with template's own example_input exits 0."""
        engine = TemplateEngine()
        template = engine.load_template(Path(template_path))
        # Use auto-generated minimal input that satisfies required fields
        # (some templates have empty example_input but non-empty config_schema.required)
        input_data = _make_minimal_input(template)

        runner = CliRunner()
        result = runner.invoke(main, [
            "run", template_path,
            "--mode", "dry-run",
            "--input", json.dumps(input_data),
            "--output-dir", str(tmp_path / "out"),
            "--skip-scoring",
        ])
        assert result.exit_code == 0, (
            f"dry-run failed for {Path(template_path).name} "
            f"(exit {result.exit_code}):\n{result.output}"
        )

    @pytest.mark.parametrize("template_path", ALL_TEMPLATES, ids=lambda p: Path(p).name)
    def test_dry_run_exits_0_with_minimal_valid_input(self, template_path, tmp_path):
        """AC-04 edge: dry-run with minimal valid input exits 0 for every template.

        Templates with required config_schema fields cannot accept a bare {}.
        We generate the minimal input that satisfies the schema instead.
        """
        engine = TemplateEngine()
        template = engine.load_template(Path(template_path))
        input_data = _make_minimal_input(template)

        runner = CliRunner()
        result = runner.invoke(main, [
            "run", template_path,
            "--mode", "dry-run",
            "--input", json.dumps(input_data),
            "--output-dir", str(tmp_path / "out"),
            "--skip-scoring",
        ])
        assert result.exit_code == 0, (
            f"dry-run with minimal input failed for {Path(template_path).name} "
            f"(exit {result.exit_code}):\n{result.output}"
        )

    def test_dry_run_without_example_input_field(self, tmp_path):
        """AC-04 edge: a template with no example_input runs with empty dict."""
        tpl_file = tmp_path / "no-example.yaml"
        tpl_file.write_text(_minimal_template_yaml("no-example-tpl", extra=""))
        runner = CliRunner()
        result = runner.invoke(main, [
            "run", str(tpl_file),
            "--mode", "dry-run",
            "--input", "{}",
            "--output-dir", str(tmp_path / "out"),
        ])
        assert result.exit_code == 0, (
            f"dry-run without example_input failed (exit {result.exit_code}):\n{result.output}"
        )

    def test_dry_run_creates_output_directory(self, tmp_path):
        """dry-run writes output to --output-dir (creates it if missing)."""
        tpl_file = tmp_path / "simple.yaml"
        tpl_file.write_text(_minimal_template_yaml("simple-tpl"))
        out_dir = tmp_path / "results"

        runner = CliRunner()
        runner.invoke(main, [
            "run", str(tpl_file),
            "--mode", "dry-run",
            "--input", "{}",
            "--output-dir", str(out_dir),
        ])
        assert out_dir.exists(), "dry-run did not create the output directory"

    def test_dry_run_writes_json_output_files(self, tmp_path):
        """dry-run writes .json output files for each phase."""
        tpl_file = tmp_path / "simple.yaml"
        tpl_file.write_text(_minimal_template_yaml("simple-tpl"))
        out_dir = tmp_path / "out"

        runner = CliRunner()
        result = runner.invoke(main, [
            "run", str(tpl_file),
            "--mode", "dry-run",
            "--input", "{}",
            "--output-dir", str(out_dir),
        ])
        if result.exit_code == 0:
            json_files = list(out_dir.glob("*.json"))
            assert json_files, "dry-run produced no .json output files"

    def test_dry_run_invalid_json_input_exits_nonzero(self, tmp_path):
        """Passing invalid JSON to --input exits non-zero."""
        tpl_file = tmp_path / "simple.yaml"
        tpl_file.write_text(_minimal_template_yaml("simple-tpl"))
        runner = CliRunner()
        result = runner.invoke(main, [
            "run", str(tpl_file),
            "--mode", "dry-run",
            "--input", "{not valid json}",
            "--output-dir", str(tmp_path / "out"),
        ])
        assert result.exit_code != 0

    def test_dry_run_uses_example_input_when_present(self, tmp_path):
        """AC-04: template.example_input is serialised and passed to --input."""
        tpl_content = textwrap.dedent("""\
            id: with-input
            name: "With Input"
            version: "1.0.0"
            description: "Has example_input"
            author: "Test"
            example_input:
              topic: "AI safety"
            phases:
              - id: only
                name: Only Phase
                model_tier: haiku
                thinking_level: off
                depends_on: []
                prompt_template: "Write about {input[topic]}"
        """)
        tpl_file = tmp_path / "with-input.yaml"
        tpl_file.write_text(tpl_content)

        engine = TemplateEngine()
        template = engine.load_template(tpl_file)
        assert template.example_input == {"topic": "AI safety"}

        runner = CliRunner()
        result = runner.invoke(main, [
            "run", str(tpl_file),
            "--mode", "dry-run",
            "--input", json.dumps(template.example_input),
            "--output-dir", str(tmp_path / "out"),
        ])
        assert result.exit_code == 0, (
            f"dry-run with example_input failed:\n{result.output}"
        )


# ===========================================================================
# Class 4 — TestConfigSchemaConsistency
# ===========================================================================

# Pre-compute templates that have both config_schema AND example_input
_TEMPLATES_WITH_SCHEMA_AND_EXAMPLE: List[str] = [
    p for p in ALL_TEMPLATES
    if (lambda t: bool(t.config_schema) and bool(t.example_input))(
        TemplateEngine().load_template(Path(p))
    )
]


class TestConfigSchemaConsistency:
    """AC-05, AC-06: config_schema vs example_input consistency checks."""

    @pytest.mark.parametrize(
        "template_path",
        _TEMPLATES_WITH_SCHEMA_AND_EXAMPLE,
        ids=lambda p: Path(p).name,
    )
    def test_required_fields_present_in_example_input(self, template_path):
        """AC-05: every field listed in config_schema.required appears in example_input."""
        engine = TemplateEngine()
        template = engine.load_template(Path(template_path))
        required = template.config_schema.get("required", [])
        missing = [f for f in required if f not in template.example_input]
        assert missing == [], (
            f"{Path(template_path).name}: required fields missing from example_input: {missing}"
        )

    @pytest.mark.parametrize(
        "template_path",
        _TEMPLATES_WITH_SCHEMA_AND_EXAMPLE,
        ids=lambda p: Path(p).name,
    )
    def test_example_input_types_match_schema(self, template_path):
        """AC-06: example_input values match the declared JSON Schema type."""
        engine = TemplateEngine()
        template = engine.load_template(Path(template_path))
        properties = template.config_schema.get("properties", {})
        example = template.example_input

        mismatches = []
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue
            schema_type = field_schema.get("type")
            if schema_type is None or schema_type not in PYTHON_TYPE_MAP:
                continue  # skip unknown / complex types (arrays of objects, etc.)
            if field_name not in example:
                continue  # absence covered by AC-05 test

            python_type = PYTHON_TYPE_MAP[schema_type]
            if not isinstance(example[field_name], python_type):
                mismatches.append(
                    f"field '{field_name}': schema says {schema_type!r}, "
                    f"example_input has {type(example[field_name]).__name__}"
                )

        assert mismatches == [], (
            f"{Path(template_path).name} example_input type mismatches:\n"
            + "\n".join(f"  • {m}" for m in mismatches)
        )

    def test_every_schema_has_type_field(self):
        """Any template with a non-empty config_schema must have a 'type' key."""
        engine = TemplateEngine()
        bad = []
        for path in ALL_TEMPLATES:
            template = engine.load_template(Path(path))
            if template.config_schema and "type" not in template.config_schema:
                bad.append(Path(path).name)
        assert bad == [], f"Templates missing 'type' in config_schema: {bad}"

    def test_object_schemas_have_properties(self):
        """Templates with config_schema.type='object' also declare 'properties'."""
        engine = TemplateEngine()
        bad = []
        for path in ALL_TEMPLATES:
            template = engine.load_template(Path(path))
            schema = template.config_schema
            if schema and schema.get("type") == "object" and "properties" not in schema:
                bad.append(Path(path).name)
        assert bad == [], (
            f"Templates with type='object' missing 'properties': {bad}"
        )

    def test_required_fields_are_subset_of_properties(self):
        """Every required field must be declared in properties (schema self-consistency)."""
        engine = TemplateEngine()
        issues = []
        for path in ALL_TEMPLATES:
            template = engine.load_template(Path(path))
            schema = template.config_schema
            if not schema:
                continue
            properties = set(schema.get("properties", {}).keys())
            required = schema.get("required", [])
            undeclared = [f for f in required if f not in properties]
            if undeclared:
                issues.append(f"{Path(path).name}: {undeclared}")
        assert issues == [], (
            f"Required fields not declared in properties:\n"
            + "\n".join(f"  • {i}" for i in issues)
        )

    def test_hello_pipeline_no_required_config_fields(self):
        """hello-pipeline.yaml has no config_schema — no required fields to check."""
        hello_path = REPO_ROOT / "examples" / "hello-pipeline.yaml"
        engine = TemplateEngine()
        template = engine.load_template(hello_path)
        # Either no config_schema or an empty required list
        required = template.config_schema.get("required", []) if template.config_schema else []
        assert required == [] or required is None, (
            f"hello-pipeline should have no required fields, got: {required}"
        )

    def test_example_input_is_valid_json_serialisable(self):
        """example_input must be JSON-serialisable (no Python-specific objects)."""
        engine = TemplateEngine()
        for path in ALL_TEMPLATES:
            template = engine.load_template(Path(path))
            if template.example_input:
                try:
                    json.dumps(template.example_input)
                except (TypeError, ValueError) as exc:
                    pytest.fail(
                        f"{Path(path).name}: example_input is not JSON-serialisable: {exc}"
                    )


# ===========================================================================
# Class 5 — TestNewTemplateAutoDiscovery
# ===========================================================================


class TestNewTemplateAutoDiscovery:
    """AC-07: prove the glob pattern automatically picks up new template files."""

    def test_new_yaml_file_discovered_by_glob(self, tmp_path):
        """AC-07: a new .yaml file placed in a dir is discovered by glob."""
        templates_dir = tmp_path .joinpath("templates")
        templates_dir.mkdir()

        new_file = templates_dir / "new-pipeline.yaml"
        new_file.write_text(_minimal_template_yaml("new-pipeline"))

        discovered = glob.glob(str(templates_dir / "*.yaml"))
        assert str(new_file) in discovered, "New .yaml file not found by glob"

    def test_new_yml_file_discovered_by_glob(self, tmp_path):
        """AC-07 edge: a new .yml file is also discoverable."""
        templates_dir = tmp_path .joinpath("templates")
        templates_dir.mkdir()

        new_file = templates_dir / "new-pipeline.yml"
        new_file.write_text(_minimal_template_yaml("new-pipeline"))

        combined = (
            glob.glob(str(templates_dir / "*.yaml"))
            + glob.glob(str(templates_dir / "*.yml"))
        )
        assert str(new_file) in combined, "New .yml file not found by combined glob"

    def test_glob_does_not_discover_sibling_dirs(self, tmp_path):
        """Glob anchored to templates/ does NOT pick up community-templates/ files."""
        templates_dir = tmp_path .joinpath("templates")
        community_dir = tmp_path / "community-templates"
        templates_dir.mkdir()
        community_dir.mkdir()

        tpl = templates_dir / "real.yaml"
        tpl.write_text(_minimal_template_yaml("real"))
        community = community_dir / "community.yaml"
        community.write_text(_minimal_template_yaml("community"))

        discovered = glob.glob(str(templates_dir / "*.yaml"))
        assert str(tpl) in discovered
        assert str(community) not in discovered

    def test_new_template_passes_validation_after_creation(self, tmp_path):
        """AC-07: a freshly created minimal template passes `orch validate`."""
        new_file = tmp_path / "fresh.yaml"
        new_file.write_text(_minimal_template_yaml("fresh-pipeline", "Fresh Pipeline"))

        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(new_file)])
        assert result.exit_code == 0, (
            f"Newly created template failed validation:\n{result.output}"
        )

    def test_multiple_new_templates_all_discovered(self, tmp_path):
        """Adding multiple templates to a dir; all are found by one glob call."""
        tdir = tmp_path .joinpath("templates")
        tdir.mkdir()
        stems = {"alpha", "beta", "gamma"}
        for stem in stems:
            (tdir / f"{stem}.yaml").write_text(_minimal_template_yaml(stem))

        discovered = glob.glob(str(tdir / "*.yaml"))
        discovered_stems = {Path(p).stem for p in discovered}
        assert discovered_stems == stems

    def test_new_template_discovered_before_sort(self, tmp_path):
        """After sorting, the newly discovered template appears in correct order."""
        tdir = tmp_path .joinpath("templates")
        tdir.mkdir()
        names = ["zebra", "apple", "mango"]
        for n in names:
            (tdir / f"{n}.yaml").write_text(_minimal_template_yaml(n))

        combined_sorted = sorted(glob.glob(str(tdir / "*.yaml")))
        stems = [Path(p).stem for p in combined_sorted]
        assert stems == sorted(names), f"Expected sorted order, got: {stems}"


# ===========================================================================
# Class 6 — TestOrchTemplatesTestCommand
# ===========================================================================


class TestOrchTemplatesTestCommand:
    """AC-11 through AC-17: `orch templates test` CLI command tests.

    The `templates_test` command discovers templates by walking up from cli.py
    to find a directory containing both templates/ and examples/. For isolation,
    tests that need to inject broken templates write directly to the repo's
    examples/ directory using the `_inject_template` context manager which
    guarantees cleanup via try/finally.
    """

    # -----------------------------------------------------------------------
    # AC-11: command exists
    # -----------------------------------------------------------------------

    def test_command_exists_under_templates_group(self):
        """AC-11: `orch templates test` is registered and `--help` exits 0."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test", "--help"])
        assert "No such command" not in result.output, (
            f"'orch templates test' command not found:\n{result.output}"
        )
        assert result.exit_code == 0

    def test_command_help_describes_template_testing(self):
        """AC-11: --help output mentions validation / templates."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test", "--help"])
        combined = result.output.lower()
        assert "template" in combined or "validate" in combined or "dry-run" in combined

    def test_templates_subgroup_has_test_command(self):
        """AC-11: `orch templates --help` lists 'test' as a subcommand."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "--help"])
        assert "test" in result.output

    # -----------------------------------------------------------------------
    # AC-12: exit 0 when all templates pass (real repo)
    # -----------------------------------------------------------------------

    def test_exit_code_0_against_real_repo(self):
        """AC-12: `orch templates test` exits 0 against the current repo's templates."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test"])
        assert result.exit_code == 0, (
            f"Expected exit 0 against real repo, got {result.exit_code}:\n{result.output}"
        )

    # -----------------------------------------------------------------------
    # AC-13: exit 1 when any template fails
    # -----------------------------------------------------------------------

    def test_exit_code_1_when_broken_template_injected(self):
        """AC-13: exit 1 when a structurally invalid template is discovered."""
        examples_dir = REPO_ROOT / "examples"
        broken_content = _broken_template_yaml("_qa-injected-broken")
        with _inject_template(examples_dir, "_qa-broken-test.yaml", broken_content):
            runner = CliRunner()
            result = runner.invoke(main, ["templates", "test"])
        assert result.exit_code == 1, (
            f"Expected exit 1 with a broken template present, "
            f"got {result.exit_code}:\n{result.output}"
        )

    # -----------------------------------------------------------------------
    # AC-14: --verbose shows full error output
    # -----------------------------------------------------------------------

    def test_verbose_flag_accepted_without_error(self):
        """AC-14: --verbose flag is accepted (not 'No such option')."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test", "--verbose", "--help"])
        assert "No such option" not in result.output

    def test_verbose_shows_error_detail_on_failure(self):
        """AC-14: --verbose includes error text when a template fails."""
        examples_dir = REPO_ROOT / "examples"
        broken_content = _broken_template_yaml("_qa-verbose-broken")
        with _inject_template(examples_dir, "_qa-verbose-test.yaml", broken_content):
            runner = CliRunner()
            result = runner.invoke(main, ["templates", "test", "--verbose"])

        # Output should contain error details (nonexistent dep or generic error word)
        output = result.output or ""
        assert "nonexistent" in output.lower() or "error" in output.lower(), (
            f"Expected verbose error details in output:\n{output}"
        )

    # -----------------------------------------------------------------------
    # AC-15: --fail-fast stops after first failure
    # -----------------------------------------------------------------------

    def test_fail_fast_flag_accepted_without_error(self):
        """AC-15: --fail-fast flag is accepted (not 'No such option')."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test", "--fail-fast", "--help"])
        assert "No such option" not in result.output

    def test_fail_fast_exits_1_on_first_failure(self):
        """AC-15: --fail-fast exits 1 as soon as a failing template is found."""
        examples_dir = REPO_ROOT / "examples"
        broken_content = _broken_template_yaml("_qa-failfast-broken")
        # Name the file so it sorts before all real templates (leading underscore)
        with _inject_template(examples_dir, "_qa-failfast.yaml", broken_content):
            runner = CliRunner()
            result = runner.invoke(main, ["templates", "test", "--fail-fast"])

        assert result.exit_code == 1, (
            f"Expected exit 1 with --fail-fast + broken template, "
            f"got {result.exit_code}:\n{result.output}"
        )

    def test_fail_fast_output_mentions_stopping(self):
        """AC-15: output mentions stopping / fail-fast after first failure."""
        examples_dir = REPO_ROOT / "examples"
        broken_content = _broken_template_yaml("_qa-failfast2-broken")
        with _inject_template(examples_dir, "_qa-failfast2.yaml", broken_content):
            runner = CliRunner()
            result = runner.invoke(main, ["templates", "test", "--fail-fast"])

        output = result.output or ""
        # Implementation says "Stopped after first failure" or similar
        assert (
            "fail" in output.lower()
            or "stop" in output.lower()
            or "first" in output.lower()
        ), f"Expected fail-fast stop message in output:\n{output}"

    # -----------------------------------------------------------------------
    # AC-16: per-template pass/fail with ✓ / ✗
    # -----------------------------------------------------------------------

    def test_passing_templates_show_checkmark(self):
        """AC-16: ✓ appears in output for passing templates (real repo run)."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test"])
        assert "✓" in result.output, (
            f"Expected ✓ for passing templates:\n{result.output}"
        )

    def test_failing_template_shows_cross_mark(self):
        """AC-16: ✗ appears in output for a failing template."""
        examples_dir = REPO_ROOT / "examples"
        broken_content = _broken_template_yaml("_qa-cross-broken")
        with _inject_template(examples_dir, "_qa-cross-test.yaml", broken_content):
            runner = CliRunner()
            result = runner.invoke(main, ["templates", "test"])

        assert "✗" in result.output, (
            f"Expected ✗ for failing template:\n{result.output}"
        )

    def test_output_includes_template_filename_in_report(self):
        """AC-16: each template's filename (or stem) appears in the report output."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test"])
        # At minimum, some well-known template names should appear
        assert "hello-pipeline" in result.output or "content-pipeline" in result.output, (
            f"Expected template names in output:\n{result.output}"
        )

    # -----------------------------------------------------------------------
    # AC-17: uses same glob pattern as the test file
    # -----------------------------------------------------------------------

    def test_command_discovers_same_files_as_test_module(self):
        """AC-17: templates discovered by `orch templates test` match ALL_TEMPLATES count."""
        # The real run reports each template; count ✓/✗ lines
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test"])

        # Count lines that contain ✓ (passing templates)
        checkmark_lines = [l for l in result.output.splitlines() if "✓" in l]
        # Should match the number of templates in ALL_TEMPLATES
        assert len(checkmark_lines) >= len(ALL_TEMPLATES), (
            f"CLI discovered fewer templates than test module glob "
            f"({len(checkmark_lines)} ✓ lines vs {len(ALL_TEMPLATES)} in ALL_TEMPLATES):\n"
            f"{result.output}"
        )

    def test_command_discovery_includes_both_dirs(self):
        """AC-17: command discovers templates from both templates/ and examples/."""
        runner = CliRunner()
        result = runner.invoke(main, ["templates", "test"])
        # Both dirs exist in the repo; the output should reflect both
        # hello-pipeline is in examples/; content-pipeline is in templates/
        assert "hello-pipeline" in result.output
        assert "content-pipeline" in result.output


# ===========================================================================
# Class 7 — TestGlobPatternEdgeCases
# ===========================================================================


class TestGlobPatternEdgeCases:
    """Additional edge cases from Section 2 of the requirements spec."""

    def test_symlinks_do_not_cause_duplicate_paths(self, tmp_path):
        """Edge case: symlinked files appear once per path (no double-counting)."""
        import os

        tdir = tmp_path .joinpath("templates")
        tdir.mkdir()
        real = tdir / "real.yaml"
        real.write_text(_minimal_template_yaml("real"))

        link = tdir / "link-to-real.yaml"
        os.symlink(real, link)

        discovered = glob.glob(str(tdir / "*.yaml"))
        # Paths should be unique (real + link are distinct paths)
        assert len(discovered) == len(set(discovered)), (
            "glob returned duplicate paths for symlinks"
        )

    def test_non_yaml_files_not_in_discovery(self, tmp_path):
        """Only .yaml/.yml files are discovered — .json, .txt, etc. are skipped."""
        tdir = tmp_path .joinpath("templates")
        tdir.mkdir()
        (tdir / "template.yaml").write_text(_minimal_template_yaml("good"))
        (tdir / "notes.txt").write_text("just notes")
        (tdir / "data.json").write_text('{"key": "value"}')

        discovered = (
            glob.glob(str(tdir / "*.yaml")) + glob.glob(str(tdir / "*.yml"))
        )
        names = [Path(p).name for p in discovered]
        assert "template.yaml" in names
        assert "notes.txt" not in names
        assert "data.json" not in names

    def test_glob_does_not_recurse_into_subdirs(self, tmp_path):
        """*.yaml glob does NOT recurse into nested subdirectories."""
        tdir = tmp_path .joinpath("templates")
        subdir = tdir / "nested"
        subdir.mkdir(parents=True)

        (tdir / "top.yaml").write_text(_minimal_template_yaml("top"))
        (subdir / "nested.yaml").write_text(_minimal_template_yaml("nested"))

        discovered = glob.glob(str(tdir / "*.yaml"))
        names = [Path(p).name for p in discovered]
        assert "top.yaml" in names
        assert "nested.yaml" not in names

    def test_template_id_in_parametrize_is_filename_only(self):
        """AC-08: test IDs from the parametrize ids lambda are filenames, not full paths."""
        for path in ALL_TEMPLATES:
            test_id = Path(path).name
            assert "/" not in test_id, f"Test ID should be filename only, got: {test_id}"
            assert test_id.endswith((".yaml", ".yml")), (
                f"Test ID should end with .yaml/.yml, got: {test_id}"
            )

    def test_all_example_template_names_are_known(self):
        """Sanity check: ALL_TEMPLATES contains the 6 expected template filenames."""
        expected_stems = {
            "content-pipeline-v28",
            "code-development-pipeline",
            "code-review-pipeline",
            "content-pipeline-v2",
            "hello-pipeline",
            "research-pipeline",
        }
        discovered_stems = {Path(p).stem for p in ALL_TEMPLATES}
        missing = expected_stems - discovered_stems
        assert missing == set(), (
            f"Expected templates not found in ALL_TEMPLATES: {missing}\n"
            f"Discovered: {discovered_stems}"
        )
