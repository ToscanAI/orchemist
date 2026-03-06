"""Tests for the routing config engine (Issue #331.2).

Covers:
- RoutingTier dataclass validation and matches()
- DEFAULT_ROUTING_CONFIG: all 4 tiers matched correctly
- Boundary score mapping
- Custom tier configs
- RoutingDecision content
- RoutingEngine.validate_thresholds(): gaps, overlaps, valid configs, coverage warnings
- _parse_routing_config(): None, non-dict, valid dict
- PipelineTemplate integration: no config, with config, gap/overlap causes validation error
"""

from __future__ import annotations

import logging
import textwrap
import tempfile
from pathlib import Path

import pytest
import yaml

from src.orchestration_engine.routing import (
    RoutingConfig,
    RoutingDecision,
    RoutingEngine,
    RoutingTier,
    DEFAULT_ROUTING_CONFIG,
    _parse_routing_config,
)
from src.orchestration_engine.confidence import ConfidenceResult, ConfidenceLevel
from src.orchestration_engine.templates import PipelineTemplate, TemplateEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(score: float) -> ConfidenceResult:
    """Return a minimal ConfidenceResult with the given composite_score."""
    return ConfidenceResult(
        composite_score=score,
        confidence_level=ConfidenceLevel.LOW,
        explanation="test",
    )


def _engine_default() -> RoutingEngine:
    return RoutingEngine()


# ---------------------------------------------------------------------------
# RoutingTier unit tests
# ---------------------------------------------------------------------------


class TestRoutingTier:
    def test_basic_construction(self):
        tier = RoutingTier(name="t", min_score=0.5, max_score=0.8)
        assert tier.name == "t"
        assert tier.min_score == 0.5
        assert tier.max_score == 0.8

    def test_defaults(self):
        tier = RoutingTier(name="x", min_score=0.0, max_score=1.0)
        assert tier.requires == []
        assert tier.notify == []
        assert tier.strategy == ""
        assert tier.max_retries == 0

    def test_matches_within_range(self):
        tier = RoutingTier(name="t", min_score=0.5, max_score=0.8)
        assert tier.matches(0.5)
        assert tier.matches(0.65)
        assert tier.matches(0.8)

    def test_matches_outside_range(self):
        tier = RoutingTier(name="t", min_score=0.5, max_score=0.8)
        assert not tier.matches(0.4999)
        assert not tier.matches(0.8001)
        assert not tier.matches(0.0)
        assert not tier.matches(1.0)

    def test_invalid_min_score_below_zero(self):
        with pytest.raises(ValueError, match="min_score must be in"):
            RoutingTier(name="t", min_score=-0.1, max_score=0.5)

    def test_invalid_max_score_above_one(self):
        with pytest.raises(ValueError, match="max_score must be in"):
            RoutingTier(name="t", min_score=0.5, max_score=1.1)

    def test_min_greater_than_max(self):
        with pytest.raises(ValueError, match="min_score.*must be.*max_score"):
            RoutingTier(name="t", min_score=0.8, max_score=0.5)

    def test_strategy_stored(self):
        tier = RoutingTier(name="t", min_score=0.0, max_score=1.0, strategy="merge")
        assert tier.strategy == "merge"

    def test_max_retries_stored(self):
        tier = RoutingTier(name="t", min_score=0.0, max_score=1.0, max_retries=3)
        assert tier.max_retries == 3

    def test_requires_and_notify_stored(self):
        tier = RoutingTier(
            name="t",
            min_score=0.0,
            max_score=1.0,
            requires=["approve"],
            notify=["slack:team"],
        )
        assert tier.requires == ["approve"]
        assert tier.notify == ["slack:team"]


# ---------------------------------------------------------------------------
# DEFAULT_ROUTING_CONFIG: standard tier lookup
# ---------------------------------------------------------------------------


class TestDefaultRoutingConfig:
    """Verify the four default tiers are configured correctly."""

    def test_default_has_four_tiers(self):
        assert len(DEFAULT_ROUTING_CONFIG.tiers) == 4

    def test_auto_merge_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "auto_merge")
        assert tier.min_score == 0.90
        assert tier.max_score == 1.00
        assert tier.strategy == "merge"

    def test_human_review_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "human_review")
        assert tier.min_score == 0.75
        assert tier.max_score == 0.90
        assert tier.strategy == "queue_review"

    def test_auto_retry_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "auto_retry")
        assert tier.min_score == 0.50
        assert tier.max_score == 0.75
        assert tier.strategy == "retry"
        assert tier.max_retries == 3

    def test_reject_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "reject")
        assert tier.min_score == 0.00
        assert tier.max_score == 0.50
        assert tier.strategy == "reject"


