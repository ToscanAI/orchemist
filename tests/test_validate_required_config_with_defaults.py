"""Regression tests for #676 — validate-order bug in _validate_required_config.

`_validate_required_config` (cli.py) runs BEFORE `apply_config_schema_defaults`
at both CLI entry points. A field that is BOTH listed in ``required`` AND has a
``config_schema.properties[field].default`` was wrongly reported missing on the
original (pre-default-fill) input, even though the default would have satisfied
it moments later.

The fix excludes required fields that carry a default from the missing-list.
A field that is genuinely required with NO default still errors.
"""

from __future__ import annotations

import pytest

from orchestration_engine.cli import _validate_required_config


class MockTemplate:
    """Minimal template mock for validation tests (mirrors test_cli_input_validation)."""

    def __init__(self, config_schema=None):
        self.config_schema = config_schema


class TestValidateRequiredConfigWithDefaults:
    def test_required_field_with_default_is_not_reported_missing(self):
        """A required field that has a default is satisfiable → not reported."""
        template = MockTemplate(config_schema={
            "required": ["lang"],
            "properties": {"lang": {"default": "typescript"}},
        })
        missing = _validate_required_config(template, {})
        assert missing == []

    def test_required_field_without_default_is_still_reported_missing(self):
        """A truly-required field (no default) absent from input still errors."""
        template = MockTemplate(config_schema={
            "required": ["repo_url"],
            "properties": {"repo_url": {"type": "string"}},
        })
        missing = _validate_required_config(template, {})
        assert missing == ["repo_url"]

    def test_mixed_required_list(self):
        """Only the no-default required field is reported; the defaulted one is not."""
        template = MockTemplate(config_schema={
            "required": ["a", "b"],
            "properties": {
                "a": {"default": "x"},
                "b": {"type": "string"},
            },
        })
        missing = _validate_required_config(template, {})
        assert missing == ["b"]

    def test_present_field_with_default_is_not_reported(self):
        """A supplied required+default field is obviously not reported."""
        template = MockTemplate(config_schema={
            "required": ["lang"],
            "properties": {"lang": {"default": "typescript"}},
        })
        missing = _validate_required_config(template, {"lang": "python"})
        assert missing == []

    def test_required_field_with_no_properties_entry_still_reported(self):
        """Required field absent from properties (no default possible) still errors."""
        template = MockTemplate(config_schema={
            "required": ["mystery"],
            "properties": {},
        })
        missing = _validate_required_config(template, {})
        assert missing == ["mystery"]
