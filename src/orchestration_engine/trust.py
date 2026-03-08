"""Trust profile data model for the Orchestration Engine (Issue #4.2.1).

Provides :class:`TrustProfile` and :class:`TrustConfig` dataclasses used to
track per-(repo, template_id, task_type) trust state and configure the
adaptive auto-merge / human-review routing algorithm.

The ``TrustProfile`` dataclass mirrors the ``trust_profiles`` DB table.
The ``TrustConfig`` dataclass carries algorithm hyper-parameters with safe
defaults and is not persisted directly — callers embed it in higher-level
config or pass it at call-time.

Pattern reference: ``regression.py`` (data model) and
``reviewer_calibration.py`` (dataclass + ``to_dict()``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# TrustProfile
# ---------------------------------------------------------------------------


@dataclass
class TrustProfile:
    """Per-(repo, template_id, task_type) trust state.

    A ``TrustProfile`` is the primary record tracked in the ``trust_profiles``
    DB table.  One row exists per unique ``(repo, template_id, task_type)``
    triplet.

    Attributes:
        repo:                   Git repository slug (e.g. ``"owner/repo"``).
        template_id:            Pipeline template identifier (e.g.
                                ``"coding-pipeline-v1"``).
        task_type:              Task type string (e.g. ``"bugfix"``,
                                ``"feature"``).
        auto_merge_threshold:   Minimum confidence score in ``[0.0, 1.0]``
                                required to auto-merge without human review.
                                Default ``0.85``.
        human_review_threshold: Minimum confidence score in ``[0.0, 1.0]``
                                required to skip the human-review queue.
                                Must be ≤ ``auto_merge_threshold``.
                                Default ``0.70``.
        trust_score:            Current trust score in ``[0.0, 1.0]`` for
                                this profile.  Initialised to ``0.5`` (neutral).
        total_runs:             Total pipeline runs attributed to this profile.
        successful_merges:      Runs that were auto-merged and never reverted.
        regressions:            Regressions detected after an auto-merge.
        reverted_prs:           PRs reverted after an auto-merge.
        last_run_at:            UTC ISO-8601 timestamp of the most-recent run,
                                or ``None`` if no run recorded yet.
        id:                     Auto-assigned integer DB primary key.
                                ``None`` until the row has been persisted.
        created_at:             UTC ISO-8601 timestamp when the row was first
                                created.  Auto-populated on construction.
        updated_at:             UTC ISO-8601 timestamp of the last update.
                                Auto-populated on construction; callers should
                                refresh this when persisting updates.
    """

    repo: str
    template_id: str
    task_type: str

    # Thresholds
    auto_merge_threshold: float = 0.85
    human_review_threshold: float = 0.70

    # Live state
    trust_score: float = 0.5
    total_runs: int = 0
    successful_merges: int = 0
    regressions: int = 0
    reverted_prs: int = 0
    last_run_at: Optional[str] = None

    # DB-managed fields
    id: Optional[int] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain-dict representation suitable for DB insertion.

        Returns:
            Dict with all fields.  ``id`` is included (may be ``None`` for
            unsaved instances).
        """
        return {
            "id":                     self.id,
            "repo":                   self.repo,
            "template_id":            self.template_id,
            "task_type":              self.task_type,
            "auto_merge_threshold":   self.auto_merge_threshold,
            "human_review_threshold": self.human_review_threshold,
            "trust_score":            self.trust_score,
            "total_runs":             self.total_runs,
            "successful_merges":      self.successful_merges,
            "regressions":            self.regressions,
            "reverted_prs":           self.reverted_prs,
            "last_run_at":            self.last_run_at,
            "created_at":             self.created_at,
            "updated_at":             self.updated_at,
        }


# ---------------------------------------------------------------------------
# TrustConfig
# ---------------------------------------------------------------------------


@dataclass
class TrustConfig:
    """Algorithm hyper-parameters for the trust-score update rule.

    ``TrustConfig`` is not persisted directly to a DB table; it is typically
    embedded in a higher-level run configuration or instantiated with defaults.

    Attributes:
        success_delta:       How much to increase ``trust_score`` after a
                             successful auto-merge.  Default ``+0.02``.
        regression_penalty:  How much to decrease ``trust_score`` after a
                             detected regression.  Default ``-0.10``.
        revert_penalty:      How much to decrease ``trust_score`` after a PR
                             revert.  Default ``-0.15``.
        min_score:           Lower bound for ``trust_score``.  Default ``0.0``.
        max_score:           Upper bound for ``trust_score``.  Default ``1.0``.
        initial_score:       Starting score for a brand-new profile.
                             Default ``0.5`` (neutral).
        initial_auto_merge_threshold:   Default ``auto_merge_threshold`` used
                                        when a new ``TrustProfile`` is created.
                                        Default ``0.85``.
        initial_human_review_threshold: Default ``human_review_threshold`` used
                                        when a new ``TrustProfile`` is created.
                                        Default ``0.70``.
    """

    success_delta: float = 0.02
    regression_penalty: float = -0.10
    revert_penalty: float = -0.15
    min_score: float = 0.0
    max_score: float = 1.0
    initial_score: float = 0.5
    initial_auto_merge_threshold: float = 0.85
    initial_human_review_threshold: float = 0.70

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain-dict representation of the config."""
        return {
            "success_delta":                   self.success_delta,
            "regression_penalty":              self.regression_penalty,
            "revert_penalty":                  self.revert_penalty,
            "min_score":                       self.min_score,
            "max_score":                       self.max_score,
            "initial_score":                   self.initial_score,
            "initial_auto_merge_threshold":    self.initial_auto_merge_threshold,
            "initial_human_review_threshold":  self.initial_human_review_threshold,
        }