# ---------------------------------------------------------------------------
# RoutingEngine.evaluate(): standard scores
# ---------------------------------------------------------------------------


class TestRoutingEngineEvaluateStandard:
    """Test the four main confidence bands."""

    def test_095_auto_merge(self):
        decision = _engine_default().evaluate(_make_result(0.95))
        assert decision.tier is not None
        assert decision.tier.name == "auto_merge"
        assert decision.action == "merge"

    def test_080_human_review(self):
        decision = _engine_default().evaluate(_make_result(0.80))
        assert decision.tier.name == "human_review"
        assert decision.action == "queue_review"

    def test_060_auto_retry(self):
        decision = _engine_default().evaluate(_make_result(0.60))
        assert decision.tier.name == "auto_retry"
        assert decision.action == "retry"

    def test_020_reject(self):
        decision = _engine_default().evaluate(_make_result(0.20))
        assert decision.tier.name == "reject"
        assert decision.action == "reject"


# ---------------------------------------------------------------------------
# RoutingEngine.evaluate(): boundary scores
# ---------------------------------------------------------------------------


class TestRoutingEngineEvaluateBoundaries:
    """Exact boundary values — the spec calls these out explicitly."""

    def test_090_auto_merge(self):
        """Score of exactly 0.90 → auto_merge (highest-priority tier wins)."""
        decision = _engine_default().evaluate(_make_result(0.90))
        assert decision.tier.name == "auto_merge"

    def test_08999_human_review(self):
        """Score just below 0.90 → human_review."""
        decision = _engine_default().evaluate(_make_result(0.8999))
        assert decision.tier.name == "human_review"

    def test_075_human_review(self):
        """Score of exactly 0.75 → human_review (ties go to higher tier)."""
        decision = _engine_default().evaluate(_make_result(0.75))
        assert decision.tier.name == "human_review"

    def test_07499_auto_retry(self):
        """Score just below 0.75 → auto_retry."""
        decision = _engine_default().evaluate(_make_result(0.7499))
        assert decision.tier.name == "auto_retry"

    def test_050_auto_retry(self):
        """Score of exactly 0.50 → auto_retry."""
        decision = _engine_default().evaluate(_make_result(0.50))
        assert decision.tier.name == "auto_retry"

    def test_04999_reject(self):
        """Score just below 0.50 → reject."""
        decision = _engine_default().evaluate(_make_result(0.4999))
        assert decision.tier.name == "reject"

    def test_100_auto_merge(self):
        """Maximum score → auto_merge."""
        decision = _engine_default().evaluate(_make_result(1.0))
        assert decision.tier.name == "auto_merge"

    def test_000_reject(self):
        """Minimum score → reject."""
        decision = _engine_default().evaluate(_make_result(0.0))
        assert decision.tier.name == "reject"


# ---------------------------------------------------------------------------
# RoutingDecision content
# ---------------------------------------------------------------------------


class TestRoutingDecisionContent:
    """Verify action matches tier.strategy and explanation is non-empty."""

    @pytest.mark.parametrize("score,expected_strategy,expected_tier_name", [
        (0.95, "merge", "auto_merge"),
        (0.80, "queue_review", "human_review"),
        (0.60, "retry", "auto_retry"),
        (0.20, "reject", "reject"),
    ])
    def test_action_matches_strategy(self, score, expected_strategy, expected_tier_name):
        decision = _engine_default().evaluate(_make_result(score))
        assert decision.action == expected_strategy
        assert decision.tier.name == expected_tier_name

    @pytest.mark.parametrize("score", [0.95, 0.80, 0.60, 0.20, 0.0, 1.0])
    def test_explanation_non_empty(self, score):
        decision = _engine_default().evaluate(_make_result(score))
        assert decision.explanation
        assert len(decision.explanation) > 0

    def test_explanation_contains_score(self):
        decision = _engine_default().evaluate(_make_result(0.95))
        assert "0.9500" in decision.explanation

    def test_explanation_contains_tier_name(self):
        decision = _engine_default().evaluate(_make_result(0.95))
        assert "auto_merge" in decision.explanation


# ---------------------------------------------------------------------------
# Custom tier configs
# ---------------------------------------------------------------------------


