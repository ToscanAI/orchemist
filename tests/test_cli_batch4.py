"""Tests for CLI Batch 4: orch templates install / uninstall (#69)."""

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main


@pytest.fixture
def runner():
    return CliRunner(mix_stderr=False)


@pytest.fixture
def tmp_orch_home(tmp_path):
    """Override ~/.orch/templates/ to a temp directory."""
    templates_dir = tmp_path / ".orch" / "templates"
    templates_dir.mkdir(parents=True)
    return templates_dir


@pytest.fixture
def sample_yaml(tmp_path):
    """Create a minimal valid pipeline YAML file."""
    yaml_content = """\
id: test-pipeline
name: Test Pipeline
version: "1.0.0"
description: A test pipeline
phases:
  - id: phase1
    name: Phase One
    prompt_template: "Do something with {input}"
    model_tier: sonnet
    thinking_level: low
"""
    filepath = tmp_path / "test-pipeline.yaml"
    filepath.write_text(yaml_content)
    return filepath


# ---------------------------------------------------------------------------
# orch templates install — local file
# ---------------------------------------------------------------------------

class TestTemplatesInstallLocal:
    """Test installing from local .yaml files."""

    def test_install_local_yaml(self, runner, sample_yaml, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            result = runner.invoke(main, [
                "templates", "install", str(sample_yaml),
            ])
        assert result.exit_code == 0
        assert "Installed" in result.output or "Installing" in result.output
        # Check file was copied
        installed = tmp_orch_home / "test-pipeline"
        assert installed.exists()
        yamls = list(installed.glob("*.yaml"))
        assert len(yamls) == 1

    def test_install_local_with_custom_name(self, runner, sample_yaml, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            result = runner.invoke(main, [
                "templates", "install", str(sample_yaml), "--name", "my-custom",
            ])
        assert result.exit_code == 0
        installed = tmp_orch_home / "my-custom"
        assert installed.exists()

    def test_install_local_refuses_overwrite(self, runner, sample_yaml, tmp_orch_home):
        # Install once
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            runner.invoke(main, ["templates", "install", str(sample_yaml)])
            # Install again without --force
            result = runner.invoke(main, ["templates", "install", str(sample_yaml)])
        assert result.exit_code != 0
        assert "already installed" in result.output or "already installed" in (result.stderr or "")

    def test_install_local_force_overwrite(self, runner, sample_yaml, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            runner.invoke(main, ["templates", "install", str(sample_yaml)])
            result = runner.invoke(main, [
                "templates", "install", str(sample_yaml), "--force",
            ])
        assert result.exit_code == 0

    def test_install_nonexistent_file(self, runner, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            result = runner.invoke(main, [
                "templates", "install", "/nonexistent/file.yaml",
            ])
        assert result.exit_code != 0
        assert "not found" in (result.output + (result.stderr or "")).lower()


# ---------------------------------------------------------------------------
# orch templates install — GitHub shorthand
# ---------------------------------------------------------------------------

class TestTemplatesInstallGitHub:
    """Test installing from GitHub shorthand and URLs."""

    def test_github_shorthand_calls_git_clone(self, runner, tmp_orch_home, tmp_path):
        """Verify GitHub shorthand constructs correct URL and calls git."""
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home), \
             patch("orchestration_engine.cli._install_from_git") as mock_git, \
             patch("orchestration_engine.cli._find_yaml_in_dir", return_value=None):
            mock_git.return_value = tmp_orch_home / "my-pipeline"
            (tmp_orch_home / "my-pipeline").mkdir()
            result = runner.invoke(main, [
                "templates", "install", "user/my-pipeline",
            ])
        mock_git.assert_called_once()
        call_args = mock_git.call_args
        assert "github.com/user/my-pipeline" in call_args[0][0]
        assert call_args[0][1] == "my-pipeline"

    def test_git_url_calls_git_clone(self, runner, tmp_orch_home, tmp_path):
        """Verify full git URL is passed through."""
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home), \
             patch("orchestration_engine.cli._install_from_git") as mock_git, \
             patch("orchestration_engine.cli._find_yaml_in_dir", return_value=None):
            mock_git.return_value = tmp_orch_home / "repo"
            (tmp_orch_home / "repo").mkdir()
            result = runner.invoke(main, [
                "templates", "install", "https://github.com/user/repo",
            ])
        mock_git.assert_called_once()
        assert mock_git.call_args[0][0] == "https://github.com/user/repo"

    def test_unknown_source_format(self, runner, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            result = runner.invoke(main, [
                "templates", "install", "some-random-name",
            ])
        assert result.exit_code != 0
        assert "Unknown source" in (result.output + (result.stderr or ""))


# ---------------------------------------------------------------------------
# orch templates uninstall
# ---------------------------------------------------------------------------

class TestTemplatesUninstall:
    """Test uninstalling templates."""

    def test_uninstall_existing(self, runner, sample_yaml, tmp_orch_home):
        # Install first
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            runner.invoke(main, ["templates", "install", str(sample_yaml)])
            # Uninstall with --force (skip prompt)
            result = runner.invoke(main, [
                "templates", "uninstall", "test-pipeline", "--force",
            ])
        assert result.exit_code == 0
        assert "uninstalled" in result.output.lower()
        assert not (tmp_orch_home / "test-pipeline").exists()

    def test_uninstall_nonexistent(self, runner, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            result = runner.invoke(main, [
                "templates", "uninstall", "nonexistent", "--force",
            ])
        assert result.exit_code != 0
        assert "not found" in (result.output + (result.stderr or "")).lower()

    def test_uninstall_abort_on_no(self, runner, sample_yaml, tmp_orch_home):
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            runner.invoke(main, ["templates", "install", str(sample_yaml)])
            result = runner.invoke(main, [
                "templates", "uninstall", "test-pipeline",
            ], input="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.output
        # Template should still exist
        assert (tmp_orch_home / "test-pipeline").exists()


# ---------------------------------------------------------------------------
# _is_github_shorthand
# ---------------------------------------------------------------------------

class TestIsGitHubShorthand:
    """Test GitHub shorthand detection."""

    def test_valid_shorthand(self):
        from orchestration_engine.cli import _is_github_shorthand
        assert _is_github_shorthand("user/repo") is True
        assert _is_github_shorthand("my-org/my-pipeline") is True

    def test_url_not_shorthand(self):
        from orchestration_engine.cli import _is_github_shorthand
        assert _is_github_shorthand("https://github.com/user/repo") is False

    def test_yaml_file_not_shorthand(self):
        from orchestration_engine.cli import _is_github_shorthand
        assert _is_github_shorthand("path/to.yaml") is False

    def test_single_name_not_shorthand(self):
        from orchestration_engine.cli import _is_github_shorthand
        assert _is_github_shorthand("my-template") is False

    def test_three_parts_not_shorthand(self):
        from orchestration_engine.cli import _is_github_shorthand
        assert _is_github_shorthand("a/b/c") is False


# ---------------------------------------------------------------------------
# _install_from_git unit tests
# ---------------------------------------------------------------------------

class TestInstallFromGit:
    """Test git clone helper."""

    def test_git_not_installed(self, tmp_orch_home):
        from orchestration_engine.cli import _install_from_git
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home), \
             patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(Exception, match="git is not installed"):
                _install_from_git("https://example.com/repo", "test", False)

    def test_clone_failure(self, tmp_orch_home):
        import subprocess
        from orchestration_engine.cli import _install_from_git
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home), \
             patch("subprocess.run", side_effect=subprocess.CalledProcessError(
                 128, "git", stderr="fatal: repo not found")):
            with pytest.raises(Exception, match="clone failed"):
                _install_from_git("https://example.com/repo", "test", False)

    def test_clone_timeout(self, tmp_orch_home):
        import subprocess
        from orchestration_engine.cli import _install_from_git
        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 60)):
            with pytest.raises(Exception, match="timed out"):
                _install_from_git("https://example.com/repo", "test", False)


# ---------------------------------------------------------------------------
# _find_yaml_in_dir
# ---------------------------------------------------------------------------

class TestFindYamlInDir:
    """Test YAML file discovery in installed directories."""

    def test_finds_root_yaml(self, tmp_path):
        from orchestration_engine.cli import _find_yaml_in_dir
        (tmp_path / "pipeline.yaml").write_text("id: test")
        assert _find_yaml_in_dir(tmp_path) is not None

    def test_finds_in_templates_subdir(self, tmp_path):
        from orchestration_engine.cli import _find_yaml_in_dir
        sub = tmp_path / "templates"
        sub.mkdir()
        (sub / "pipeline.yaml").write_text("id: test")
        assert _find_yaml_in_dir(tmp_path) is not None

    def test_finds_in_examples_subdir(self, tmp_path):
        from orchestration_engine.cli import _find_yaml_in_dir
        sub = tmp_path / "examples"
        sub.mkdir()
        (sub / "pipeline.yml").write_text("id: test")
        assert _find_yaml_in_dir(tmp_path) is not None

    def test_empty_dir_returns_none(self, tmp_path):
        from orchestration_engine.cli import _find_yaml_in_dir
        assert _find_yaml_in_dir(tmp_path) is None

    def test_skips_dotfiles(self, tmp_path):
        from orchestration_engine.cli import _find_yaml_in_dir
        (tmp_path / ".hidden.yaml").write_text("id: test")
        assert _find_yaml_in_dir(tmp_path) is None


# ---------------------------------------------------------------------------
# Validation on install
# ---------------------------------------------------------------------------

class TestInstallValidation:
    """Test that install validates the template."""

    def test_install_invalid_yaml_warns(self, runner, tmp_path, tmp_orch_home):
        """Installing a malformed YAML should fail with validation error."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("not: a: valid: pipeline")

        with patch("orchestration_engine.cli._USER_TEMPLATES_DIR", tmp_orch_home):
            result = runner.invoke(main, [
                "templates", "install", str(bad_yaml),
            ])
        # Should fail validation
        assert result.exit_code != 0
