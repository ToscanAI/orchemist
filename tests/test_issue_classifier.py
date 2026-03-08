"""Tests for Issue #5.1.1 — IssueClassifier and IssueClassification.

Covers:
- IssueClassification dataclass fields, defaults, to_dict()
- VALID_CLASSIFICATION_TYPES constant
- CLASSIFICATION_TEMPLATE_MAP constant
- IssueClassifier stub mode (no executor)
- IssueClassifier with mock executor returning valid JSON
- IssueClassifier with mock executor returning JSON embedded in prose
- IssueClassifier with unknown classification_type → defaults to 'feature'
- IssueClassifier with confidence clamping (>1.0, <0.0)
- IssueClassifier executor error → falls back to stub
- IssueClassifier executor returning object with .text attribute
- IssueClassifier prompt content validation
- IssueClassifier body truncation at 3000 chars
- IssueClassifier DB persistence (insert_issue_classification)
- IssueClassifier DB persistence: id set on result after DB call
- Database CRUD: insert_issue_classification returns int
- Database CRUD: get_issue_classification returns latest row
- Database CRUD: list_issue_classifications with and without repo filter
- Database CRUD: update_issue_classification_status returns True on hit
- Database CRUD: update_issue_classification_status returns False on miss
- Module exports via __init__.py

All tests are independent — no shared mutable state, no real LLM calls.
"""

from __future__ import annotations

import json
import tempfile
import os
from dataclasses import fields
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from orchestration_engine.issue_automation import (
    IssueClassification,
    IssueClassifier,
    VALID_CLASSIFICATION_TYPES,
    CLASSIFICATION_TEMPLATE_MAP,
)
from orchestration_engine.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_exec(json_text: str) -> MagicMock:
    """Build a mock executor whose execute() returns *json_text*."""
    mock = MagicMock()
    mock.execute.return_value = json_text
    mock.model = "test-haiku"
    return mock


def _valid_json(
    cls_type: str = "bug",
    confidence: float = 0.90,
    reasoning: str = "Test reasoning.",
) -> str:
    return json.dumps({
        "classification_type": cls_type,
        "confidence": confidence,
        "reasoning": reasoning,
    })