class TestCustomTierConfig:
    def test_single_tier_full_range(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="all", min_score=0.0, max_score=1.0, strategy="pass"),
        ])
        engine = RoutingEngine(config)
        for score in [0.0, 0.5, 1.0]:
            decision = engine.evaluate(_make_result(score))
            assert decision.tier.name == "all"
            assert decision.action == "pass"

    def test_two_tier_config(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="high", min_score=0.7, max_score=1.0, strategy="approve"),
            RoutingTier(name="low", min_score=0.0, max_score=0.7, strategy="deny"),
        ])
        engine = RoutingEngine(config)
        assert engine.evaluate(_make_result(0.9)).tier.name == "high"
        assert engine.evaluate(_make_result(0.3)).tier.name == "low"
        # Boundary — 0.7 matches "high" (higher min_score wins)
        assert engine.evaluate(_make_result(0.7)).tier.name == "high"

    def test_none_config_uses_default(self):
        engine = RoutingEngine(None)
        decision = engine.evaluate(_make_result(0.95))
        assert decision.tier.name == "auto_merge"

    def test_no_matching_tier_returns_reject_action(self):
        # Config that only covers 0.8-1.0; a score of 0.5 won't match
        config = RoutingConfig(tiers=[
            RoutingTier(name="top", min_score=0.8, max_score=1.0, strategy="merge"),
        ])
        engine = RoutingEngine(config)
        decision = engine.evaluate(_make_result(0.5))
        assert decision.tier is None
        assert decision.action == "reject"
        assert decision.explanation  # non-empty


# ---------------------------------------------------------------------------
# validate_thresholds()
# ---------------------------------------------------------------------------


