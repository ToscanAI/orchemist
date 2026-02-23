"""Tests for knowledge-work TaskTypes added in issue #123.

Covers:
- Enum values exist and have correct string values
- select_model_tier() returns expected tiers for each new type
- DEFAULT_MAX_RETRIES has entries for all new types
- Regression: existing task types are unchanged
"""

import pytest

from orchestration_engine.schemas import (
    DEFAULT_MAX_RETRIES,
    ModelTier,
    TaskType,
    select_model_tier,
)


# ---------------------------------------------------------------------------
# Enum existence & string values
# ---------------------------------------------------------------------------

class TestKnowledgeTaskTypeEnum:
    """All six new enum members exist and carry the right string value."""

    def test_triage_exists(self):
        assert TaskType.TRIAGE == "triage"

    def test_analysis_exists(self):
        assert TaskType.ANALYSIS == "analysis"

    def test_compliance_exists(self):
        assert TaskType.COMPLIANCE == "compliance"

    def test_financial_exists(self):
        assert TaskType.FINANCIAL == "financial"

    def test_sales_exists(self):
        assert TaskType.SALES == "sales"

    def test_support_exists(self):
        assert TaskType.SUPPORT == "support"

    def test_all_new_types_present(self):
        new_types = {"triage", "analysis", "compliance", "financial", "sales", "support"}
        enum_values = {t.value for t in TaskType}
        assert new_types.issubset(enum_values)

    def test_enum_string_values(self):
        """String values match the member names (lower-cased)."""
        for member in [
            TaskType.TRIAGE,
            TaskType.ANALYSIS,
            TaskType.COMPLIANCE,
            TaskType.FINANCIAL,
            TaskType.SALES,
            TaskType.SUPPORT,
        ]:
            assert member.value == member.name.lower()


# ---------------------------------------------------------------------------
# select_model_tier — first-attempt defaults
# ---------------------------------------------------------------------------

class TestSelectModelTierKnowledgeTypes:
    """select_model_tier() returns the expected tier on attempt 1."""

    def test_triage_returns_sonnet(self):
        assert select_model_tier(TaskType.TRIAGE, 1) == ModelTier.SONNET

    def test_analysis_returns_sonnet(self):
        assert select_model_tier(TaskType.ANALYSIS, 1) == ModelTier.SONNET

    def test_compliance_returns_opus(self):
        assert select_model_tier(TaskType.COMPLIANCE, 1) == ModelTier.OPUS

    def test_financial_returns_opus(self):
        assert select_model_tier(TaskType.FINANCIAL, 1) == ModelTier.OPUS

    def test_sales_returns_sonnet(self):
        assert select_model_tier(TaskType.SALES, 1) == ModelTier.SONNET

    def test_support_returns_haiku(self):
        assert select_model_tier(TaskType.SUPPORT, 1) == ModelTier.HAIKU

    # Escalation on retry
    def test_triage_escalates_on_retry(self):
        assert select_model_tier(TaskType.TRIAGE, 2) == ModelTier.OPUS

    def test_analysis_escalates_on_retry(self):
        assert select_model_tier(TaskType.ANALYSIS, 2) == ModelTier.OPUS

    def test_compliance_stays_opus_on_retry(self):
        assert select_model_tier(TaskType.COMPLIANCE, 2) == ModelTier.OPUS

    def test_financial_stays_opus_on_retry(self):
        assert select_model_tier(TaskType.FINANCIAL, 2) == ModelTier.OPUS

    def test_sales_escalates_on_retry(self):
        assert select_model_tier(TaskType.SALES, 2) == ModelTier.OPUS

    def test_support_escalates_to_sonnet_on_retry(self):
        assert select_model_tier(TaskType.SUPPORT, 2) == ModelTier.SONNET

    def test_support_escalates_to_opus_on_second_retry(self):
        assert select_model_tier(TaskType.SUPPORT, 3) == ModelTier.OPUS


# ---------------------------------------------------------------------------
# DEFAULT_MAX_RETRIES
# ---------------------------------------------------------------------------

class TestDefaultMaxRetriesKnowledgeTypes:
    """DEFAULT_MAX_RETRIES contains sensible entries for all new types."""

    NEW_TYPES = [
        TaskType.TRIAGE,
        TaskType.ANALYSIS,
        TaskType.COMPLIANCE,
        TaskType.FINANCIAL,
        TaskType.SALES,
        TaskType.SUPPORT,
    ]

    def test_all_new_types_have_entry(self):
        for t in self.NEW_TYPES:
            assert t in DEFAULT_MAX_RETRIES, f"{t} missing from DEFAULT_MAX_RETRIES"

    def test_retry_counts_in_range(self):
        for t in self.NEW_TYPES:
            count = DEFAULT_MAX_RETRIES[t]
            assert 2 <= count <= 3, f"{t} retry count {count} not in [2, 3]"

    def test_triage_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.TRIAGE] == 3

    def test_analysis_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.ANALYSIS] == 3

    def test_compliance_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.COMPLIANCE] == 2

    def test_financial_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.FINANCIAL] == 2

    def test_sales_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.SALES] == 3

    def test_support_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.SUPPORT] == 3


# ---------------------------------------------------------------------------
# Regression — existing task types unchanged
# ---------------------------------------------------------------------------

class TestExistingTaskTypesUnchanged:
    """Existing enum values, model tiers, and retry counts are unaffected."""

    def test_content_enum_value(self):
        assert TaskType.CONTENT == "content"

    def test_code_enum_value(self):
        assert TaskType.CODE == "code"

    def test_research_enum_value(self):
        assert TaskType.RESEARCH == "research"

    def test_translation_enum_value(self):
        assert TaskType.TRANSLATION == "translation"

    def test_review_enum_value(self):
        assert TaskType.REVIEW == "review"

    # Model tiers — attempt 1
    def test_content_tier(self):
        assert select_model_tier(TaskType.CONTENT, 1) == ModelTier.HAIKU

    def test_code_tier(self):
        assert select_model_tier(TaskType.CODE, 1) == ModelTier.SONNET

    def test_research_tier(self):
        assert select_model_tier(TaskType.RESEARCH, 1) == ModelTier.HAIKU

    def test_translation_tier(self):
        assert select_model_tier(TaskType.TRANSLATION, 1) == ModelTier.SONNET

    def test_review_tier(self):
        assert select_model_tier(TaskType.REVIEW, 1) == ModelTier.SONNET

    # Retry counts
    def test_content_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.CONTENT] == 3

    def test_code_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.CODE] == 2

    def test_research_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.RESEARCH] == 3

    def test_translation_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.TRANSLATION] == 4

    def test_review_retries(self):
        assert DEFAULT_MAX_RETRIES[TaskType.REVIEW] == 2
