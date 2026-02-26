"""Tests for template field alias mapping and unknown field warnings.

Covers the postmortem fixes from 2026-02-26:
- prompt → prompt_template alias
- model → model_tier alias
- Both-present conflict resolution
- Unknown field warning
- Empty prompt_template validation
"""

import logging
from pathlib import Path

import pytest
import yaml

from orchestration_engine.templates import TemplateEngine


@pytest.fixture
def manager():
    return TemplateEngine()


def _make_template_yaml(phases, **kwargs):
    """Build a minimal valid template YAML dict."""
    base = {
        "id": "test-aliases",
        "name": "Test Aliases",
        "description": "Test template",
        "author": "test",
        "version": "1.0.0",
        "phases": phases,
    }
    base.update(kwargs)
    return base


def _write_yaml(tmp_path, data, filename="test.yaml"):
    p = tmp_path / filename
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


class TestAliasMapping:
    """Test that prompt → prompt_template and model → model_tier work."""

    def test_prompt_alias_resolves(self, tmp_path, manager):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt": "Do the thing",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        assert template.phases[0].prompt_template == "Do the thing"

    def test_model_alias_resolves(self, tmp_path, manager):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "Do the thing",
            "model": "opus",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        assert template.phases[0].model_tier == "opus"

    def test_both_aliases_together(self, tmp_path, manager):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt": "My prompt",
            "model": "haiku",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        assert template.phases[0].prompt_template == "My prompt"
        assert template.phases[0].model_tier == "haiku"

    def test_canonical_names_still_work(self, tmp_path, manager):
        """Existing templates using prompt_template/model_tier must not break."""
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "Canonical prompt",
            "model_tier": "sonnet",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        assert template.phases[0].prompt_template == "Canonical prompt"
        assert template.phases[0].model_tier == "sonnet"


class TestBothPresentConflict:
    """Test that canonical field wins when both alias and canonical are present."""

    def test_prompt_template_wins_over_prompt(self, tmp_path, manager, caplog):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt": "I should be ignored",
            "prompt_template": "I should win",
        }])
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING):
            template = manager.load_template(path)
        assert template.phases[0].prompt_template == "I should win"
        assert "both 'prompt' and 'prompt_template' present" in caplog.text

    def test_model_tier_wins_over_model(self, tmp_path, manager, caplog):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "Do it",
            "model": "haiku",
            "model_tier": "opus",
        }])
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING):
            template = manager.load_template(path)
        assert template.phases[0].model_tier == "opus"
        assert "both 'model' and 'model_tier' present" in caplog.text


class TestUnknownFieldWarning:
    """Test that unknown fields produce warnings."""

    def test_unknown_field_warns(self, tmp_path, manager, caplog):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "Do it",
            "temperature": 0.5,
            "banana": "yellow",
        }])
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING):
            template = manager.load_template(path)
        assert "unknown fields dropped" in caplog.text
        assert "temperature" in caplog.text or "banana" in caplog.text

    def test_known_fields_no_warning(self, tmp_path, manager, caplog):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "Do it",
            "model_tier": "sonnet",
            "thinking_level": "low",
        }])
        path = _write_yaml(tmp_path, data)
        with caplog.at_level(logging.WARNING):
            manager.load_template(path)
        assert "unknown fields dropped" not in caplog.text


class TestEmptyPromptValidation:
    """Test that validate_template catches empty prompts."""

    def test_empty_prompt_flagged(self, tmp_path, manager):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        errors = manager.validate_template(template)
        assert any("empty prompt_template" in e for e in errors)

    def test_whitespace_only_prompt_flagged(self, tmp_path, manager):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "   \n  ",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        errors = manager.validate_template(template)
        assert any("empty prompt_template" in e for e in errors)

    def test_missing_prompt_flagged(self, tmp_path, manager):
        """Phase with no prompt at all (neither alias nor canonical)."""
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        errors = manager.validate_template(template)
        assert any("empty prompt_template" in e for e in errors)

    def test_valid_prompt_no_error(self, tmp_path, manager):
        data = _make_template_yaml([{
            "id": "phase1",
            "name": "Phase 1",
            "prompt_template": "Research {config.topic}",
        }])
        path = _write_yaml(tmp_path, data)
        template = manager.load_template(path)
        errors = manager.validate_template(template)
        assert not any("empty prompt_template" in e for e in errors)
