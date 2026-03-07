"""Tests for CLI input validation against template config_schema (#411)."""

import pytest
from unittest.mock import MagicMock

from orchestration_engine.cli import _validate_required_config


class MockTemplate:
    """Minimal template mock for validation tests."""

    def __init__(self, config_schema=None):
        self.config_schema = config_schema


class TestValidateRequiredConfig:
    """Test _validate_required_config helper."""

    def test_all_present_returns_empty(self):
        template = MockTemplate(config_schema={
            "type": "object",
            "required": ["name", "path"],
        })
        missing = _validate_required_config(template, {"name": "foo", "path": "/bar"})
        assert missing == []

    def test_one_missing_returns_it(self):
        template = MockTemplate(config_schema={
            "type": "object",
            "required": ["name", "path"],
        })
        missing = _validate_required_config(template, {"name": "foo"})
        assert missing == ["path"]

    def test_multiple_missing_returns_all(self):
        template = MockTemplate(config_schema={
            "type": "object",
            "required": ["a", "b", "c"],
        })
        missing = _validate_required_config(template, {"b": "yes"})
        assert missing == ["a", "c"]

    def test_no_schema_returns_empty(self):
        template = MockTemplate(config_schema=None)
        missing = _validate_required_config(template, {})
        assert missing == []

    def test_no_required_field_returns_empty(self):
        template = MockTemplate(config_schema={"type": "object"})
        missing = _validate_required_config(template, {})
        assert missing == []

    def test_empty_required_returns_empty(self):
        template = MockTemplate(config_schema={
            "type": "object",
            "required": [],
        })
        missing = _validate_required_config(template, {})
        assert missing == []

    def test_extra_fields_ignored(self):
        template = MockTemplate(config_schema={
            "type": "object",
            "required": ["name"],
        })
        missing = _validate_required_config(template, {"name": "foo", "extra": "bar"})
        assert missing == []

    def test_no_config_schema_attr(self):
        """Template without config_schema attribute at all."""
        template = MagicMock(spec=[])  # no attributes
        template.config_schema = None
        missing = _validate_required_config(template, {})
        assert missing == []
