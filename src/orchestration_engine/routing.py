"""Routing config schema and rules engine for confidence-based pipeline routing.

Provides a declarative way to map a composite confidence score (from
:class:`~orchestration_engine.confidence.ConfidenceResult`) to one of several
named tiers, each with its own action strategy, notification list, and retry cap.

Typical usage::

    from orchestration_engine.routing import RoutingEngine

    engine = RoutingEngine()          # uses DEFAULT_ROUTING_CONFIG
    decision = engine.evaluate(confidence_result)
    print(decision.action)            # e.g. "merge", "queue_review", …

Custom configurations can be loaded from a pipeline template YAML via
:func:`_parse_routing_config` and passed to :class:`RoutingEngine`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RoutingTier:
    """A named confidence band with associated routing strategy.

    Attributes:
        name:        Unique human-readable name for this tier
                     (e.g. ``"auto_merge"``).
        min_score:   Lower bound of the confidence band (inclusive), in [0, 1].
        max_score:   Upper bound of the confidence band (inclusive), in [0, 1].
        requires:    Optional list of preconditions that must be met before the
                     strategy fires (e.g. ``["approve_verdict"]``).
        notify:      Optional list of notification targets
                     (e.g. ``["slack:dev-team"]``).
        strategy:    Action verb to execute when this tier is matched
                     (e.g. ``"merge"``, ``"queue_review"``, ``"retry"``,
                     ``"reject"``).
        max_retries: Maximum number of retry attempts, meaningful only when
                     ``strategy == "retry"``.  ``0`` means no retries.
    """

    name: str
    min_score: float
    max_score: float
    requires: List[str] = field(default_factory=list)
    notify: List[str] = field(default_factory=list)
    strategy: str = ""
    max_retries: int = 0

    def __post_init__(self) -> None:
        self.min_score = float(self.min_score)
        self.max_score = float(self.max_score)
        self.max_retries = int(self.max_retries)

        if not (0.0 <= self.min_score <= 1.0):
            raise ValueError(
                f"RoutingTier '{self.name}': min_score must be in [0, 1], "
                f"got {self.min_score}"
            )
        if not (0.0 <= self.max_score <= 1.0):
            raise ValueError(
                f"RoutingTier '{self.name}': max_score must be in [0, 1], "
                f"got {self.max_score}"
            )
        if self.min_score > self.max_score:
            raise ValueError(
                f"RoutingTier '{self.name}': min_score ({self.min_score}) "
                f"must be <= max_score ({self.max_score})"
            )
        if self.requires is None:
            self.requires = []
        if self.notify is None:
            self.notify = []

    def matches(self, score: float) -> bool:
        """Return True if *score* falls within this tier's [min_score, max_score] range.

        Args:
            score: Composite confidence score in [0, 1].

        Returns:
            ``True`` if ``min_score <= score <= max_score``.
        """
        return self.min_score <= score <= self.max_score


@dataclass
class RoutingConfig:
    """Container for an ordered list of :class:`RoutingTier` definitions.

    Attributes:
        tiers: Ordered list of routing tiers.  Evaluation order is determined
               by :class:`RoutingEngine` (sorted by ``min_score`` descending).
    """

    tiers: List[RoutingTier] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tiers is None:
            self.tiers = []


@dataclass
class RoutingDecision:
    """The outcome of evaluating a confidence result against a routing config.

    Attributes:
        tier:        The matched :class:`RoutingTier`, or ``None`` when no
                     tier matched (should not happen with a well-formed config).
        action:      Short action string derived from ``tier.strategy``
                     (e.g. ``"merge"``).
        explanation: Human-readable explanation of why this tier was chosen.
    """

    tier: Optional[RoutingTier]
    action: str
    explanation: str


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_ROUTING_CONFIG: RoutingConfig = RoutingConfig(
    tiers=[
        RoutingTier(
            name="auto_merge",
            min_score=0.90,
            max_score=1.00,
            strategy="merge",
        ),
        RoutingTier(
            name="human_review",
            min_score=0.75,
            max_score=0.90,
            strategy="queue_review",
        ),
        RoutingTier(
            name="auto_retry",
            min_score=0.50,
            max_score=0.75,
            strategy="retry",
            max_retries=3,
        ),
        RoutingTier(
            name="reject",
            min_score=0.00,
            max_score=0.50,
            strategy="reject",
        ),
    ]
)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RoutingEngine:
    """Evaluates a :class:`~orchestration_engine.confidence.ConfidenceResult`
    against a :class:`RoutingConfig` and returns a :class:`RoutingDecision`.

    Args:
        config: Optional custom :class:`RoutingConfig`.  When ``None`` the
                :data:`DEFAULT_ROUTING_CONFIG` is used.
    """

    def __init__(self, config: Optional[RoutingConfig] = None) -> None:
        self._config: RoutingConfig = config if config is not None else DEFAULT_ROUTING_CONFIG

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, confidence_result: Any) -> RoutingDecision:
        """Route *confidence_result* to the highest-matching tier.

        Tiers are sorted by ``min_score`` **descending** so that the most
        specific (highest-threshold) tier is tested first.  The first tier
        whose ``min_score <= score`` wins.  This handles overlapping
        boundaries correctly: a score of exactly ``0.90`` matches
        ``auto_merge`` (min 0.90) before ``human_review`` (min 0.75).

        .. note::
            ``ConfidenceResult`` is imported inside this method to avoid
            circular import issues between ``routing`` and ``confidence``.

        Args:
            confidence_result: A
                :class:`~orchestration_engine.confidence.ConfidenceResult`
                instance (or any object with a ``composite_score`` attribute).

        Returns:
            A :class:`RoutingDecision` describing the matched tier and action.
        """
        # Deferred import to avoid circular dependency
        from .confidence import ConfidenceResult  # noqa: F401 (used for type clarity)

        score: float = float(confidence_result.composite_score)

        # Sort tiers by min_score DESC — highest threshold wins first
        sorted_tiers = sorted(
            self._config.tiers, key=lambda t: t.min_score, reverse=True
        )

        matched_tier: Optional[RoutingTier] = None
        for tier in sorted_tiers:
            if score >= tier.min_score:
                matched_tier = tier
                break

        if matched_tier is None:
            return RoutingDecision(
                tier=None,
                action="reject",
                explanation=(
                    f"No tier matched score={score:.4f}; "
                    f"defaulting to reject action."
                ),
            )

        explanation = (
            f"Score {score:.4f} matched tier '{matched_tier.name}' "
            f"[{matched_tier.min_score}, {matched_tier.max_score}]; "
            f"strategy={matched_tier.strategy!r}."
        )
        if matched_tier.max_retries:
            explanation += f" max_retries={matched_tier.max_retries}."

        return RoutingDecision(
            tier=matched_tier,
            action=matched_tier.strategy,
            explanation=explanation,
        )

    def validate_thresholds(self) -> List[str]:
        """Check the configured tiers for gaps and overlaps.

        Tiers are inspected in ascending ``min_score`` order.  Consecutive
        tiers ``(a, b)`` (where ``a.min_score < b.min_score``) are compared:

        * **Gap**: ``a.max_score < b.min_score`` — scores between the two
          tiers would fall through without a match.
        * **Overlap**: ``a.max_score > b.min_score`` — two tiers both claim
          to match the same score range.

        Additionally, a **warning** (not an error) is logged if:
        * The lowest ``min_score`` is not ``0.0`` (coverage starts above 0).
        * The highest ``max_score`` is not ``1.0`` (coverage ends below 1).

        Returns:
            List of error strings.  An empty list means no gaps or overlaps
            were detected.  Coverage warnings are emitted via ``logger.warning``
            but are **not** included in the returned list.
        """
        errors: List[str] = []

        if not self._config.tiers:
            return errors

        sorted_tiers = sorted(self._config.tiers, key=lambda t: t.min_score)

        # Check coverage bounds — warn only, do not error
        if sorted_tiers[0].min_score != 0.0:
            logger.warning(
                "RoutingConfig: coverage does not start at 0.0 "
                "(lowest min_score=%.4f). "
                "Scores below %.4f will not match any tier.",
                sorted_tiers[0].min_score,
                sorted_tiers[0].min_score,
            )
        if sorted_tiers[-1].max_score != 1.0:
            logger.warning(
                "RoutingConfig: coverage does not reach 1.0 "
                "(highest max_score=%.4f). "
                "Scores above %.4f will not match any tier.",
                sorted_tiers[-1].max_score,
                sorted_tiers[-1].max_score,
            )

        # Detect gaps and overlaps between adjacent pairs
        for i in range(len(sorted_tiers) - 1):
            a = sorted_tiers[i]
            b = sorted_tiers[i + 1]

            if a.max_score < b.min_score:
                errors.append(
                    f"Gap detected between tiers '{a.name}' and '{b.name}': "
                    f"scores in ({a.max_score}, {b.min_score}) are unmatched."
                )
            elif a.max_score > b.min_score:
                errors.append(
                    f"Overlap detected between tiers '{a.name}' and '{b.name}': "
                    f"scores in [{b.min_score}, {a.max_score}] match both tiers."
                )

        return errors


# ---------------------------------------------------------------------------
# Parser helper
# ---------------------------------------------------------------------------


def _parse_routing_config(raw: Any) -> Optional[RoutingConfig]:
    """Parse a raw ``routing_config:`` dict from a pipeline template YAML.

    Args:
        raw: The value of ``data.get("routing_config")`` — expected to be a
             dict with a ``"tiers"`` list, ``None``, or any other type
             (treated as absent).

    Returns:
        A :class:`RoutingConfig` instance when *raw* is a valid dict with a
        ``"tiers"`` list, otherwise ``None``.
    """
    if not isinstance(raw, dict):
        return None

    raw_tiers = raw.get("tiers")
    if not isinstance(raw_tiers, list):
        logger.warning(
            "_parse_routing_config: 'tiers' key missing or not a list — "
            "returning None."
        )
        return None

    tiers: List[RoutingTier] = []
    for item in raw_tiers:
        if not isinstance(item, dict):
            logger.warning(
                "_parse_routing_config: tier entry is not a dict (skipped): %r",
                item,
            )
            continue
        try:
            tiers.append(
                RoutingTier(
                    name=str(item.get("name", "")),
                    min_score=float(item.get("min_score", 0.0)),
                    max_score=float(item.get("max_score", 1.0)),
                    requires=list(item.get("requires") or []),
                    notify=list(item.get("notify") or []),
                    strategy=str(item.get("strategy", "")),
                    max_retries=int(item.get("max_retries", 0)),
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "_parse_routing_config: failed to parse tier %r: %s",
                item,
                exc,
            )

    return RoutingConfig(tiers=tiers)
