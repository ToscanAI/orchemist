"""Trust profile data model and EMA-based calibrator for the Orchestration Engine.

Issue #4.2.1 — :class:`TrustProfile` and :class:`TrustConfig` dataclasses.
Issue #4.2.2 — :class:`TrustCalibrator` (EMA-based trust score updater).

:class:`TrustProfile` mirrors the ``trust_profiles`` DB table and holds the
per-(repo, template_id, task_type) trust state.

:class:`TrustConfig` carries algorithm hyper-parameters with safe defaults and
is not persisted directly — callers embed it in higher-level config or pass it
at call-time.

:class:`TrustCalibrator` receives post-run outcomes and updates the
``trust_profiles`` row via an Exponential Moving Average (EMA) formula.  Every
update is also written to the ``trust_adjustments`` audit table.

Pattern reference: ``regression.py`` (data model) and
``reviewer_calibration.py`` (dataclass + ``to_dict()``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Outcome score constants (raw EMA inputs)                    Issue #4.2.2
# ---------------------------------------------------------------------------

OUTCOME_SCORES: Dict[str, float] = {
    "run_success":           1.0,
    "regression":           -3.0,
    "revert":               -2.0,
    "human_override_reject": -1.0,
}

VALID_OUTCOMES: frozenset = frozenset(OUTCOME_SCORES)


# ---------------------------------------------------------------------------
# TrustCalibrator                                              Issue #4.2.2
# ---------------------------------------------------------------------------


class TrustCalibrator:
    """EMA-based trust score updater for a specific (repo, template_id, task_type) profile.

    Each call to :meth:`update_after_run` reads the current ``trust_score``
    from the DB, applies an Exponential Moving Average (EMA) update based on
    the run outcome, persists the new score, and logs the adjustment to
    ``trust_adjustments``.

    The *auto-merge threshold* is derived dynamically from the trust score
    after every update.  A bootstrap guard locks the threshold at
    ``conservative`` until at least ``bootstrap_threshold`` successful merges
    have been recorded for the profile.

    Args:
        repo:               Git repository slug (e.g. ``"owner/repo"``).
        template_id:        Pipeline template identifier
                            (e.g. ``"coding-pipeline-v1"``).
        task_type:          Task type string (e.g. ``"bugfix"``).
        alpha:              EMA smoothing factor in ``(0, 1]``.  Higher values
                            make the score react faster to recent outcomes.
                            Default ``0.1``.
        conservative:       Upper-bound threshold — used during bootstrap and
                            when trust is low.  Default ``0.98``.
        aggressive:         Lower-bound threshold — applied at maximum trust
                            (score = 1.0) once past bootstrap.  Default ``0.7``.
        bootstrap_threshold: Minimum number of *successful* merges required
                            before the threshold can relax below
                            ``conservative``.  Default ``10``.

    Raises:
        ValueError: If any constructor argument is out of range.

    Example::

        calibrator = TrustCalibrator(
            repo="owner/repo",
            template_id="coding-pipeline-v1",
            task_type="bugfix",
        )
        result = calibrator.update_after_run(
            run_id="run-abc-123",
            outcome="run_success",
            db=db,
        )
        print(result["new_score"], result["threshold"])
    """

    def __init__(
        self,
        repo: str,
        template_id: str,
        task_type: str,
        alpha: float = 0.1,
        conservative: float = 0.98,
        aggressive: float = 0.7,
        bootstrap_threshold: int = 10,
    ) -> None:
        # Validate parameters
        if not (0.0 < alpha <= 1.0):
            raise ValueError(
                f"alpha must be in (0, 1], got {alpha!r}"
            )
        if not (0.0 <= aggressive < conservative <= 1.0):
            raise ValueError(
                f"aggressive and conservative must satisfy "
                f"0.0 <= aggressive < conservative <= 1.0; "
                f"got aggressive={aggressive!r}, conservative={conservative!r}"
            )
        if bootstrap_threshold < 0:
            raise ValueError(
                f"bootstrap_threshold must be >= 0, got {bootstrap_threshold!r}"
            )

        self.repo = repo
        self.template_id = template_id
        self.task_type = task_type
        self.alpha = alpha
        self.conservative = conservative
        self.aggressive = aggressive
        self.bootstrap_threshold = bootstrap_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_threshold(
        self,
        trust_score: float,
        successful_merges: int,
    ) -> float:
        """Return the auto-merge threshold for the given state.

        Pure function — no DB reads or writes.

        During bootstrap (``successful_merges < bootstrap_threshold``) the
        threshold is locked at :attr:`conservative`.  Once past bootstrap the
        threshold is linearly interpolated between :attr:`conservative` (at
        ``trust_score = 0``) and :attr:`aggressive` (at ``trust_score = 1``):

        .. code-block:: text

            threshold = conservative - trust_score * (conservative - aggressive)

        The result is clamped to ``[0.0, 1.0]``.

        Args:
            trust_score:       Current trust score in ``[0.0, 1.0]``.
            successful_merges: Number of successful auto-merges recorded for
                               this profile.

        Returns:
            Auto-merge threshold in ``[0.0, 1.0]``.
        """
        if successful_merges < self.bootstrap_threshold:
            return self.conservative
        raw = self.conservative - trust_score * (self.conservative - self.aggressive)
        return max(0.0, min(1.0, raw))

    def update_after_run(
        self,
        run_id: str,
        outcome: str,
        db: "Database",
    ) -> Dict[str, Any]:
        """Apply an EMA update to the trust profile and log the adjustment.

        Steps performed:

        1. Validate ``outcome`` against :data:`VALID_OUTCOMES`.
        2. Load (or initialise) the trust profile from the DB.
        3. Compute the new trust score via EMA:
           ``new_score = clamp(alpha * outcome_score + (1 - alpha) * old_score, 0.0, 1.0)``
        4. Increment run counters appropriate to the outcome.
        5. Compute the new auto-merge threshold via :meth:`compute_threshold`.
        6. Persist the updated profile via ``db.upsert_trust_profile``.
        7. Record the adjustment via ``db.insert_trust_adjustment``.

        Args:
            run_id:  Identifier of the pipeline run that just completed.
            outcome: One of ``"run_success"``, ``"regression"``,
                     ``"revert"``, or ``"human_override_reject"``.
            db:      :class:`~db.Database` instance used for persistence.

        Returns:
            A dict summarising the update::

                {
                    "profile_id":       int,
                    "adjustment_id":    int,
                    "run_id":           str,
                    "outcome":          str,
                    "old_score":        float,
                    "new_score":        float,
                    "delta":            float,
                    "threshold":        float,
                    "total_runs":       int,
                    "successful_merges": int,
                    "regressions":      int,
                    "reverted_prs":     int,
                }

        Raises:
            ValueError: When ``outcome`` is not a member of
                        :data:`VALID_OUTCOMES`.
        """
        if outcome not in VALID_OUTCOMES:
            raise ValueError(
                f"Unknown outcome {outcome!r}. "
                f"Valid outcomes: {sorted(VALID_OUTCOMES)}"
            )

        # ------------------------------------------------------------------
        # Load or initialise profile
        # ------------------------------------------------------------------
        profile = db.get_trust_profile(self.repo, self.template_id, self.task_type)
        if profile is None:
            # First run — create a fresh profile with TrustProfile defaults
            default = TrustProfile(
                repo=self.repo,
                template_id=self.template_id,
                task_type=self.task_type,
            )
            profile_id = db.upsert_trust_profile(default.to_dict())
            profile = db.get_trust_profile(self.repo, self.template_id, self.task_type)
            assert profile is not None  # just inserted
        else:
            profile_id = profile["id"]

        old_score:        float = float(profile["trust_score"])
        total_runs:       int   = int(profile["total_runs"])
        successful_merges: int  = int(profile["successful_merges"])
        regressions:      int   = int(profile["regressions"])
        reverted_prs:     int   = int(profile["reverted_prs"])

        # ------------------------------------------------------------------
        # EMA update
        # ------------------------------------------------------------------
        outcome_score = OUTCOME_SCORES[outcome]
        raw = self.alpha * outcome_score + (1.0 - self.alpha) * old_score
        new_score = max(0.0, min(1.0, raw))
        delta = new_score - old_score

        # ------------------------------------------------------------------
        # Update counters
        # ------------------------------------------------------------------
        total_runs += 1
        if outcome == "run_success":
            successful_merges += 1
        elif outcome == "regression":
            regressions += 1
        elif outcome == "revert":
            reverted_prs += 1
        # "human_override_reject" does not increment a dedicated counter

        # ------------------------------------------------------------------
        # Derive new threshold
        # ------------------------------------------------------------------
        new_threshold = self.compute_threshold(new_score, successful_merges)

        # ------------------------------------------------------------------
        # Persist updated profile
        # ------------------------------------------------------------------
        now = datetime.now(timezone.utc).isoformat()
        updated_profile: Dict[str, Any] = {
            "repo":                   self.repo,
            "template_id":            self.template_id,
            "task_type":              self.task_type,
            "auto_merge_threshold":   new_threshold,
            "human_review_threshold": float(profile["human_review_threshold"]),
            "trust_score":            new_score,
            "total_runs":             total_runs,
            "successful_merges":      successful_merges,
            "regressions":            regressions,
            "reverted_prs":           reverted_prs,
            "last_run_at":            now,
            "created_at":             profile["created_at"],
            "updated_at":             now,
        }
        db.upsert_trust_profile(updated_profile)

        # ------------------------------------------------------------------
        # Log adjustment
        # ------------------------------------------------------------------
        adjustment_id = db.insert_trust_adjustment({
            "profile_id":  profile_id,
            "delta":       delta,
            "reason":      outcome,
            "run_id":      run_id,
            "score_before": old_score,
            "score_after": new_score,
            "created_at":  now,
        })

        logger.debug(
            "TrustCalibrator: %s/%s/%s outcome=%r old=%.4f new=%.4f delta=%.4f threshold=%.4f",
            self.repo,
            self.template_id,
            self.task_type,
            outcome,
            old_score,
            new_score,
            delta,
            new_threshold,
        )

        return {
            "profile_id":        profile_id,
            "adjustment_id":     adjustment_id,
            "run_id":            run_id,
            "outcome":           outcome,
            "old_score":         old_score,
            "new_score":         new_score,
            "delta":             delta,
            "threshold":         new_threshold,
            "total_runs":        total_runs,
            "successful_merges": successful_merges,
            "regressions":       regressions,
            "reverted_prs":      reverted_prs,
        }


# ---------------------------------------------------------------------------
# Idle-profile trust decay                                     Issue #4.2.4
# ---------------------------------------------------------------------------

#: Default decay rate applied to trust scores per week of inactivity.
DEFAULT_DECAY_RATE: float = 0.05

#: Minimum trust score after decay; scores are never reduced below this floor.
DECAY_FLOOR: float = 0.3

#: Number of days without a pipeline run before decay begins.
DECAY_THRESHOLD_DAYS: int = 7


def decay_idle_profiles(
    db: "Database",
    decay_rate: float = DEFAULT_DECAY_RATE,
    threshold_days: int = DECAY_THRESHOLD_DAYS,
    floor: float = DECAY_FLOOR,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Apply weekly trust-score decay to profiles idle for *threshold_days* or more.

    For each trust profile whose ``last_run_at`` is older than *threshold_days*
    (or has never been set), the score is decremented by *decay_rate* for every
    full week of inactivity, subject to a minimum of *floor*.

    After each score update the ``auto_merge_threshold`` is re-derived from the
    new score using a default :class:`TrustCalibrator` instance for the profile.
    A ``trust_adjustments`` row is written for every profile that is actually
    modified (i.e. where the computed delta is non-zero after the floor clamp).

    Args:
        db:             :class:`~db.Database` instance to read and write.
        decay_rate:     Score reduction applied per full idle week.
                        Default ``0.05``.
        threshold_days: Minimum idle days before decay starts.
                        Default ``7``.
        floor:          Minimum score value; decay never pushes below this.
                        Default ``0.3``.
        now:            Reference timestamp for computing idle duration.
                        Defaults to ``datetime.now(timezone.utc)``.  Intended
                        for use in tests to pin time.

    Returns:
        List of dicts describing each profile that was modified::

            [
                {
                    "profile_id":   int,
                    "adjustment_id": int,
                    "old_score":    float,
                    "new_score":    float,
                    "delta":        float,
                    "weeks_idle":   int,
                    "threshold":    float,
                },
                ...
            ]

        Profiles that are within the idle threshold (or already at the floor)
        are not included in the returned list.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(days=threshold_days)

    profiles = db.list_trust_profiles()
    results: List[Dict[str, Any]] = []

    for profile in profiles:
        last_run_at_str: Optional[str] = profile.get("last_run_at")

        if last_run_at_str is None:
            # Profile has never had a run — treat as infinitely idle (1 week)
            weeks_idle = 1
        else:
            last_run_at = datetime.fromisoformat(last_run_at_str)
            # Ensure timezone-aware for comparison
            if last_run_at.tzinfo is None:
                last_run_at = last_run_at.replace(tzinfo=timezone.utc)

            if last_run_at >= cutoff:
                # Active recently — no decay needed
                continue

            idle_duration = now - last_run_at
            weeks_idle = max(1, int(idle_duration.total_seconds() // (7 * 24 * 3600)))

        old_score: float = float(profile["trust_score"])
        if old_score <= floor:
            # Already at or below the floor — nothing to do
            continue

        raw_new_score = old_score - decay_rate * weeks_idle
        new_score = max(floor, raw_new_score)
        delta = new_score - old_score

        if delta == 0.0:
            # No change (e.g. decay_rate is 0)
            continue

        # Re-derive auto_merge_threshold using default calibrator settings
        calibrator = TrustCalibrator(
            repo=profile["repo"],
            template_id=profile["template_id"],
            task_type=profile["task_type"],
        )
        successful_merges = int(profile.get("successful_merges", 0))
        new_threshold = calibrator.compute_threshold(new_score, successful_merges)

        # Persist updated profile
        now_iso = now.isoformat()
        updated: Dict[str, Any] = {
            "repo":                   profile["repo"],
            "template_id":            profile["template_id"],
            "task_type":              profile["task_type"],
            "auto_merge_threshold":   new_threshold,
            "human_review_threshold": float(profile["human_review_threshold"]),
            "trust_score":            new_score,
            "total_runs":             int(profile["total_runs"]),
            "successful_merges":      successful_merges,
            "regressions":            int(profile["regressions"]),
            "reverted_prs":           int(profile["reverted_prs"]),
            "last_run_at":            last_run_at_str,
            "created_at":             profile["created_at"],
            "updated_at":             now_iso,
        }
        db.upsert_trust_profile(updated)

        # Log the adjustment
        profile_id = int(profile["id"])
        adjustment_id = db.insert_trust_adjustment({
            "profile_id":   profile_id,
            "delta":        delta,
            "reason":       "idle_decay",
            "run_id":       None,
            "score_before": old_score,
            "score_after":  new_score,
            "created_at":   now_iso,
        })

        logger.debug(
            "decay_idle_profiles: profile_id=%d %s/%s/%s weeks_idle=%d "
            "old=%.4f new=%.4f delta=%.4f threshold=%.4f",
            profile_id,
            profile["repo"],
            profile["template_id"],
            profile["task_type"],
            weeks_idle,
            old_score,
            new_score,
            delta,
            new_threshold,
        )

        results.append({
            "profile_id":    profile_id,
            "adjustment_id": adjustment_id,
            "old_score":     old_score,
            "new_score":     new_score,
            "delta":         delta,
            "weeks_idle":    weeks_idle,
            "threshold":     new_threshold,
        })

    return results