class TestValidateThresholds:
    def test_default_config_no_errors(self):
        engine = RoutingEngine()
        errors = engine.validate_thresholds()
        assert errors == []

    def test_gap_detected(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="high", min_score=0.8, max_score=1.0, strategy="merge"),
            RoutingTier(name="low", min_score=0.0, max_score=0.6, strategy="reject"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert len(errors) == 1
        assert "Gap" in errors[0]
        assert "low" in errors[0]
        assert "high" in errors[0]

    def test_overlap_detected(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="high", min_score=0.6, max_score=1.0, strategy="merge"),
            RoutingTier(name="low", min_score=0.0, max_score=0.8, strategy="reject"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert len(errors) == 1
        assert "Overlap" in errors[0]

    def test_valid_contiguous_tiers_no_errors(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="a", min_score=0.5, max_score=1.0, strategy="a"),
            RoutingTier(name="b", min_score=0.0, max_score=0.5, strategy="b"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert errors == []

    def test_empty_config_no_errors(self):
        engine = RoutingEngine(RoutingConfig(tiers=[]))
        errors = engine.validate_thresholds()
        assert errors == []

    def test_single_tier_no_errors(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="x", min_score=0.0, max_score=1.0, strategy="pass"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert errors == []

    def test_incomplete_coverage_logs_warning_not_error(self, caplog):
        """Coverage that doesn't span [0, 1] should WARN, not error."""
        config = RoutingConfig(tiers=[
            RoutingTier(name="mid", min_score=0.3, max_score=0.8, strategy="review"),
        ])
        engine = RoutingEngine(config)
        with caplog.at_level(logging.WARNING, logger="src.orchestration_engine.routing"):
            errors = engine.validate_thresholds()
        # No errors — only warnings
        assert errors == []
        # Warnings emitted
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("0.0" in w or "start" in w.lower() for w in warnings)

    def test_multiple_gaps(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="a", min_score=0.8, max_score=1.0, strategy="a"),
            RoutingTier(name="b", min_score=0.4, max_score=0.5, strategy="b"),
            RoutingTier(name="c", min_score=0.0, max_score=0.2, strategy="c"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert len(errors) == 2
        assert all("Gap" in e for e in errors)


# ---------------------------------------------------------------------------
# _parse_routing_config()
# ---------------------------------------------------------------------------


class TestParseRoutingConfig:
    def test_none_returns_none(self):
        assert _parse_routing_config(None) is None

    def test_non_dict_string_returns_none(self):
        assert _parse_routing_config("not a dict") is None

    def test_non_dict_int_returns_none(self):
        assert _parse_routing_config(42) is None

    def test_non_dict_list_returns_none(self):
        assert _parse_routing_config([{"name": "x"}]) is None

    def test_valid_dict_parses_correctly(self):
        raw = {
            "tiers": [
                {
                    "name": "top",
                    "min_score": 0.8,
                    "max_score": 1.0,
                    "strategy": "merge",
                },
                {
                    "name": "bottom",
                    "min_score": 0.0,
                    "max_score": 0.8,
                    "strategy": "reject",
                    "max_retries": 2,
                    "requires": ["approve"],
                    "notify": ["slack:ops"],
                },
            ]
        }
        config = _parse_routing_config(raw)
        assert config is not None
        assert isinstance(config, RoutingConfig)
        assert len(config.tiers) == 2
        top = next(t for t in config.tiers if t.name == "top")
        bottom = next(t for t in config.tiers if t.name == "bottom")
        assert top.min_score == 0.8
        assert top.strategy == "merge"
        assert bottom.max_retries == 2
        assert bottom.requires == ["approve"]
        assert bottom.notify == ["slack:ops"]

    def test_missing_tiers_key_returns_none(self):
        assert _parse_routing_config({"not_tiers": []}) is None

    def test_empty_tiers_list_returns_empty_config(self):
        config = _parse_routing_config({"tiers": []})
        assert config is not None
        assert config.tiers == []

    def test_tiers_not_list_returns_none(self):
        assert _parse_routing_config({"tiers": "oops"}) is None


# ---------------------------------------------------------------------------
# PipelineTemplate integration
# ---------------------------------------------------------------------------


def _make_minimal_template_yaml(extra: str = "") -> str:
    """Return a minimal valid pipeline YAML with an optional extra block.

    The base YAML and extra block are concatenated *after* each is dedented
    independently, so indentation isn't corrupted by f-string embedding.
    """
    base = textwrap.dedent("""
        id: test-pipeline
        name: Test Pipeline
        phases:
          - id: phase1
            name: Phase 1
            prompt_template: "Do something with {input}"
    """).strip()
    if extra:
        extra_clean = textwrap.dedent(extra).strip()
        return base + "\n" + extra_clean
    return base


@pytest.fixture
def templates_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def engine(templates_dir):
    return TemplateEngine(templates_dir=templates_dir)


class TestTemplateIntegration:
    def test_template_without_routing_config_has_none(self, templates_dir, engine):
        path = templates_dir / "no_routing.yaml"
        path.write_text(_make_minimal_template_yaml())
        template = engine.load_template(path)
        assert template.routing_config is None

    def test_template_with_routing_config_parses(self, templates_dir, engine):
        routing_block = textwrap.dedent("""
            routing_config:
              tiers:
                - name: auto_merge
                  min_score: 0.80
                  max_score: 1.00
                  strategy: merge
                - name: reject
                  min_score: 0.00
                  max_score: 0.80
                  strategy: reject
        """)
        path = templates_dir / "with_routing.yaml"
        path.write_text(_make_minimal_template_yaml(routing_block))
        template = engine.load_template(path)
        assert template.routing_config is not None
        assert len(template.routing_config.tiers) == 2
        names = {t.name for t in template.routing_config.tiers}
        assert names == {"auto_merge", "reject"}

    def test_valid_routing_config_no_validation_errors(self, templates_dir, engine):
        routing_block = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.70
                  max_score: 1.00
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.70
                  strategy: reject
        """)
        path = templates_dir / "valid_routing.yaml"
        path.write_text(_make_minimal_template_yaml(routing_block))
        template = engine.load_template(path)
        errors = engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert routing_errors == []

    def test_routing_config_with_gap_causes_validation_error(self, templates_dir, engine):
        routing_block = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.80
                  max_score: 1.00
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.60
                  strategy: reject
        """)
        path = templates_dir / "gap_routing.yaml"
        path.write_text(_make_minimal_template_yaml(routing_block))
        template = engine.load_template(path)
        errors = engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert len(routing_errors) >= 1
        assert any("Gap" in e for e in routing_errors)

    def test_routing_config_with_overlap_causes_validation_error(self, templates_dir, engine):
        routing_block = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.50
                  max_score: 1.00
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.70
                  strategy: reject
        """)
        path = templates_dir / "overlap_routing.yaml"
        path.write_text(_make_minimal_template_yaml(routing_block))
        template = engine.load_template(path)
        errors = engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert len(routing_errors) >= 1
        assert any("Overlap" in e for e in routing_errors)

    def test_no_routing_config_no_extra_errors(self, templates_dir, engine):
        """A template without routing_config should not produce routing errors."""
        path = templates_dir / "no_routing2.yaml"
        path.write_text(_make_minimal_template_yaml())
        template = engine.load_template(path)
        errors = engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert routing_errors == []


# ---------------------------------------------------------------------------
# __init__ exports
# ---------------------------------------------------------------------------


class TestInitExports:
    def test_exports_accessible(self):
        import orchestration_engine as oe
        assert hasattr(oe, "RoutingTier")
        assert hasattr(oe, "RoutingConfig")
        assert hasattr(oe, "RoutingDecision")
        assert hasattr(oe, "RoutingEngine")
        assert hasattr(oe, "DEFAULT_ROUTING_CONFIG")

    def test_default_routing_config_exported(self):
        from orchestration_engine import DEFAULT_ROUTING_CONFIG as drc
        assert drc is not None
        assert len(drc.tiers) == 4
