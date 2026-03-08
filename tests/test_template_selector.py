"""Tests for Issue #5.1.2 — TemplateSelector and InputExtractor.

Covers:
- DEFAULT_TEMPLATE_MAPPING constant (keys, values, types)
- TemplateSelector default construction and mapping
- TemplateSelector with custom mapping
- TemplateSelector fallback for unknown types
- TemplateSelector custom fallback value
- TemplateSelector does not mutate DEFAULT_TEMPLATE_MAPPING
- TemplateSelector.select returns correct template for all 6 types
- InputExtractor stub mode (no executor) returns empty dict
- InputExtractor with mock executor returning valid JSON
- InputExtractor with mock executor returning JSON embedded in prose
- InputExtractor with executor returning object with .text attribute
- InputExtractor executor error falls back to empty dict
- InputExtractor body truncated at 3000 chars
- InputExtractor prompt contains schema, title, and body
- InputExtractor returns empty dict on unparseable LLM output
- InputExtractor returns empty dict when JSON is not a dict (e.g. list)
- Module exports via __init__.py

All tests are independent — no shared mutable state, no real LLM calls.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from orchestration_engine.issue_automation import (
    DEFAULT_TEMPLATE_MAPPING,
    InputExtractor,
    TemplateSelector,
    VALID_CLASSIFICATION_TYPES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_exec(json_text: str) -> MagicMock:
    """Build a mock executor whose execute() returns *json_text*."""
    mock = MagicMock()
    mock.execute.return_value = json_text
    mock.model = "test-haiku"
    return mock


def _simple_schema() -> Dict[str, Any]:
    """A minimal config schema used in InputExtractor tests."""
    return {
        "issue_number": "int",
        "repo": "str",
        "title": "str",
    }


def _valid_extract_json(
    issue_number: int = 42,
    repo: str = "owner/repo",
    title: str = "Fix crash",
) -> str:
    return json.dumps({
        "issue_number": issue_number,
        "repo": repo,
        "title": title,
    })


# ===========================================================================
# TestDefaultTemplateMapping — constant shape
# ===========================================================================


class TestDefaultTemplateMapping:
    def test_is_dict(self):
        assert isinstance(DEFAULT_TEMPLATE_MAPPING, dict)

    def test_covers_all_valid_types(self):
        for cls_type in VALID_CLASSIFICATION_TYPES:
            assert cls_type in DEFAULT_TEMPLATE_MAPPING, (
                f"DEFAULT_TEMPLATE_MAPPING missing key: {cls_type!r}"
            )

    def test_values_are_strings(self):
        for k, v in DEFAULT_TEMPLATE_MAPPING.items():
            assert isinstance(v, str), f"value for {k!r} should be str"

    def test_bug_maps_to_coding_pipeline(self):
        assert DEFAULT_TEMPLATE_MAPPING["bug"] == "coding-pipeline"

    def test_feature_maps_to_coding_pipeline(self):
        assert DEFAULT_TEMPLATE_MAPPING["feature"] == "coding-pipeline"

    def test_refactor_maps_to_coding_pipeline(self):
        assert DEFAULT_TEMPLATE_MAPPING["refactor"] == "coding-pipeline"

    def test_docs_maps_to_content_pipeline(self):
        assert DEFAULT_TEMPLATE_MAPPING["docs"] == "content-pipeline"

    def test_content_maps_to_content_pipeline(self):
        assert DEFAULT_TEMPLATE_MAPPING["content"] == "content-pipeline"

    def test_research_maps_to_research_pipeline(self):
        assert DEFAULT_TEMPLATE_MAPPING["research"] == "research-pipeline"


# ===========================================================================
# TestTemplateSelectorDefault — default mapping behaviour
# ===========================================================================


class TestTemplateSelectorDefault:
    def test_select_bug(self):
        selector = TemplateSelector()
        assert selector.select("bug") == "coding-pipeline"

    def test_select_feature(self):
        selector = TemplateSelector()
        assert selector.select("feature") == "coding-pipeline"

    def test_select_refactor(self):
        selector = TemplateSelector()
        assert selector.select("refactor") == "coding-pipeline"

    def test_select_docs(self):
        selector = TemplateSelector()
        assert selector.select("docs") == "content-pipeline"

    def test_select_content(self):
        selector = TemplateSelector()
        assert selector.select("content") == "content-pipeline"

    def test_select_research(self):
        selector = TemplateSelector()
        assert selector.select("research") == "research-pipeline"

    def test_all_valid_types_return_string(self):
        selector = TemplateSelector()
        for cls_type in VALID_CLASSIFICATION_TYPES:
            result = selector.select(cls_type)
            assert isinstance(result, str), (
                f"select({cls_type!r}) should return str, got {type(result)}"
            )

    def test_unknown_type_returns_fallback(self):
        selector = TemplateSelector()
        assert selector.select("unknown_type") == "coding-pipeline"

    def test_empty_string_returns_fallback(self):
        selector = TemplateSelector()
        assert selector.select("") == "coding-pipeline"


# ===========================================================================
# TestTemplateSelectorCustomMapping — dependency injection
# ===========================================================================


class TestTemplateSelectorCustomMapping:
    def test_custom_mapping_used(self):
        custom = {"bug": "my-bug-pipeline", "feature": "my-feature-pipeline"}
        selector = TemplateSelector(mapping=custom)
        assert selector.select("bug") == "my-bug-pipeline"
        assert selector.select("feature") == "my-feature-pipeline"

    def test_custom_mapping_fallback_for_missing_key(self):
        custom = {"bug": "my-bug-pipeline"}
        selector = TemplateSelector(mapping=custom)
        # "feature" not in custom mapping → fallback
        assert selector.select("feature") == "coding-pipeline"

    def test_custom_fallback_value(self):
        selector = TemplateSelector(
            mapping={"bug": "my-pipeline"},
            fallback="default-pipeline",
        )
        assert selector.select("research") == "default-pipeline"

    def test_custom_mapping_does_not_mutate_default(self):
        original_bug = DEFAULT_TEMPLATE_MAPPING["bug"]
        custom = {"bug": "mutant-pipeline"}
        TemplateSelector(mapping=custom)
        # DEFAULT_TEMPLATE_MAPPING must remain unchanged
        assert DEFAULT_TEMPLATE_MAPPING["bug"] == original_bug

    def test_none_mapping_uses_default(self):
        selector = TemplateSelector(mapping=None)
        assert selector.select("docs") == DEFAULT_TEMPLATE_MAPPING["docs"]

    def test_empty_mapping_uses_fallback_for_all(self):
        selector = TemplateSelector(mapping={}, fallback="fallback-pipeline")
        for cls_type in VALID_CLASSIFICATION_TYPES:
            assert selector.select(cls_type) == "fallback-pipeline"

    def test_two_selectors_are_independent(self):
        s1 = TemplateSelector(mapping={"bug": "pipeline-a"})
        s2 = TemplateSelector(mapping={"bug": "pipeline-b"})
        assert s1.select("bug") == "pipeline-a"
        assert s2.select("bug") == "pipeline-b"


# ===========================================================================
# TestInputExtractorStubMode — no executor
# ===========================================================================


class TestInputExtractorStubMode:
    def test_stub_returns_empty_dict(self):
        extractor = InputExtractor()
        result = extractor.extract(
            issue_title="Fix crash",
            issue_body="Something is broken.",
            config_schema=_simple_schema(),
        )
        assert result == {}

    def test_stub_returns_dict_type(self):
        extractor = InputExtractor()
        result = extractor.extract(
            issue_title="Test",
            issue_body="",
            config_schema={},
        )
        assert isinstance(result, dict)

    def test_stub_does_not_call_executor(self):
        extractor = InputExtractor(executor=None)
        # No executor set — should not raise
        result = extractor.extract(
            issue_title="Test",
            issue_body="Details.",
            config_schema={"field": "str"},
        )
        assert result == {}


# ===========================================================================
# TestInputExtractorWithExecutor — with mock executor
# ===========================================================================


class TestInputExtractorWithExecutor:
    def test_basic_extraction(self):
        json_out = _valid_extract_json(42, "owner/repo", "Fix crash")
        extractor = InputExtractor(executor=_mock_exec(json_out))
        result = extractor.extract(
            issue_title="Fix crash",
            issue_body="The runner crashes on empty input.",
            config_schema=_simple_schema(),
        )
        assert result["issue_number"] == 42
        assert result["repo"] == "owner/repo"
        assert result["title"] == "Fix crash"

    def test_returns_dict(self):
        extractor = InputExtractor(executor=_mock_exec(_valid_extract_json()))
        result = extractor.extract(
            issue_title="Test",
            issue_body="",
            config_schema=_simple_schema(),
        )
        assert isinstance(result, dict)

    def test_executor_called_once(self):
        mock = _mock_exec(_valid_extract_json())
        extractor = InputExtractor(executor=mock)
        extractor.extract("Title", "Body", _simple_schema())
        mock.execute.assert_called_once()

    def test_prompt_contains_title(self):
        mock = _mock_exec(_valid_extract_json())
        extractor = InputExtractor(executor=mock)
        extractor.extract("Unique Issue Title 12345", "Body", _simple_schema())
        prompt_arg = mock.execute.call_args[0][0]
        assert "Unique Issue Title 12345" in prompt_arg

    def test_prompt_contains_body(self):
        mock = _mock_exec(_valid_extract_json())
        extractor = InputExtractor(executor=mock)
        extractor.extract("Title", "Body content XYZABC", _simple_schema())
        prompt_arg = mock.execute.call_args[0][0]
        assert "Body content XYZABC" in prompt_arg

    def test_prompt_contains_schema(self):
        mock = _mock_exec(_valid_extract_json())
        extractor = InputExtractor(executor=mock)
        schema = {"my_unique_field_abc": "int"}
        extractor.extract("Title", "Body", schema)
        prompt_arg = mock.execute.call_args[0][0]
        assert "my_unique_field_abc" in prompt_arg

    def test_executor_returning_text_attribute(self):
        """Executor returning an object with .text attribute (e.g. ExecutorResult)."""
        mock = MagicMock()
        obj_with_text = MagicMock()
        obj_with_text.text = _valid_extract_json(7, "a/b", "Some title")
        mock.execute.return_value = obj_with_text
        extractor = InputExtractor(executor=mock)
        result = extractor.extract("Title", "Body", _simple_schema())
        assert result["issue_number"] == 7
        assert result["repo"] == "a/b"

    def test_json_embedded_in_prose(self):
        """LLM adds prose before JSON — should still parse correctly."""
        prose_output = (
            "Based on the issue, here are the extracted values:\n"
            '{"issue_number": 99, "repo": "org/svc", "title": "Title"}'
        )
        extractor = InputExtractor(executor=_mock_exec(prose_output))
        result = extractor.extract("Title", "Body", _simple_schema())
        assert result.get("issue_number") == 99

    def test_executor_error_returns_empty_dict(self):
        mock = MagicMock()
        mock.execute.side_effect = RuntimeError("LLM connection failed")
        extractor = InputExtractor(executor=mock)
        result = extractor.extract("Title", "Body", _simple_schema())
        assert result == {}

    def test_unparseable_output_returns_empty_dict(self):
        extractor = InputExtractor(executor=_mock_exec("This is not JSON at all!"))
        result = extractor.extract("Title", "Body", _simple_schema())
        assert result == {}

    def test_json_array_output_returns_empty_dict(self):
        """LLM returns a JSON array instead of object — should return empty dict."""
        extractor = InputExtractor(executor=_mock_exec('[1, 2, 3]'))
        result = extractor.extract("Title", "Body", _simple_schema())
        assert result == {}

    def test_body_truncated_to_3000_chars(self):
        marker = "BODYMARKER"
        long_body = marker * 500  # 5000 chars
        mock = _mock_exec(_valid_extract_json())
        extractor = InputExtractor(executor=mock)
        extractor.extract("Title", long_body, _simple_schema())
        prompt_arg = mock.execute.call_args[0][0]
        # After 3000-char truncation, at most 300 full markers (3000 / 10)
        marker_count = prompt_arg.count(marker)
        assert marker_count <= 300

    def test_empty_body_handled_gracefully(self):
        extractor = InputExtractor(executor=_mock_exec(_valid_extract_json()))
        result = extractor.extract("Title", "", _simple_schema())
        assert isinstance(result, dict)

    def test_empty_schema_handled_gracefully(self):
        extractor = InputExtractor(executor=_mock_exec("{}"))
        result = extractor.extract("Title", "Body", {})
        assert result == {}

    def test_custom_model_label_stored(self):
        extractor = InputExtractor(executor=None, model="sonnet")
        assert extractor.model == "sonnet"


# ===========================================================================
# TestModuleExports — __init__.py
# ===========================================================================


class TestModuleExports:
    def test_template_selector_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "TemplateSelector")

    def test_input_extractor_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "InputExtractor")

    def test_default_template_mapping_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "DEFAULT_TEMPLATE_MAPPING")

    def test_all_contains_template_selector(self):
        import orchestration_engine
        assert "TemplateSelector" in orchestration_engine.__all__

    def test_all_contains_input_extractor(self):
        import orchestration_engine
        assert "InputExtractor" in orchestration_engine.__all__

    def test_all_contains_default_template_mapping(self):
        import orchestration_engine
        assert "DEFAULT_TEMPLATE_MAPPING" in orchestration_engine.__all__

    def test_direct_import_template_selector(self):
        from orchestration_engine import TemplateSelector
        assert TemplateSelector is not None

    def test_direct_import_input_extractor(self):
        from orchestration_engine import InputExtractor
        assert InputExtractor is not None

    def test_direct_import_default_template_mapping(self):
        from orchestration_engine import DEFAULT_TEMPLATE_MAPPING
        assert isinstance(DEFAULT_TEMPLATE_MAPPING, dict)
