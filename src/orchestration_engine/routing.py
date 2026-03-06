"""Routing config schema and rules engine for pipeline confidence-based routing.

Issue #331.2 — maps ConfidenceResult composite scores to RoutingDecisions
via a configurable tier system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .confidence import ConfidenceLevel, ConfidenceResult

logger = logging.getLogger(__name__)


@dataclass
class RoutingTier:
    """A single tier in a routing configuration.

    Score ranges use half-open intervals [min_score, max_score) so adjacent
    tiers never overlap. The highest tier conventionally sets max_score
    slightly above 1.0 (e.g. 1.01) so a perfect score of 1.0 still matches.

    Attributes:
        name:        Unique identifier for this tier (e.g. "auto_merge").
        min_score:   Minimum composite score (inclusive).
        max_score:   Maximum composite score (exclusive). May exceed 1.0.
        requires:    Optional prerequisite conditions (e.g. ["review:APPROVE"]).
        notify:      Optional notification targets (e.g. ["slack:#deploys"]).
        strategy:    Action strategy ("merge", "queue_review", "retry", "reject").
        max_retries: Max retry count when strategy == "retry".
    """

    name: str
    min_score: float
    max_score: float
    requires: List[str] = field(default_factory=list)
    notify: List[str] = field(default_factory=list)
    strategy: str = "review"
    max_retries: int = 0

    def __post_init__(self) -> None:
        self.name = str(self.name)
        self.min_score = float(self.min_score)
        self.max_score = float(self.max_score)
        if self.requires is None:
            self.requires = []
        if self.notify is None:
            self.notify = []
        self.strategy = str(self.strategy)
        self.max_retries = max(0, int(self.max_retries))

        if self.min_score < 0.0 or self.min_score > 1.0:
            raise ValueError(
                f"RoutingTier '{self.name}': min_score must be in [0.0, 1.0], "
                f"got {self.min_score}"
            )
        # max_score may exceed 1.0 for the highest tier.
        if self.max_score <= self.min_score:
            raise ValueError(
                f"RoutingTier '{self.name}': max_score must be > min_score "
                f"({self.max_score} <= {self.min_score})"
            )

    def matches(self, score: float) -> bool:
        """Return True when score falls within [min_score, max_score)."""
        return self.min_score <= score < self.max_score


@dataclass
class RoutingConfig:
    """A complete routing configuration made up of ordered tiers.

    Attributes:
        tiers: List of RoutingTier instances.
    """

    tiers: List[RoutingTier] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tiers is None:
            self.tiers = []


@dataclass
class RoutingDecision:
    """The routing outcome for a single pipeline run.

    Attributes:
        tier:             Name of the matched tier, or "unrouted" when none matches.
        score:            The composite score from ConfidenceResult.
        confidence_level: The coarse ConfidenceLevel from ConfidenceResult.
        strategy:         The action strategy from the matched tier.
        requires:         Prerequisite condition strings from the matched tier.
        notify:           Notification targets from the matched tier.
        max_retries:      Max retry count from the matched tier.
        matched:          True when a tier was successfully matched.
    """

    tier: str
    score: float
    confidence_level: ConfidenceLevel
    strategy: str = "review"
    requires: List[str] = field(default_factory=list)
    notify: List[str] = field(default_factory=list)
    max_retries: int = 0
    matched: bool = True

    def __post_init__(self) -> None:
        if self.requires is None:
            self.requires = []
        if self.notify is None:
            self.notify = []


#: Four canonical tiers used when no custom routing config is declared.
DEFAULT_ROUTING_CONFIG: RoutingConfig = RoutingConfig(
    tiers=[
        RoutingTier(
            name="auto_merge",
            min_score=0.90,
            max_score=1.01,
            strategy="merge",
            requires=["review:APPROVE"],
            notify=["slack:#deploys"],
            max_retries=0,
        ),
        RoutingTier(
            name="queue_review",
            min_score=0.75,
            max_score=0.90,
            strategy="queue_review",
            requires=[],
            notify=["slack:#review-queue"],
            max_retries=0,
        ),
        RoutingTier(
            name="retry",
            min_score=0.60,
            max_score=0.75,
            strategy="retry",
            requires=[],
            notify=[],
            max_retries=2,
        ),
        RoutingTier(
            name="reject",
            min_score=0.00,
            max_score=0.60,
            strategy="reject",
            requires=[],
            notify=["slack:#failures"],
            max_retries=0,
        ),
    ]
)


class RoutingEngine:
    """Routes a ConfidenceResult to a RoutingDecision via configured tiers.

    Args:
        config: A RoutingConfig instance. When None, uses DEFAULT_ROUTING_CONFIG.
    """

    def __init__(self, config: Optional[RoutingConfig] = None) -> None:
        self.config: RoutingConfig = (
            config if config is not None else DEFAULT_ROUTING_CONFIG
        )

    def route(self, confidence_result: ConfidenceResult) -> RoutingDecision:
        """Determine the routing tier for confidence_result.

        Tiers are checked in descending order of min_score so the
        highest-priority tier wins when ranges overlap.

        Args:
            confidence_result: A populated ConfidenceResult.

        Returns:
            A RoutingDecision with the matched tier's metadata, or a
            fallback "unrouted" decision when no tier matches.
        """
        score = confidence_result.composite_score
        level = confidence_result.confidence_level

        sorted_tiers = sorted(
            self.config.tiers, key=lambda t: t.min_score, reverse=True
        )

        for tier in sorted_tiers:
            if tier.matches(score):
                logger.debug(
                    "route: score=%.4f matched tier '%s' (strategy=%s)",
                    score, tier.name, tier.strategy,
                )
                return RoutingDecision(
                    tier=tier.name,
                    score=score,
                    confidence_level=level,
                    strategy=tier.strategy,
                    requires=list(tier.requires),
                    notify=list(tier.notify),
                    max_retries=tier.max_retries,
                    matched=True,
                )

        logger.warning(
            "route: score=%.4f did not match any configured tier", score
        )
        return RoutingDecision(
            tier="unrouted",
            score=score,
            confidence_level=level,
            strategy="review",
            requires=[],
            notify=[],
            max_retries=0,
            matched=False,
        )

    def validate_thresholds(self) -> List[str]:
        """Validate tier thresholds for gaps, overlaps, and duplicate names.

        Returns:
            A list of error strings. Empty list means config is valid.
        """
        errors: List[str] = []
        tiers = self.config.tiers

        if not tiers:
            return []

        # Duplicate name check
        seen_names: Dict[str, int] = {}
        for idx, tier in enumerate(tiers):
            if tier.name in seen_names:
                errors.append(
                    f"Duplicate tier name '{tier.name}' "
                    f"(first at index {seen_names[tier.name]}, again at index {idx})"
                )
            else:
                seen_names[tier.name] = idx

        sorted_tiers = sorted(tiers, key=lambda t: t.min_score)

        # Gap: coverage must start at 0.0
        if sorted_tiers[0].min_score > 0.0:
            errors.append(
                f"Routing config has a gap: scores below "
                f"{sorted_tiers[0].min_score:.4f} are not covered by any tier "
                f"(lowest tier '{sorted_tiers[0].name}' starts at "
                f"{sorted_tiers[0].min_score:.4f})"
            )

        # Check consecutive tiers for gaps and overlaps
        for i in range(len(sorted_tiers) - 1):
            current = sorted_tiers[i]
            nxt = sorted_tiers[i + 1]

            if current.max_score > nxt.min_score:
                errors.append(
                    f"Routing tiers '{current.name}' and '{nxt.name}' overlap: "
                    f"'{current.name}' ends at {current.max_score:.4f} but "
                    f"'{nxt.name}' starts at {nxt.min_score:.4f}"
                )
            elif current.max_score < nxt.min_score:
                errors.append(
                    f"Routing config has a gap between tiers '{current.name}' "
                    f"and '{nxt.name}': range "
                    f"[{current.max_score:.4f}, {nxt.min_score:.4f}) is not covered"
                )

        # Gap: highest tier must reach score 1.0
        if sorted_tiers[-1].max_score < 1.0:
            errors.append(
                f"Routing config has a gap: scores above "
                f"{sorted_tiers[-1].max_score:.4f} are not covered by any tier "
                f"(highest tier '{sorted_tiers[-1].name}' ends at "
                f"{sorted_tiers[-1].max_score:.4f})"
            )

        return errors


def _parse_routing_config(raw: Any) -> Optional[RoutingConfig]:
    """Parse the routing_config: section of a pipeline YAML.

    Args:
        raw: The value of data.get("routing_config").

    Returns:
        A RoutingConfig instance or None.
    """
    if not isinstance(raw, dict):
        return None

    known_config_fields = {"tiers"}
    unknown = set(raw.keys()) - known_config_fields
    if unknown:
        logger.warning(
            "Template routing_config has unknown fields (ignored): %s",
            sorted(unknown),
        )

    raw_tiers = raw.get("tiers")
    if not isinstance(raw_tiers, list):
        return None

    known_tier_fields = {
        "name", "min_score", "max_score", "requires", "notify",
        "strategy", "max_retries",
    }

    tiers: List[RoutingTier] = []
    for idx, raw_tier in enumerate(raw_tiers):
        if not isinstance(raw_tier, dict):
            logger.warning(
                "routing_config.tiers[%d] is not a dict (ignored): %r",
                idx, raw_tier,
            )
            continue
        unknown_tier = set(raw_tier.keys()) - known_tier_fields
        if unknown_tier:
            logger.warning(
                "routing_config.tiers[%d] has unknown fields (ignored): %s",
                idx, sorted(unknown_tier),
            )
        try:
            tiers.append(
                RoutingTier(
                    name=str(raw_tier.get("name", f"tier_{idx}")),
                    min_score=float(raw_tier.get("min_score", 0.0)),
                    max_score=float(raw_tier.get("max_score", 1.0)),
                    requires=list(raw_tier.get("requires") or []),
                    notify=list(raw_tier.get("notify") or []),
                    strategy=str(raw_tier.get("strategy", "review")),
                    max_retries=int(raw_tier.get("max_retries", 0)),
                )
            )
        except (ValueError, TypeError) as exc:
            logger.error(
                "routing_config.tiers[%d]: failed to parse tier: %s", idx, exc,
            )

    return RoutingConfig(tiers=tiers) if tiers else None
