"""Tests for Issue #331.2 — Routing Config Schema, Rules Engine, and Template Integration.

Covers:
- RoutingTier dataclass (construction, validation, matches())
- RoutingConfig dataclass
- RoutingDecision dataclass
- RoutingEngine.route() — all four DEFAULT tiers
- RoutingEngine.route() — unrouted fallback
- RoutingEngine.validate_thresholds() — valid config
- RoutingEngine.validate_thresholds() — gaps and overlaps
- DEFAULT_ROUTING_CONFIG tier structure and completeness
- _parse_routing_config() YAML parsing helper
- PipelineTemplate.routing_config field
- Template load_template() persists routing_config
- validate_template() reports routing config errors
- Module exports via __init__.py
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from orchestration_engine.confidence import (
    ConfidenceLevel,
    ConfidenceResult,
)
from orchestration_engine.routing import (
    DEFAULT_ROUTING_CONFIG,
    RoutingConfig,
    RoutingDecision,
    RoutingEngine,
    RoutingTier,
    _parse_routing_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(score: float) -> ConfidenceResult:
    """Build a minimal ConfidenceResult with the given composite_score."""
    if score >= 0.90:
        level = ConfidenceLevel.HIGH
    elif score >= 0.75:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW
    return ConfidenceResult(
        signals=[],
        composite_score=score,
        confidence_level=level,
        explanation="",
    )


def _two_tier_config(gap: bool = False, overlap: bool = False) -> RoutingConfig:
    """Return a two-tier config, optionally with gap or overlap."""
    if gap:
        # [0.00, 0.50) then [0.60, 1.01) — gap [0.50, 0.60)
        return RoutingConfig(tiers=[
            RoutingTier(name="low", min_score=0.00, max_score=0.50, strategy="reject"),
            RoutingTier(name="high", min_score=0.60, max_score=1.01, strategy="merge"),
        ])
    if overlap:
        # [0.00, 0.70) then [0.60, 1.01) — overlap [0.60, 0.70)
        return RoutingConfig(tiers=[
            RoutingTier(name="low", min_score=0.00, max_score=0.70, strategy="reject"),
            RoutingTier(name="high", min_score=0.60, max_score=1.01, strategy="merge"),
        ])
    # Clean two-tier config
    return RoutingConfig(tiers=[
        RoutingTier(name="low", min_score=0.00, max_score=0.75, strategy="retry"),
        RoutingTier(name="high", min_score=0.75, max_score=1.01, strategy="merge"),
    ])


# ---------------------------------------------------------------------------
# RoutingTier
# ---------------------------------------------------------------------------


class TestRoutingTier:
    def test_basic_construction(self):
        tier = RoutingTier(name="auto_merge", min_score=0.90, max_score=1.01)
        assert tier.name == "auto_merge"
        assert tier.min_score == 0.90
        assert tier.max_score == 1.01
        assert tier.strategy == "review"  # default
        assert tier.max_retries == 0

    def test_full_construction(self):
        tier = RoutingTier(
            name="retry",
            min_score=0.60,
            max_score=0.75,
            requires=["ci:pass"],
            notify=["slack:#alerts"],
            strategy="retry",
            max_retries=3,
        )
        assert tier.name == "retry"
        assert tier.strategy == "retry"
        assert tier.max_retries == 3
        assert "ci:pass" in tier.requires
        assert "slack:#alerts" in tier.notify

    def test_matches_inclusive_lower_bound(self):
        tier = RoutingTier(name="t", min_score=0.75, max_score=0.90)
        assert tier.matches(0.75) is True

    def test_matches_exclusive_upper_bound(self):
        tier = RoutingTier(name="t", min_score=0.75, max_score=0.90)
        assert tier.matches(0.90) is False

    def test_matches_interior_score(self):
        tier = RoutingTier(name="t", min_score=0.60, max_score=0.75)
        assert tier.matches(0.67) is True

    def test_matches_below_range(self):
        tier = RoutingTier(name="t", min_score=0.60, max_score=0.75)
        assert tier.matches(0.50) is False

    def test_matches_above_range(self):
        tier = RoutingTier(name="t", min_score=0.60, max_score=0.75)
        assert tier.matches(0.80) is False

    def test_max_score_above_one_allowed(self):
        # max_score > 1.0 is legal — needed for the highest tier to capture 1.0
        tier = RoutingTier(name="t", min_score=0.90, max_score=1.01)
        assert tier.max_score == 1.01
        assert tier.matches(1.0) is True

    def test_invalid_min_score_negative(self):
        with pytest.raises(ValueError, match="min_score"):
            RoutingTier(name="t", min_score=-0.1, max_score=0.5)

    def test_invalid_min_score_above_one(self):
        with pytest.raises(ValueError, match="min_score"):
            RoutingTier(name="t", min_score=1.5, max_score=2.0)

    def test_invalid_max_not_greater_than_min(self):
        with pytest.raises(ValueError, match="max_score"):
            RoutingTier(name="t", min_score=0.5, max_score=0.5)

    def test_invalid_max_less_than_min(self):
        with pytest.raises(ValueError, match="max_score"):
            RoutingTier(name="t", min_score=0.8, max_score=0.5)

    def test_negative_max_retries_clamped_to_zero(self):
        tier = RoutingTier(name="t", min_score=0.0, max_score=0.5, max_retries=-5)
        assert tier.max_retries == 0

    def test_none_requires_normalised_to_empty_list(self):
        tier = RoutingTier(name="t", min_score=0.0, max_score=1.0, requires=None)  # type: ignore[arg-type]
        assert tier.requires == []

    def test_none_notify_normalised_to_empty_list(self):
        tier = RoutingTier(name="t", min_score=0.0, max_score=1.0, notify=None)  # type: ignore[arg-type]
        assert tier.notify == []


# ---------------------------------------------------------------------------
# RoutingConfig
# ---------------------------------------------------------------------------


class TestRoutingConfig:
    def test_empty_config(self):
        config = RoutingConfig()
        assert config.tiers == []

    def test_with_tiers(self):
        tiers = [
            RoutingTier(name="high", min_score=0.75, max_score=1.01, strategy="merge"),
            RoutingTier(name="low", min_score=0.00, max_score=0.75, strategy="reject"),
        ]
        config = RoutingConfig(tiers=tiers)
        assert len(config.tiers) == 2

    def test_none_tiers_normalised(self):
        config = RoutingConfig(tiers=None)  # type: ignore[arg-type]
        assert config.tiers == []


# ---------------------------------------------------------------------------
# RoutingDecision
# ---------------------------------------------------------------------------


class TestRoutingDecision:
    def test_basic_construction(self):
        decision = RoutingDecision(
            tier="auto_merge",
            score=0.95,
            confidence_level=ConfidenceLevel.HIGH,
            strategy="merge",
            matched=True,
        )
        assert decision.tier == "auto_merge"
        assert decision.strategy == "merge"
        assert decision.matched is True
        assert decision.max_retries == 0

    def test_unmatched_decision(self):
        decision = RoutingDecision(
            tier="unrouted",
            score=0.5,
            confidence_level=ConfidenceLevel.LOW,
            strategy="review",
            matched=False,
        )
        assert decision.matched is False
        assert decision.tier == "unrouted"

    def test_none_requires_normalised(self):
        decision = RoutingDecision(
            tier="t",
            score=0.8,
            confidence_level=ConfidenceLevel.MEDIUM,
            requires=None,  # type: ignore[arg-type]
        )
        assert decision.requires == []

    def test_none_notify_normalised(self):
        decision = RoutingDecision(
            tier="t",
            score=0.8,
            confidence_level=ConfidenceLevel.MEDIUM,
            notify=None,  # type: ignore[arg-type]
        )
        assert decision.notify == []


# ---------------------------------------------------------------------------
# DEFAULT_ROUTING_CONFIG
# ---------------------------------------------------------------------------


class TestDefaultRoutingConfig:
    def test_has_four_tiers(self):
        assert len(DEFAULT_ROUTING_CONFIG.tiers) == 4

    def test_tier_names(self):
        names = {t.name for t in DEFAULT_ROUTING_CONFIG.tiers}
        assert names == {"auto_merge", "queue_review", "retry", "reject"}

    def test_auto_merge_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "auto_merge")
        assert tier.min_score == 0.90
        assert tier.strategy == "merge"

    def test_queue_review_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "queue_review")
        assert tier.min_score == 0.75
        assert tier.max_score == 0.90
        assert tier.strategy == "queue_review"

    def test_retry_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "retry")
        assert tier.min_score == 0.60
        assert tier.max_score == 0.75
        assert tier.strategy == "retry"
        assert tier.max_retries == 2

    def test_reject_tier(self):
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "reject")
        assert tier.min_score == 0.00
        assert tier.max_score == 0.60
        assert tier.strategy == "reject"

    def test_default_config_has_no_threshold_errors(self):
        """The DEFAULT_ROUTING_CONFIG must be self-consistent."""
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        errors = engine.validate_thresholds()
        assert errors == [], f"DEFAULT_ROUTING_CONFIG has errors: {errors}"


# ---------------------------------------------------------------------------
# RoutingEngine.route()
# ---------------------------------------------------------------------------


class TestRoutingEngineRoute:
    def test_route_auto_merge_at_high_boundary(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.90))
        assert decision.tier == "auto_merge"
        assert decision.strategy == "merge"
        assert decision.matched is True

    def test_route_auto_merge_high_score(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.95))
        assert decision.tier == "auto_merge"

    def test_route_auto_merge_perfect_score(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(1.0))
        assert decision.tier == "auto_merge"

    def test_route_queue_review_at_boundary(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.75))
        assert decision.tier == "queue_review"
        assert decision.strategy == "queue_review"

    def test_route_queue_review_interior(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.82))
        assert decision.tier == "queue_review"

    def test_route_queue_review_just_below_auto_merge(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.8999))
        assert decision.tier == "queue_review"

    def test_route_retry_at_boundary(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.60))
        assert decision.tier == "retry"
        assert decision.strategy == "retry"
        assert decision.max_retries == 2

    def test_route_retry_interior(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.67))
        assert decision.tier == "retry"

    def test_route_retry_just_below_queue_review(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.7499))
        assert decision.tier == "retry"

    def test_route_reject_at_zero(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.0))
        assert decision.tier == "reject"
        assert decision.strategy == "reject"

    def test_route_reject_interior(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.30))
        assert decision.tier == "reject"

    def test_route_reject_just_below_retry(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.5999))
        assert decision.tier == "reject"

    def test_route_preserves_score(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.92))
        assert decision.score == pytest.approx(0.92)

    def test_route_preserves_confidence_level(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        result = _make_result(0.92)
        decision = engine.route(result)
        assert decision.confidence_level == result.confidence_level

    def test_route_unrouted_when_no_tier_matches(self):
        # Config with only a high tier — score 0.55 has no match
        config = RoutingConfig(tiers=[
            RoutingTier(name="high", min_score=0.60, max_score=1.01, strategy="merge"),
        ])
        engine = RoutingEngine(config)
        decision = engine.route(_make_result(0.55))
        assert decision.matched is False
        assert decision.tier == "unrouted"
        assert decision.strategy == "review"

    def test_route_empty_config_always_unrouted(self):
        engine = RoutingEngine(RoutingConfig(tiers=[]))
        decision = engine.route(_make_result(0.80))
        assert decision.matched is False
        assert decision.tier == "unrouted"

    def test_route_none_config_falls_back_to_default(self):
        engine = RoutingEngine(None)
        decision = engine.route(_make_result(0.95))
        assert decision.tier == "auto_merge"

    def test_route_copies_requires_list(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.92))
        # Should be a copy, not a reference to the tier's list
        decision.requires.append("extra")
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "auto_merge")
        assert "extra" not in tier.requires

    def test_route_copies_notify_list(self):
        engine = RoutingEngine(DEFAULT_ROUTING_CONFIG)
        decision = engine.route(_make_result(0.92))
        decision.notify.append("extra")
        tier = next(t for t in DEFAULT_ROUTING_CONFIG.tiers if t.name == "auto_merge")
        assert "extra" not in tier.notify


# ---------------------------------------------------------------------------
# RoutingEngine.validate_thresholds()
# ---------------------------------------------------------------------------


class TestValidateThresholds:
    def test_valid_two_tier_config(self):
        engine = RoutingEngine(_two_tier_config())
        errors = engine.validate_thresholds()
        assert errors == []

    def test_gap_between_tiers(self):
        engine = RoutingEngine(_two_tier_config(gap=True))
        errors = engine.validate_thresholds()
        assert len(errors) == 1
        assert "gap" in errors[0].lower()

    def test_overlap_between_tiers(self):
        engine = RoutingEngine(_two_tier_config(overlap=True))
        errors = engine.validate_thresholds()
        assert len(errors) == 1
        assert "overlap" in errors[0].lower()

    def test_gap_at_start_below_zero_point_zero(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="high", min_score=0.10, max_score=1.01, strategy="merge"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert any("gap" in e.lower() for e in errors)

    def test_gap_at_end_above_one_point_zero(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="low", min_score=0.00, max_score=0.80, strategy="reject"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert any("gap" in e.lower() for e in errors)

    def test_duplicate_tier_names(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="dup", min_score=0.00, max_score=0.50, strategy="reject"),
            RoutingTier(name="dup", min_score=0.50, max_score=1.01, strategy="merge"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert any("dup" in e.lower() for e in errors)

    def test_empty_config_no_errors(self):
        engine = RoutingEngine(RoutingConfig(tiers=[]))
        errors = engine.validate_thresholds()
        assert errors == []

    def test_single_full_coverage_tier(self):
        config = RoutingConfig(tiers=[
            RoutingTier(name="only", min_score=0.00, max_score=1.01, strategy="review"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert errors == []

    def test_multiple_errors_reported(self):
        # Duplicate name + gap at start + gap between
        config = RoutingConfig(tiers=[
            RoutingTier(name="t", min_score=0.20, max_score=0.50, strategy="reject"),
            RoutingTier(name="t", min_score=0.60, max_score=1.01, strategy="merge"),
        ])
        engine = RoutingEngine(config)
        errors = engine.validate_thresholds()
        assert len(errors) >= 2  # duplicate + gap at start + gap between


# ---------------------------------------------------------------------------
# _parse_routing_config()
# ---------------------------------------------------------------------------


class TestParseRoutingConfig:
    def test_none_returns_none(self):
        assert _parse_routing_config(None) is None

    def test_non_dict_returns_none(self):
        assert _parse_routing_config("string") is None
        assert _parse_routing_config(42) is None
        assert _parse_routing_config([]) is None

    def test_dict_without_tiers_returns_none(self):
        assert _parse_routing_config({"unknown": "value"}) is None

    def test_empty_tiers_list_returns_none(self):
        assert _parse_routing_config({"tiers": []}) is None

    def test_valid_two_tier_config(self):
        raw = {
            "tiers": [
                {
                    "name": "high",
                    "min_score": 0.75,
                    "max_score": 1.01,
                    "strategy": "merge",
                    "requires": ["review:APPROVE"],
                    "notify": ["slack:#main"],
                    "max_retries": 0,
                },
                {
                    "name": "low",
                    "min_score": 0.00,
                    "max_score": 0.75,
                    "strategy": "reject",
                },
            ]
        }
        config = _parse_routing_config(raw)
        assert config is not None
        assert isinstance(config, RoutingConfig)
        assert len(config.tiers) == 2

    def test_tier_fields_parsed_correctly(self):
        raw = {
            "tiers": [
                {
                    "name": "retry_tier",
                    "min_score": 0.60,
                    "max_score": 0.90,
                    "strategy": "retry",
                    "max_retries": 3,
                }
            ]
        }
        config = _parse_routing_config(raw)
        assert config is not None
        tier = config.tiers[0]
        assert tier.name == "retry_tier"
        assert tier.min_score == pytest.approx(0.60)
        assert tier.max_score == pytest.approx(0.90)
        assert tier.strategy == "retry"
        assert tier.max_retries == 3

    def test_non_dict_tier_entry_skipped(self):
        raw = {
            "tiers": [
                "not-a-dict",
                {"name": "ok", "min_score": 0.0, "max_score": 1.01, "strategy": "merge"},
            ]
        }
        config = _parse_routing_config(raw)
        assert config is not None
        assert len(config.tiers) == 1
        assert config.tiers[0].name == "ok"

    def test_invalid_tier_scores_skipped(self):
        # min_score > max_score should raise ValueError and be skipped
        raw = {
            "tiers": [
                {"name": "bad", "min_score": 0.9, "max_score": 0.5},
                {"name": "good", "min_score": 0.0, "max_score": 1.01},
            ]
        }
        config = _parse_routing_config(raw)
        assert config is not None
        assert len(config.tiers) == 1
        assert config.tiers[0].name == "good"


# ---------------------------------------------------------------------------
# Template integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def template_engine(tmp_path):
    """Return a TemplateEngine rooted at tmp_path."""
    from orchestration_engine.templates import TemplateEngine
    return TemplateEngine(templates_dir=tmp_path)


def _write_template(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal valid template YAML with optional extra content."""
    base = (
        "id: test-template\n"
        "name: Test Template\n"
        'version: "1.0.0"\n'
        "description: A test template\n"
        "author: Tester\n"
        "category: content\n"
        "use_cases:\n"
        "  - testing\n"
        "example_input:\n"
        "  topic: foo\n"
        "phases:\n"
        "  - id: write\n"
        "    name: Write\n"
        "    task_type: content\n"
        "    model_tier: sonnet\n"
        '    prompt_template: "Write about {input}"\n'
    )
    if extra:
        base += extra.strip() + "\n"
    p = tmp_path / "test-template.yaml"
    p.write_text(base)
    return p


