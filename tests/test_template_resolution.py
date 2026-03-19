"""Tests for template name-based resolution (issue #75).

Tests cover:
- Resolution order: project → user → bundled (first match wins)
- ORCH_TEMPLATES_PATH env var prepends custom paths
- resolve_template raises clear error when template not found
- list_templates returns correct source labels
- Direct path loading still works via load_template(path)
- Same template name in multiple dirs: project wins over user wins over bundled
- Empty directories are skipped gracefully
- Name-based resolution in CLI commands (run, validate, list-phases)
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner

from src.orchestration_engine.templates import (
    TemplateEngine,
    TemplateNotFoundError,
)
from src.orchestration_engine.cli import main


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MINIMAL_YAML = """\
id: test-template
name: Test Template
version: "1.0.0"
description: "Minimal template for testing."
author: "Test Author"
phases:
  - id: greet
    name: Greet
    prompt_template: "Say hello"
"""


@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / "project_templates"
    d.mkdir()
    return d


@pytest.fixture
def user_dir(tmp_path):
    d = tmp_path / "user_templates"
    d.mkdir()
    return d


@pytest.fixture
def bundled_dir(tmp_path):
    d = tmp_path / "bundled_templates"
    d.mkdir()
    return d


@pytest.fixture
def custom_dir(tmp_path):
    d = tmp_path / "custom_templates"
    d.mkdir()
    return d


def make_template(directory: Path, stem: str, content: str = MINIMAL_YAML) -> Path:
    """Write a YAML template file to *directory* and return its path."""
    path = directory / f"{stem}.yaml"
    path.write_text(content)
    return path


def engine_with_dirs(project_dir, user_dir, bundled_dir=None):
    """Create a TemplateEngine pointing at the given tmp dirs."""
    e = TemplateEngine(project_dir=project_dir, user_dir=user_dir)
    if bundled_dir is not None:
        e._bundled_dir = bundled_dir
    return e


# ---------------------------------------------------------------------------
# 1. Resolution order: project → user → bundled
# ---------------------------------------------------------------------------

class TestResolutionOrder:
    def test_project_wins_over_user(self, project_dir, user_dir, bundled_dir):
        make_template(project_dir, "my-pipe")
        make_template(user_dir, "my-pipe")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("my-pipe")
        assert resolved.parent == project_dir.resolve()

    def test_user_wins_over_bundled(self, project_dir, user_dir, bundled_dir):
        make_template(user_dir, "my-pipe")
        make_template(bundled_dir, "my-pipe")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("my-pipe")
        assert resolved.parent == user_dir.resolve()

    def test_bundled_found_when_no_others(self, project_dir, user_dir, bundled_dir):
        make_template(bundled_dir, "my-pipe")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("my-pipe")
        assert resolved.parent == bundled_dir.resolve()

    def test_project_wins_all_three(self, project_dir, user_dir, bundled_dir):
        make_template(project_dir, "shared")
        make_template(user_dir, "shared")
        make_template(bundled_dir, "shared")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("shared")
        assert resolved.parent == project_dir.resolve()


# ---------------------------------------------------------------------------
# 2. ORCH_TEMPLATES_PATH prepends custom paths
# ---------------------------------------------------------------------------

class TestOrcTemplatesPath:
    def test_custom_path_wins_over_project(self, custom_dir, project_dir, user_dir, bundled_dir, monkeypatch):
        make_template(custom_dir, "alpha")
        make_template(project_dir, "alpha")
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", str(custom_dir))
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("alpha")
        assert resolved.parent == custom_dir.resolve()

    def test_multiple_custom_paths_colon_separated(self, tmp_path, project_dir, user_dir, bundled_dir, monkeypatch):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        make_template(second, "beta")
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", f"{first}:{second}")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("beta")
        assert resolved.parent == second.resolve()

    def test_env_var_not_set_uses_normal_order(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        make_template(project_dir, "gamma")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("gamma")
        assert resolved.parent == project_dir.resolve()

    def test_get_search_paths_includes_custom(self, project_dir, user_dir, monkeypatch):
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", "/some/custom/path")
        e = engine_with_dirs(project_dir, user_dir)
        paths = e.get_search_paths()
        labels = [label for _, label in paths]
        assert labels[0] == "custom"

    def test_get_search_paths_order_without_env(self, project_dir, user_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        e = engine_with_dirs(project_dir, user_dir)
        paths = e.get_search_paths()
        labels = [label for _, label in paths]
        assert labels == ["project", "user", "bundled"]


# ---------------------------------------------------------------------------
# 3. resolve_template raises clear error when not found
# ---------------------------------------------------------------------------

class TestResolveTemplateNotFound:
    def test_raises_template_not_found_error(self, project_dir, user_dir, bundled_dir):
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(TemplateNotFoundError) as exc_info:
            e.resolve_template("nonexistent-template")
        assert "nonexistent-template" in str(exc_info.value)

    def test_error_message_lists_searched_paths(self, project_dir, user_dir, bundled_dir):
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(TemplateNotFoundError) as exc_info:
            e.resolve_template("missing")
        msg = str(exc_info.value)
        assert "missing" in msg
        assert "Searched" in msg

    def test_error_is_filenotfounderror_subclass(self, project_dir, user_dir, bundled_dir):
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(FileNotFoundError):
            e.resolve_template("nope")

    def test_resolve_with_yaml_extension_still_works(self, project_dir, user_dir, bundled_dir):
        make_template(project_dir, "mypipe")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        # Should strip extension and find it
        resolved = e.resolve_template("mypipe.yaml")
        assert resolved.stem == "mypipe"


# ---------------------------------------------------------------------------
# 4. list_templates shows correct source labels
# ---------------------------------------------------------------------------

class TestListTemplates:
    def test_source_labels_project(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        make_template(project_dir, "proj-tmpl")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        proj = next((r for r in results if r["id"] == "test-template"), None)
        assert proj is not None
        assert proj["source"] == "project"

    def test_source_labels_user(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        make_template(user_dir, "user-tmpl")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        user = next((r for r in results if r["id"] == "test-template"), None)
        assert user is not None
        assert user["source"] == "user"

    def test_source_labels_bundled(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        make_template(bundled_dir, "bundled-tmpl")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        bundled = next((r for r in results if r["id"] == "test-template"), None)
        assert bundled is not None
        assert bundled["source"] == "bundled"

    def test_source_labels_custom_env(self, custom_dir, project_dir, user_dir, bundled_dir, monkeypatch):
        make_template(custom_dir, "custom-tmpl")
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", str(custom_dir))
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        custom = next((r for r in results if r["id"] == "test-template"), None)
        assert custom is not None
        assert custom["source"] == "custom"

    def test_list_returns_expected_keys(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        make_template(project_dir, "full-tmpl")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        assert results, "Expected at least one template"
        item = results[0]
        for key in ("name", "id", "version", "phases", "description", "source", "path"):
            assert key in item, f"Missing key: {key}"

    def test_first_wins_in_list(self, project_dir, user_dir, bundled_dir, monkeypatch):
        """When the same stem exists in both project and user, list only shows project entry."""
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        make_template(project_dir, "dupe")
        make_template(user_dir, "dupe")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        dupes = [r for r in results if r["path"].endswith("dupe.yaml")]
        assert len(dupes) == 1
        assert dupes[0]["source"] == "project"


# ---------------------------------------------------------------------------
# 5. Direct path loading via load_template(path) still works
# ---------------------------------------------------------------------------

class TestDirectPathLoading:
    def test_load_template_by_path(self, tmp_path):
        tpl = make_template(tmp_path, "direct")
        e = TemplateEngine()
        result = e.load_template(tpl)
        assert result.id == "test-template"
        assert result.name == "Test Template"
        assert len(result.phases) == 1

    def test_load_template_raises_on_missing_file(self, tmp_path):
        e = TemplateEngine()
        with pytest.raises(FileNotFoundError):
            e.load_template(tmp_path / "nonexistent.yaml")

    def test_load_template_absolute_path(self, tmp_path):
        tpl = make_template(tmp_path, "abs")
        e = TemplateEngine()
        result = e.load_template(tpl.resolve())
        assert result.id == "test-template"


# ---------------------------------------------------------------------------
# 6. Same name in multiple dirs: project > user > bundled
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    def test_project_over_user_over_bundled(self, project_dir, user_dir, bundled_dir):
        # Write unique ids to distinguish which file was loaded
        (project_dir / "winner.yaml").write_text(
            "id: from-project\nname: From Project\nversion: '1.0'\nphases: []\n"
        )
        (user_dir / "winner.yaml").write_text(
            "id: from-user\nname: From User\nversion: '1.0'\nphases: []\n"
        )
        (bundled_dir / "winner.yaml").write_text(
            "id: from-bundled\nname: From Bundled\nversion: '1.0'\nphases: []\n"
        )
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        path = e.resolve_template("winner")
        tpl = e.load_template(path)
        assert tpl.id == "from-project"

    def test_user_over_bundled_when_no_project(self, project_dir, user_dir, bundled_dir):
        (user_dir / "second.yaml").write_text(
            "id: from-user\nname: From User\nversion: '1.0'\nphases: []\n"
        )
        (bundled_dir / "second.yaml").write_text(
            "id: from-bundled\nname: From Bundled\nversion: '1.0'\nphases: []\n"
        )
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        path = e.resolve_template("second")
        tpl = e.load_template(path)
        assert tpl.id == "from-user"


# ---------------------------------------------------------------------------
# 7. Empty directories are skipped gracefully
# ---------------------------------------------------------------------------

class TestEmptyDirectories:
    def test_resolve_skips_empty_project_dir(self, project_dir, user_dir, bundled_dir):
        # project_dir is empty; template only in bundled
        make_template(bundled_dir, "only-bundled")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("only-bundled")
        assert resolved.exists()

    def test_resolve_skips_nonexistent_dirs(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        ghost_project = tmp_path / "does_not_exist_project"
        ghost_user = tmp_path / "does_not_exist_user"
        ghost_bundled = tmp_path / "does_not_exist_bundled"
        e = engine_with_dirs(ghost_project, ghost_user, ghost_bundled)
        with pytest.raises(TemplateNotFoundError):
            e.resolve_template("any")

    def test_list_templates_returns_empty_when_all_dirs_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        ghost_project = tmp_path / "no_project"
        ghost_user = tmp_path / "no_user"
        ghost_bundled = tmp_path / "no_bundled"
        e = engine_with_dirs(ghost_project, ghost_user, ghost_bundled)
        results = e.list_templates()
        assert results == []

    def test_list_templates_skips_empty_existing_dirs(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # All dirs exist but are empty
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        assert results == []


# ---------------------------------------------------------------------------
# 8. Name-based resolution in CLI commands (run, validate, list-phases)
# ---------------------------------------------------------------------------

class TestCLINameResolution:
    """Test that CLI commands accept template names, not just paths."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def _write_template(self, directory: Path, stem: str = "mytemplate") -> Path:
        return make_template(directory, stem)

    def test_validate_by_name(self, runner, tmp_path, monkeypatch):
        tmpl_dir = tmp_path .joinpath("templates")
        tmpl_dir.mkdir()
        self._write_template(tmpl_dir, "mytemplate")
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", str(tmpl_dir))
        result = runner.invoke(main, ["validate", "mytemplate"])
        assert result.exit_code == 0, result.output

    def test_validate_by_path(self, runner, tmp_path):
        tpl = make_template(tmp_path, "direct")
        result = runner.invoke(main, ["validate", str(tpl)])
        assert result.exit_code == 0, result.output

    def test_validate_error_on_missing_name(self, runner, tmp_path, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # Point engine at empty dirs so nothing is found
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", str(tmp_path / "empty_custom"))
        result = runner.invoke(main, ["validate", "totally-nonexistent-template-xyz"])
        assert result.exit_code != 0

    def test_list_phases_by_name(self, runner, tmp_path, monkeypatch):
        tmpl_dir = tmp_path .joinpath("templates")
        tmpl_dir.mkdir()
        self._write_template(tmpl_dir, "mypipe")
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", str(tmpl_dir))
        result = runner.invoke(main, ["list-phases", "mypipe"])
        assert result.exit_code == 0, result.output

    def test_list_phases_by_path(self, runner, tmp_path):
        tpl = make_template(tmp_path, "phases-test")
        result = runner.invoke(main, ["list-phases", str(tpl)])
        assert result.exit_code == 0, result.output

    def test_run_dry_run_by_name(self, runner, tmp_path, monkeypatch):
        tmpl_dir = tmp_path .joinpath("templates")
        tmpl_dir.mkdir()
        self._write_template(tmpl_dir, "dryrun-tpl")
        monkeypatch.setenv("ORCH_TEMPLATES_PATH", str(tmpl_dir))
        result = runner.invoke(main, [
            "run", "dryrun-tpl",
            "--mode", "dry-run",
        ])
        # dry-run should complete without error (exit 0)
        assert result.exit_code == 0, result.output

    def test_run_error_on_missing_name(self, runner, tmp_path, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        result = runner.invoke(main, [
            "run", "totally-nonexistent-xyz",
            "--mode", "dry-run",
        ])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 9. Additional edge-case tests (review feedback #75)
# ---------------------------------------------------------------------------

class TestYmlExtension:
    """.yml extension resolution and .yaml vs .yml precedence."""

    def test_yml_extension_resolves(self, project_dir, user_dir, bundled_dir, monkeypatch):
        """.yml template files are discovered and resolvable by stem."""
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # Write a .yml (not .yaml) file
        yml_path = project_dir / "my-workflow.yml"
        yml_path.write_text(MINIMAL_YAML)
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("my-workflow")
        assert resolved.exists()
        assert resolved.suffix == ".yml"

    def test_yml_listed_in_list_templates(self, project_dir, user_dir, bundled_dir, monkeypatch):
        """.yml templates appear in list_templates() results."""
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        (project_dir / "yml-only.yml").write_text(MINIMAL_YAML)
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        assert any(r["path"].endswith(".yml") for r in results)

    def test_yaml_wins_over_yml_same_dir(self, project_dir, user_dir, bundled_dir, monkeypatch):
        """.yaml takes precedence over .yml when both exist in the same directory."""
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # Write both .yaml and .yml with distinct ids so we can tell them apart
        (project_dir / "competing.yaml").write_text(
            "id: from-yaml\nname: From YAML\nversion: '1.0'\nphases: []\n"
        )
        (project_dir / "competing.yml").write_text(
            "id: from-yml\nname: From YML\nversion: '1.0'\nphases: []\n"
        )
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        resolved = e.resolve_template("competing")
        tpl = e.load_template(resolved)
        # .yaml should be found first (glob("*.yaml") runs before glob("*.yml"))
        assert tpl.id == "from-yaml", f"Expected from-yaml but got {tpl.id}"


class TestPathTraversalRejection:
    """resolve_template() must reject names containing path separators or '..'."""

    def test_dotdot_raises_value_error(self, project_dir, user_dir, bundled_dir):
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(ValueError, match="path separators"):
            e.resolve_template("../../etc/passwd")

    def test_forward_slash_raises_value_error(self, project_dir, user_dir, bundled_dir):
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(ValueError):
            e.resolve_template("subdir/template")

    def test_backslash_raises_value_error(self, project_dir, user_dir, bundled_dir):
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(ValueError):
            e.resolve_template("subdir\\template")

    def test_safe_name_is_not_rejected(self, project_dir, user_dir, bundled_dir):
        """A normal template name must not raise ValueError (even if not found)."""
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        with pytest.raises(Exception) as exc_info:
            e.resolve_template("safe-template-name")
        # Must raise TemplateNotFoundError, NOT ValueError
        from src.orchestration_engine.templates import TemplateNotFoundError
        assert isinstance(exc_info.value, TemplateNotFoundError)


class TestMalformedYamlSkipped:
    """list_templates() must skip malformed YAML files gracefully, not crash."""

    def test_malformed_yaml_skipped_in_list(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # Write a valid template alongside a broken one
        make_template(project_dir, "valid-template")
        (project_dir / "broken.yaml").write_text("id: [\nthis is: not: valid: yaml\n")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        # Should NOT raise; broken file silently skipped
        results = e.list_templates()
        ids = [r["id"] for r in results]
        assert "test-template" in ids, "Valid template should still be listed"
        # broken.yaml has no valid id, so it won't appear
        assert all(r["id"] != "broken" for r in results)

    def test_empty_yaml_skipped_in_list(self, project_dir, user_dir, bundled_dir, monkeypatch):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # An empty file raises ValueError in load_template — must be skipped
        (project_dir / "empty.yaml").write_text("")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        assert all(r.get("id") != "empty" for r in results)

    def test_malformed_yaml_does_not_prevent_valid_templates(
        self, project_dir, user_dir, bundled_dir, monkeypatch
    ):
        monkeypatch.delenv("ORCH_TEMPLATES_PATH", raising=False)
        # Mix: broken first (alphabetically), valid second
        (project_dir / "aaa-broken.yaml").write_text(": this is not valid yaml :\n")
        make_template(project_dir, "zzz-valid")
        e = engine_with_dirs(project_dir, user_dir, bundled_dir)
        results = e.list_templates()
        # The valid template must still appear despite the broken one
        assert any(r["path"].endswith("zzz-valid.yaml") for r in results)