def _make_db() -> Database:
    """Create a fresh in-memory (temp-file) Database for testing."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(tmp.name)
    return db


# ===========================================================================
# TestIssueClassification — dataclass shape
# ===========================================================================


class TestIssueClassification:
    def test_fields_exist(self):
        names = {f.name for f in fields(IssueClassification)}
        required = {
            "issue_number", "repo", "classification_type",
            "confidence", "template_id", "reasoning",
            "status", "id", "run_id", "created_at",
        }
        assert required.issubset(names)

    def test_field_count(self):
        assert len(fields(IssueClassification)) == 10

    def test_defaults(self):
        ic = IssueClassification(
            issue_number=1,
            repo="owner/repo",
            classification_type="bug",
            confidence=0.9,
            template_id="coding-pipeline-v1",
            reasoning="It crashes.",
        )
        assert ic.status == "classified"
        assert ic.id is None
        assert ic.run_id is None
        assert ic.created_at is not None

    def test_to_dict_keys(self):
        ic = IssueClassification(
            issue_number=7,
            repo="owner/repo",
            classification_type="feature",
            confidence=0.75,
            template_id="coding-pipeline-v1",
            reasoning="New capability.",
        )
        d = ic.to_dict()
        assert set(d.keys()) == {
            "id", "issue_number", "repo", "classification_type",
            "confidence", "template_id", "run_id", "status", "created_at",
        }

    def test_to_dict_values(self):
        ic = IssueClassification(
            issue_number=42,
            repo="acme/service",
            classification_type="refactor",
            confidence=0.88,
            template_id="coding-pipeline-v1",
            reasoning="Clean up legacy code.",
            status="launched",
            run_id="run-abc",
        )
        d = ic.to_dict()
        assert d["issue_number"] == 42
        assert d["repo"] == "acme/service"
        assert d["classification_type"] == "refactor"
        assert d["confidence"] == pytest.approx(0.88)
        assert d["template_id"] == "coding-pipeline-v1"
        assert d["run_id"] == "run-abc"
        assert d["status"] == "launched"


# ===========================================================================
# TestConstants
# ===========================================================================


class TestConstants:
    def test_valid_classification_types(self):
        assert VALID_CLASSIFICATION_TYPES == frozenset(
            {"bug", "feature", "docs", "refactor", "research", "content"}
        )

    def test_template_map_covers_all_types(self):
        for cls_type in VALID_CLASSIFICATION_TYPES:
            assert cls_type in CLASSIFICATION_TEMPLATE_MAP, (
                f"CLASSIFICATION_TEMPLATE_MAP missing key: {cls_type!r}"
            )

    def test_template_map_values_are_strings(self):
        for k, v in CLASSIFICATION_TEMPLATE_MAP.items():
            assert isinstance(v, str), f"template_id for {k!r} should be str"


# ===========================================================================
# TestIssueClassifier — stub mode
# ===========================================================================


class TestIssueClassifierStubMode:
    def test_stub_returns_feature(self):
        clf = IssueClassifier()
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.classification_type == IssueClassifier._STUB_CLASSIFICATION

    def test_stub_confidence_is_zero(self):
        clf = IssueClassifier()
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.confidence == IssueClassifier._STUB_CONFIDENCE

    def test_stub_id_is_none_without_db(self):
        clf = IssueClassifier()
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.id is None

    def test_stub_result_has_template_id(self):
        clf = IssueClassifier()
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.template_id in CLASSIFICATION_TEMPLATE_MAP.values()

    def test_stub_result_fields(self):
        clf = IssueClassifier()
        result = clf.classify(issue_number=5, repo="myorg/myrepo", title="Hello")
        assert result.issue_number == 5
        assert result.repo == "myorg/myrepo"
        assert result.status == "classified"


# ===========================================================================
# TestIssueClassifier — with mock executor
# ===========================================================================


class TestIssueClassifierWithExecutor:
    def test_basic_bug_classification(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("bug", 0.95)))
        result = clf.classify(issue_number=10, repo="o/r", title="Crash on start")
        assert result.classification_type == "bug"
        assert result.confidence == pytest.approx(0.95)
        assert result.template_id == CLASSIFICATION_TEMPLATE_MAP["bug"]

    def test_feature_classification(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("feature", 0.80)))
        result = clf.classify(issue_number=20, repo="o/r", title="Add dark mode")
        assert result.classification_type == "feature"
        assert result.template_id == CLASSIFICATION_TEMPLATE_MAP["feature"]

    def test_docs_classification(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("docs", 0.70)))
        result = clf.classify(issue_number=30, repo="o/r", title="Update README")
        assert result.classification_type == "docs"
        assert result.template_id == CLASSIFICATION_TEMPLATE_MAP["docs"]

    def test_refactor_classification(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("refactor", 0.85)))
        result = clf.classify(issue_number=40, repo="o/r", title="Refactor router")
        assert result.classification_type == "refactor"

    def test_research_classification(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("research", 0.60)))
        result = clf.classify(issue_number=50, repo="o/r", title="Spike: LLM routing")
        assert result.classification_type == "research"
        assert result.template_id == CLASSIFICATION_TEMPLATE_MAP["research"]

    def test_content_classification(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("content", 0.88)))
        result = clf.classify(issue_number=60, repo="o/r", title="Write blog post")
        assert result.classification_type == "content"

    def test_reasoning_captured(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json("bug", 0.9, "A crash on empty input.")))
        result = clf.classify(issue_number=1, repo="o/r", title="Crash")
        assert result.reasoning == "A crash on empty input."

    def test_issue_number_and_repo_passed_through(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json()))
        result = clf.classify(issue_number=99, repo="acme/service", title="Test")
        assert result.issue_number == 99
        assert result.repo == "acme/service"

    def test_executor_called_with_prompt(self):
        mock = _mock_exec(_valid_json())
        clf = IssueClassifier(executor=mock)
        clf.classify(issue_number=1, repo="o/r", title="My Issue", body="Details here")
        mock.execute.assert_called_once()
        prompt_arg = mock.execute.call_args[0][0]
        assert "My Issue" in prompt_arg
        assert "Details here" in prompt_arg

    def test_labels_included_in_prompt(self):
        mock = _mock_exec(_valid_json())
        clf = IssueClassifier(executor=mock)
        clf.classify(
            issue_number=1, repo="o/r", title="Bug",
            labels=["critical", "backend"],
        )
        prompt_arg = mock.execute.call_args[0][0]
        assert "critical" in prompt_arg
        assert "backend" in prompt_arg


# ===========================================================================
# TestIssueClassifier — parse edge cases
# ===========================================================================


class TestIssueClassifierParseEdgeCases:
    def test_json_embedded_in_prose(self):
        """LLM adds prose before JSON — should still parse correctly."""
        prose_output = (
            'Here is my classification:\n'
            '{"classification_type": "bug", "confidence": 0.92, "reasoning": "It crashes."}'
        )
        clf = IssueClassifier(executor=_mock_exec(prose_output))
        result = clf.classify(issue_number=1, repo="o/r", title="Crash")
        assert result.classification_type == "bug"
        assert result.confidence == pytest.approx(0.92)

    def test_unknown_classification_type_defaults_to_feature(self):
        unknown_json = json.dumps({
            "classification_type": "unknown_type",
            "confidence": 0.5,
            "reasoning": "Unclear.",
        })
        clf = IssueClassifier(executor=_mock_exec(unknown_json))
        result = clf.classify(issue_number=1, repo="o/r", title="Mystery")
        assert result.classification_type == "feature"

    def test_confidence_clamped_above_one(self):
        high_confidence = json.dumps({
            "classification_type": "bug",
            "confidence": 1.5,
            "reasoning": "Very sure.",
        })
        clf = IssueClassifier(executor=_mock_exec(high_confidence))
        result = clf.classify(issue_number=1, repo="o/r", title="Bug")
        assert result.confidence == pytest.approx(1.0)

    def test_confidence_clamped_below_zero(self):
        negative_confidence = json.dumps({
            "classification_type": "feature",
            "confidence": -0.3,
            "reasoning": "Not sure.",
        })
        clf = IssueClassifier(executor=_mock_exec(negative_confidence))
        result = clf.classify(issue_number=1, repo="o/r", title="Feature")
        assert result.confidence == pytest.approx(0.0)

    def test_unparseable_output_falls_back_to_stub(self):
        clf = IssueClassifier(executor=_mock_exec("This is not JSON at all!"))
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.classification_type == IssueClassifier._STUB_CLASSIFICATION
        assert result.confidence == IssueClassifier._STUB_CONFIDENCE

    def test_executor_error_falls_back_to_stub(self):
        mock = MagicMock()
        mock.execute.side_effect = RuntimeError("LLM connection failed")
        clf = IssueClassifier(executor=mock)
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.classification_type == IssueClassifier._STUB_CLASSIFICATION
        assert result.confidence == IssueClassifier._STUB_CONFIDENCE

    def test_executor_returning_text_attribute(self):
        """Executor returning an object with .text attribute (e.g. ExecutorResult)."""
        mock = MagicMock()
        obj_with_text = MagicMock()
        obj_with_text.text = _valid_json("research", 0.65)
        mock.execute.return_value = obj_with_text
        clf = IssueClassifier(executor=mock)
        result = clf.classify(issue_number=1, repo="o/r", title="Spike")
        assert result.classification_type == "research"
        assert result.confidence == pytest.approx(0.65)

    def test_body_truncated_to_3000_chars(self):
        # Use a unique marker unlikely to appear in the prompt template
        marker = "BODYMARKER"
        long_body = marker * 500  # 10 chars * 500 = 5000 chars
        mock = _mock_exec(_valid_json())
        clf = IssueClassifier(executor=mock)
        clf.classify(issue_number=1, repo="o/r", title="Test", body=long_body)
        prompt_arg = mock.execute.call_args[0][0]
        # Count occurrences of the marker — after truncation at 3000 chars,
        # at most 300 full markers can appear (3000 / 10).
        marker_count = prompt_arg.count(marker)
        assert marker_count <= 300

    def test_empty_labels_defaults_to_none_string(self):
        mock = _mock_exec(_valid_json())
        clf = IssueClassifier(executor=mock)
        clf.classify(issue_number=1, repo="o/r", title="Test", labels=[])
        prompt_arg = mock.execute.call_args[0][0]
        assert "(none)" in prompt_arg

    def test_reasoning_capped_at_200_chars(self):
        long_reasoning = "R" * 300
        json_text = json.dumps({
            "classification_type": "bug",
            "confidence": 0.9,
            "reasoning": long_reasoning,
        })
        clf = IssueClassifier(executor=_mock_exec(json_text))
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert len(result.reasoning) <= 200


# ===========================================================================
# TestIssueClassifierWithDB — DB integration
# ===========================================================================


class TestIssueClassifierWithDB:
    def test_classify_with_db_sets_id(self):
        db = _make_db()
        clf = IssueClassifier(executor=_mock_exec(_valid_json("bug", 0.9)))
        result = clf.classify(
            issue_number=100, repo="owner/repo", title="Crash",
            db=db,
        )
        assert result.id is not None
        assert isinstance(result.id, int)
        assert result.id > 0

    def test_classify_with_db_persists_row(self):
        db = _make_db()
        clf = IssueClassifier(executor=_mock_exec(_valid_json("feature", 0.75)))
        clf.classify(
            issue_number=200, repo="owner/repo", title="New feature",
            db=db,
        )
        row = db.get_issue_classification(200, "owner/repo")
        assert row is not None
        assert row["classification_type"] == "feature"
        assert row["confidence"] == pytest.approx(0.75)

    def test_classify_without_db_id_is_none(self):
        clf = IssueClassifier(executor=_mock_exec(_valid_json()))
        result = clf.classify(issue_number=1, repo="o/r", title="Test")
        assert result.id is None


# ===========================================================================
# TestDatabaseCRUD — issue_pipeline_map
# ===========================================================================


class TestDatabaseCRUD:
    def _sample_data(self, issue_number: int = 1, repo: str = "o/r") -> Dict:
        return {
            "issue_number": issue_number,
            "repo": repo,
            "classification_type": "bug",
            "confidence": 0.88,
            "template_id": "coding-pipeline-v1",
            "run_id": None,
            "status": "classified",
            "created_at": None,
        }

    def test_insert_returns_positive_int(self):
        db = _make_db()
        row_id = db.insert_issue_classification(self._sample_data())
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_insert_multiple_increments_id(self):
        db = _make_db()
        id1 = db.insert_issue_classification(self._sample_data(1))
        id2 = db.insert_issue_classification(self._sample_data(2))
        assert id2 > id1

    def test_get_issue_classification_returns_latest(self):
        db = _make_db()
        db.insert_issue_classification(self._sample_data(42))
        data2 = self._sample_data(42)
        data2["classification_type"] = "feature"
        db.insert_issue_classification(data2)

        row = db.get_issue_classification(42, "o/r")
        assert row is not None
        # Should return the most-recently inserted row (feature)
        assert row["classification_type"] == "feature"

    def test_get_issue_classification_returns_none_when_missing(self):
        db = _make_db()
        assert db.get_issue_classification(9999, "nobody/norepo") is None

    def test_list_issue_classifications_no_filter(self):
        db = _make_db()
        db.insert_issue_classification(self._sample_data(1, "a/b"))
        db.insert_issue_classification(self._sample_data(2, "c/d"))
        rows = db.list_issue_classifications()
        assert len(rows) == 2

    def test_list_issue_classifications_with_repo_filter(self):
        db = _make_db()
        db.insert_issue_classification(self._sample_data(1, "a/b"))
        db.insert_issue_classification(self._sample_data(2, "c/d"))
        rows = db.list_issue_classifications(repo="a/b")
        assert len(rows) == 1
        assert rows[0]["repo"] == "a/b"

    def test_list_issue_classifications_empty(self):
        db = _make_db()
        assert db.list_issue_classifications() == []

    def test_list_issue_classifications_respects_limit(self):
        db = _make_db()
        for i in range(10):
            db.insert_issue_classification(self._sample_data(i))
        rows = db.list_issue_classifications(limit=5)
        assert len(rows) == 5

    def test_list_issue_classifications_ordered_newest_first(self):
        db = _make_db()
        id1 = db.insert_issue_classification(self._sample_data(1))
        id2 = db.insert_issue_classification(self._sample_data(2))
        rows = db.list_issue_classifications()
        assert rows[0]["id"] == id2
        assert rows[1]["id"] == id1

    def test_update_status_returns_true_on_hit(self):
        db = _make_db()
        row_id = db.insert_issue_classification(self._sample_data())
        updated = db.update_issue_classification_status(row_id, "launched")
        assert updated is True

    def test_update_status_persists_change(self):
        db = _make_db()
        row_id = db.insert_issue_classification(self._sample_data(77))
        db.update_issue_classification_status(row_id, "skipped")
        row = db.get_issue_classification(77, "o/r")
        assert row["status"] == "skipped"

    def test_update_status_returns_false_on_miss(self):
        db = _make_db()
        updated = db.update_issue_classification_status(99999, "launched")
        assert updated is False

    def test_classification_type_persisted_correctly(self):
        db = _make_db()
        data = self._sample_data(5)
        data["classification_type"] = "research"
        db.insert_issue_classification(data)
        row = db.get_issue_classification(5, "o/r")
        assert row["classification_type"] == "research"

    def test_confidence_persisted_correctly(self):
        db = _make_db()
        data = self._sample_data(6)
        data["confidence"] = 0.42
        db.insert_issue_classification(data)
        row = db.get_issue_classification(6, "o/r")
        assert row["confidence"] == pytest.approx(0.42)


# ===========================================================================
# TestModuleExports — __init__.py
# ===========================================================================


class TestModuleExports:
    def test_issue_classifier_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "IssueClassifier")

    def test_issue_classification_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "IssueClassification")

    def test_valid_classification_types_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "VALID_CLASSIFICATION_TYPES")

    def test_classification_template_map_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "CLASSIFICATION_TEMPLATE_MAP")

    def test_all_contains_classifier(self):
        import orchestration_engine
        assert "IssueClassifier" in orchestration_engine.__all__

    def test_all_contains_classification(self):
        import orchestration_engine
        assert "IssueClassification" in orchestration_engine.__all__

    def test_direct_import(self):
        from orchestration_engine import IssueClassifier, IssueClassification
        assert IssueClassifier is not None
        assert IssueClassification is not None