class TestTemplateIntegration:
    def test_template_without_routing_config_is_none(self, tmp_path, template_engine):
        p = _write_template(tmp_path)
        template = template_engine.load_template(p)
        assert template.routing_config is None

    def test_template_with_routing_config_parsed(self, tmp_path, template_engine):
        extra = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.75
                  max_score: 1.01
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.75
                  strategy: reject
        """)
        p = _write_template(tmp_path, extra=extra)
        template = template_engine.load_template(p)
        assert template.routing_config is not None
        assert isinstance(template.routing_config, RoutingConfig)
        assert len(template.routing_config.tiers) == 2

    def test_template_routing_tier_fields(self, tmp_path, template_engine):
        extra = textwrap.dedent("""
            routing_config:
              tiers:
                - name: auto_merge
                  min_score: 0.90
                  max_score: 1.01
                  strategy: merge
                  requires:
                    - "review:APPROVE"
                  notify:
                    - "slack:#deploys"
                  max_retries: 0
                - name: reject
                  min_score: 0.00
                  max_score: 0.90
                  strategy: reject
        """)
        p = _write_template(tmp_path, extra=extra)
        template = template_engine.load_template(p)
        assert template.routing_config is not None
        high_tier = next(
            t for t in template.routing_config.tiers if t.name == "auto_merge"
        )
        assert high_tier.strategy == "merge"
        assert "review:APPROVE" in high_tier.requires
        assert "slack:#deploys" in high_tier.notify

    def test_validate_template_valid_routing_config_no_errors(self, tmp_path, template_engine):
        extra = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.75
                  max_score: 1.01
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.75
                  strategy: reject
        """)
        p = _write_template(tmp_path, extra=extra)
        template = template_engine.load_template(p)
        errors = template_engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert routing_errors == []

    def test_validate_template_gap_in_routing_config_reports_error(self, tmp_path, template_engine):
        extra = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.80
                  max_score: 1.01
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.60
                  strategy: reject
        """)
        p = _write_template(tmp_path, extra=extra)
        template = template_engine.load_template(p)
        errors = template_engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert len(routing_errors) >= 1
        assert any("gap" in e.lower() for e in routing_errors)

    def test_validate_template_overlap_in_routing_config_reports_error(self, tmp_path, template_engine):
        extra = textwrap.dedent("""
            routing_config:
              tiers:
                - name: high
                  min_score: 0.60
                  max_score: 1.01
                  strategy: merge
                - name: low
                  min_score: 0.00
                  max_score: 0.80
                  strategy: reject
        """)
        p = _write_template(tmp_path, extra=extra)
        template = template_engine.load_template(p)
        errors = template_engine.validate_template(template)
        routing_errors = [e for e in errors if "routing_config" in e]
        assert len(routing_errors) >= 1
        assert any("overlap" in e.lower() for e in routing_errors)

    def test_post_init_non_routing_config_normalised_to_none(self, tmp_path):
        from orchestration_engine.templates import PipelineTemplate
        template = PipelineTemplate(
            id="t",
            name="Test",
            routing_config="not-a-RoutingConfig",  # type: ignore[arg-type]
        )
        assert template.routing_config is None

    def test_post_init_valid_routing_config_preserved(self, tmp_path):
        from orchestration_engine.templates import PipelineTemplate
        config = RoutingConfig(tiers=[
            RoutingTier(name="only", min_score=0.0, max_score=1.01, strategy="review"),
        ])
        template = PipelineTemplate(
            id="t",
            name="Test",
            routing_config=config,
        )
        assert template.routing_config is config


# ---------------------------------------------------------------------------
# __init__.py exports
# ---------------------------------------------------------------------------


class TestModuleExports:
    def test_routing_tier_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "RoutingTier")

    def test_routing_config_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "RoutingConfig")

    def test_routing_decision_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "RoutingDecision")

    def test_routing_engine_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "RoutingEngine")

    def test_default_routing_config_exported(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "DEFAULT_ROUTING_CONFIG")

    def test_all_contains_routing_names(self):
        import orchestration_engine
        for name in ("RoutingTier", "RoutingConfig", "RoutingDecision",
                     "RoutingEngine", "DEFAULT_ROUTING_CONFIG"):
            assert name in orchestration_engine.__all__, f"{name} not in __all__"

    def test_exported_routing_tier_is_correct_class(self):
        from orchestration_engine import RoutingTier as ExportedTier
        assert ExportedTier is RoutingTier

    def test_exported_default_config_is_correct_instance(self):
        from orchestration_engine import DEFAULT_ROUTING_CONFIG as exported
        assert exported is DEFAULT_ROUTING_CONFIG
