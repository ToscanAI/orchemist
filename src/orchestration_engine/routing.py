"""Routing config schema and rules engine for confidence-based pipeline routing.

Provides a declarative way to map a composite confidence score (from
:class:`~orchestration_engine.confidence.ConfidenceResult`) to one of several
named tiers, each with its own action strategy, notification list, and retry cap.

Typical usage::

    from orchestration_engine.routing import RoutingEngine

    engine = RoutingEngine()          # uses DEFAULT_ROUTING_CONFIG
    decision = engine.route(confidence_result)
    print(decision.strategy)          # e.g. "merge", "queue_review", …

Custom configurations can be loaded from a pipeline template YAML via
:func:`_parse_routing_config` and passed to :class:`RoutingEngine`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

from .confidence import ConfidenceLevel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingTier:
    """A named confidence band with associated routing strategy.

    Attributes:
        name:        Unique human-readable name for this tier
                     (e.g. ``"auto_merge"``).
        min_score:   Lower bound of the confidence band (inclusive), in [0, 1].
        max_score:   Upper bound of the confidence band (exclusive), may exceed
                     1.0 so the highest tier can capture a score of exactly 1.0.
        requires:    Optional list of preconditions that must be met before the
                     strategy fires (e.g. ``["approve_verdict"]``).
        notify:      Optional list of notification targets
                     (e.g. ``["slack:dev-team"]``).
        strategy:    Action verb to execute when this tier is matched
                     (e.g. ``"merge"``, ``"queue_review"``, ``"retry"``,
                     ``"reject"``).
        max_retries: Maximum number of retry attempts, meaningful only when
                     ``strategy == "retry"``.  Negative values are clamped to 0.
    """

    name: str
    min_score: float
    max_score: float
    requires: List[str] = field(default_factory=list)
    notify: List[str] = field(default_factory=list)
    strategy: str = "review"
    max_retries: int = 0

    def __post_init__(self) -> None:
        # Normalize types — frozen dataclass requires object.__setattr__
        object.__setattr__(self, "min_score", float(self.min_score))
        object.__setattr__(self, "max_score", float(self.max_score))
        # Clamp max_retries to 0 if negative
        object.__setattr__(self, "max_retries", max(0, int(self.max_retries)))
        if self.requires is None:
            object.__setattr__(self, "requires", [])
        if self.notify is None:
            object.__setattr__(self, "notify", [])

        if not (0.0 <= self.min_score <= 1.0):
            raise ValueError(
                f"RoutingTier '{self.name}': min_score must be in [0, 1], "
                f"got {self.min_score}"
            )
        # max_score may exceed 1.0 (e.g. 1.01) so the top tier captures 1.0
        # under exclusive-upper-bound semantics.  Only validate that it is
        # strictly greater than min_score (tier must have positive width).
        if self.max_score <= self.min_score:
            raise ValueError(
                f"RoutingTier '{self.name}': max_score ({self.max_score}) "
                f"must be strictly greater than min_score ({self.min_score})"
            )

    def matches(self, score: float) -> bool:
        """Return True if *score* falls within this tier's [min_score, max_score) range.

        The upper bound is **exclusive** so that adjacent tiers with a shared
        boundary (e.g. ``[0.75, 0.90)`` and ``[0.90, 1.01)``) never both claim
        the same score.  The higher tier always wins because
        :meth:`RoutingEngine.route` iterates in descending ``min_score`` order.

        Args:
            score: Composite confidence score in [0, 1].

        Returns:
            ``True`` if ``min_score <= score < max_score``.
        """
        return self.min_score <= score < self.max_score


@dataclass(frozen=True)
class RoutingConfig:
    """Container for an ordered list of :class:`RoutingTier` definitions.

    Attributes:
        tiers: Ordered list of routing tiers.  Evaluation order is determined
               by :class:`RoutingEngine` (sorted by ``min_score`` descending).
    """

    tiers: List[RoutingTier] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tiers is None:
            object.__setattr__(self, "tiers", [])


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of routing a confidence result against a routing config.

    Attributes:
        tier:             Name of the matched tier, or ``"unrouted"`` when no
                          tier matched.
        score:            The composite confidence score that was evaluated.
        confidence_level: The :class:`~confidence.ConfidenceLevel` from the
                          originating :class:`~confidence.ConfidenceResult`.
        strategy:         Action verb from the matched tier
                          (e.g. ``"merge"``).  Defaults to ``"review"`` when
                          no tier matched.
        matched:          ``True`` when a tier was matched, ``False`` for the
                          unrouted fallback.
        requires:         Copy of the matched tier's ``requires`` list, or an
                          empty list when unrouted.
        notify:           Copy of the matched tier's ``notify`` list, or an
                          empty list when unrouted.
        max_retries:      Maximum retry count from the matched tier, or ``0``
                          when unrouted.
    """

    tier: str
    score: float
    confidence_level: ConfidenceLevel
    strategy: str = "review"
    matched: bool = True
    requires: List[str] = field(default_factory=list)
    notify: List[str] = field(default_factory=list)
    max_retries: int = 0

    def __post_init__(self) -> None:
        if self.requires is None:
            object.__setattr__(self, "requires", [])
        if self.notify is None:
            object.__setattr__(self, "notify", [])


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_ROUTING_CONFIG: RoutingConfig = RoutingConfig(
    tiers=[
        RoutingTier(
            name="auto_merge",
            min_score=0.90,
            max_score=1.01,  # exclusive upper bound — captures score == 1.0
            strategy="merge",
        ),
        RoutingTier(
            name="queue_review",
            min_score=0.75,
            max_score=0.90,
            strategy="queue_review",
        ),
        RoutingTier(
            name="retry",
            min_score=0.60,
            max_score=0.75,
            strategy="retry",
            max_retries=2,
        ),
        RoutingTier(
            name="reject",
            min_score=0.00,
            max_score=0.60,
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

    def route(self, confidence_result: Any) -> RoutingDecision:
        """Route *confidence_result* to the highest-matching tier.

        Tiers are sorted by ``min_score`` **descending** so that the most
        specific (highest-threshold) tier is tested first.  The first tier
        whose :meth:`RoutingTier.matches` returns ``True`` wins.

        .. note::
            **Adjacent boundary behaviour:** when two tiers share a boundary
            value (e.g. tier A covers ``[0.5, 1.01)`` and tier B covers
            ``[0.0, 0.5)``), the *higher* tier always wins because tiers are
            evaluated in descending ``min_score`` order — A is checked before
            B, so a score of exactly ``0.5`` is routed to A.  The exclusive
            upper bound on :meth:`RoutingTier.matches` ensures the same score
            cannot match both tiers simultaneously.

        Args:
            confidence_result: A
                :class:`~orchestration_engine.confidence.ConfidenceResult`
                instance (or any object with ``composite_score`` and
                ``confidence_level`` attributes).

        Returns:
            A :class:`RoutingDecision` describing the matched tier and action.
        """
        score: float = float(confidence_result.composite_score)
        confidence_level: ConfidenceLevel = confidence_result.confidence_level

        # Sort tiers by min_score DESC — highest threshold wins first
        sorted_tiers = sorted(
            self._config.tiers, key=lambda t: t.min_score, reverse=True
        )

        for tier in sorted_tiers:
            if tier.matches(score):
                return RoutingDecision(
                    tier=tier.name,
                    score=score,
                    confidence_level=confidence_level,
                    strategy=tier.strategy,
                    matched=True,
                    requires=list(tier.requires),
                    notify=list(tier.notify),
                    max_retries=tier.max_retries,
                )

        # Fallback — no tier matched
        return RoutingDecision(
            tier="unrouted",
            score=score,
            confidence_level=confidence_level,
            strategy="review",
            matched=False,
        )

    def evaluate(self, confidence_result: Any) -> RoutingDecision:
        """Alias for :meth:`route` (kept for backward compatibility)."""
        return self.route(confidence_result)

    def validate_thresholds(self) -> List[str]:
        """Check the configured tiers for gaps, overlaps, and duplicate names.

        Tiers are inspected in ascending ``min_score`` order.

        Errors are reported for:
        * **Duplicate tier names** — two tiers share the same ``name``.
        * **Start gap** — ``min_score`` of the lowest tier is above ``0.0``.
        * **End gap** — ``max_score`` of the highest tier is below ``1.0``.
        * **Gap between tiers** — ``a.max_score < b.min_score`` for consecutive
          tiers ``(a, b)`` — scores in that range fall through unmatched.
        * **Overlap between tiers** — ``a.max_score > b.min_score`` — two tiers
          both claim the same score range.

        Returns:
            List of error strings.  An empty list means no issues were found.
        """
        errors: List[str] = []

        if not self._config.tiers:
            return errors

        sorted_tiers = sorted(self._config.tiers, key=lambda t: t.min_score)

        # Detect duplicate tier names
        seen_names: set[str] = set()
        for tier in sorted_tiers:
            if tier.name in seen_names:
                errors.append(
                    f"Duplicate tier name '{tier.name}' detected in routing config."
                )
            seen_names.add(tier.name)

        # Coverage start gap
        if sorted_tiers[0].min_score > 0.0:
            errors.append(
                f"Gap detected: coverage starts at "
                f"{sorted_tiers[0].min_score:.4f}; "
                f"scores below {sorted_tiers[0].min_score:.4f} are unmatched."
            )

        # Coverage end gap
        if sorted_tiers[-1].max_score < 1.0:
            errors.append(
                f"Gap detected: coverage ends at "
                f"{sorted_tiers[-1].max_score:.4f}; "
                f"scores of {sorted_tiers[-1].max_score:.4f} and above are unmatched."
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
        non-empty ``"tiers"`` list, otherwise ``None``.
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

    if not raw_tiers:
        logger.warning(
            "routing_config has empty tiers list — no scores will match any tier"
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
                    strategy=str(item.get("strategy", "review")),
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
