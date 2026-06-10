"""sprint_chain.py — Post-merge sprint chain automation (Issue #514).

Provides :class:`SprintChainManager` which orchestrates automatic label
advancement after a successful PR auto-merge.  When a pipeline run merges
successfully, the manager:

1. Loads the sprint queue config from a YAML file.
2. Checks the confidence-score guard rail.
3. Finds the next unprocessed issue in the queue.
4. Checks the daily-budget guard rail.
5. Checks for a human-pause override.
6. Marks the current issue as processed in the DB.
7. Labels the next issue ``pipeline-ready`` via the GitHub CLI.
8. Posts a contextual comment on the next issue.

All public methods are safe to call from ``_dispatch_auto_merge`` in
``daemon.py`` — the caller wraps the top-level call in a broad ``try/except``
so any exception here is logged as a warning and never propagates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import yaml

if TYPE_CHECKING:
    from .cost_tracker import CostTracker
    from .db import Database

logger = logging.getLogger(__name__)

__all__ = [
    "SprintQueueConfig",
    "TriggerResult",
    "SprintChainManager",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SprintQueueConfig:
    """Parsed sprint queue configuration.

    Attributes:
        repo:                 Repository slug (e.g. ``"owner/repo"``).
        issues:               Ordered list of GitHub issue numbers to process.
        score_threshold:      Minimum confidence score required to advance.
        daily_budget_cap_usd: Maximum daily spend in USD; ``None`` disables
                              the budget guard.
        comment_template:     f-string template for the queue comment.
                              Supports ``{previous_issue}`` and ``{next_issue}``.
    """

    repo: str
    issues: List[int]
    score_threshold: float = 0.75
    daily_budget_cap_usd: Optional[float] = None
    comment_template: str = (
        "Queued by sprint runner — previous issue #{previous_issue} merged successfully"
    )


@dataclass
class TriggerResult:
    """Result of a :meth:`SprintChainManager.trigger_next` call.

    Attributes:
        triggered:   ``True`` when the next issue was labeled ``pipeline-ready``.
        next_issue:  Issue number that was labeled (or would have been labeled
                     when a guard stopped the chain).  ``None`` when the queue
                     is exhausted or config loading failed.
        reason:      Short machine-readable reason string.  ``"ok"`` on success;
                     one of ``"score_below_threshold"``, ``"queue_exhausted"``,
                     ``"daily_budget_cap_reached"``, ``"human_paused"``,
                     ``"label_apply_failed"``, or ``"config_load_failed:<msg>"``
                     on failure.
        comment_url: URL of the comment posted on *next_issue*, or ``None``.
    """

    triggered: bool
    next_issue: Optional[int] = None
    reason: str = ""
    comment_url: Optional[str] = None


# ---------------------------------------------------------------------------
# SprintChainManager
# ---------------------------------------------------------------------------


class SprintChainManager:
    """Orchestrate post-merge sprint chain advancement.

    Intended to be called once per successful auto-merge from
    ``_dispatch_auto_merge`` in ``daemon.py``.  All external I/O (GitHub CLI,
    DB writes) is isolated into small helper methods to facilitate testing.

    Args:
        db:           :class:`~db.Database` instance for persisting chain state.
        cost_tracker: Optional :class:`~cost_tracker.CostTracker` used by the
                      budget guard.  When ``None`` the budget guard is bypassed
                      with a warning.
    """

    def __init__(
        self,
        db: "Database",
        cost_tracker: Optional["CostTracker"] = None,
    ) -> None:
        self._db = db
        self._cost_tracker = cost_tracker

    # ------------------------------------------------------------------
    # Configuration loading
    # ------------------------------------------------------------------

    def load_queue_config(self, path: str) -> SprintQueueConfig:
        """Parse a sprint_queue.yaml file into a :class:`SprintQueueConfig`.

        Args:
            path: Absolute or relative path to the YAML config file.

        Returns:
            Populated :class:`SprintQueueConfig`.

        Raises:
            FileNotFoundError: When the file does not exist.
            ValueError: When required fields are missing or invalid.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Sprint queue config not found: {path}")

        with p.open() as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, dict):
            raise ValueError(
                f"Sprint queue config must be a YAML mapping, got {type(data).__name__}"
            )

        repo = data.get("repo", "")
        if not repo:
            raise ValueError("Sprint queue config missing required 'repo' field")

        issues = data.get("issues", [])
        if not isinstance(issues, list) or not all(
            isinstance(i, int) and i > 0 for i in issues
        ):
            raise ValueError(
                "Sprint queue config 'issues' must be a list of positive integers"
            )

        daily_budget_cap_usd: Optional[float] = None
        if "daily_budget_cap_usd" in data:
            daily_budget_cap_usd = float(data["daily_budget_cap_usd"])

        return SprintQueueConfig(
            repo=repo,
            issues=issues,
            score_threshold=float(data.get("score_threshold", 0.75)),
            daily_budget_cap_usd=daily_budget_cap_usd,
            comment_template=data.get(
                "comment_template",
                "Queued by sprint runner — previous issue #{previous_issue} merged successfully",
            ),
        )

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------

    def get_next_issue(
        self,
        config: SprintQueueConfig,
        processed: List[int],
    ) -> Optional[int]:
        """Return the first issue in ``config.issues`` that is not in *processed*.

        Args:
            config:    Sprint queue configuration.
            processed: List of already-processed issue numbers.

        Returns:
            Next unprocessed issue number, or ``None`` when the queue is
            exhausted.
        """
        processed_set = set(processed)
        for issue in config.issues:
            if issue not in processed_set:
                return issue
        return None

    # ------------------------------------------------------------------
    # Guard rails
    # ------------------------------------------------------------------

    def check_score_guard(
        self,
        score: Optional[float],
        threshold: float,
    ) -> bool:
        """Return ``True`` when *score* meets or exceeds *threshold*.

        A ``None`` score always fails the guard.  A threshold of ``0.0``
        effectively disables the guard (every non-None score passes).

        Args:
            score:     Confidence score from the routing decision.
            threshold: Minimum acceptable score (from config).

        Returns:
            ``True`` when the chain is allowed to advance; ``False`` to halt.
        """
        if score is None:
            return False
        return score >= threshold

    def check_budget_guard(self, config: SprintQueueConfig) -> bool:
        """Return ``True`` when the daily spend is below *config.daily_budget_cap_usd*.

        When no cap is configured (``None``) or no cost tracker is available,
        the guard always passes (with a warning in the latter case).

        Args:
            config: Sprint queue configuration with the budget cap.

        Returns:
            ``True`` when the chain is allowed to advance; ``False`` to halt.
        """
        if config.daily_budget_cap_usd is None:
            return True
        if self._cost_tracker is None:
            logger.warning(
                "sprint_chain: no cost_tracker — skipping budget guard"
            )
            return True
        today_cost = self._cost_tracker.get_daily_cost()
        if today_cost >= config.daily_budget_cap_usd:
            logger.info(
                "sprint_chain: daily budget cap $%.4f reached (today: $%.4f) "
                "— pausing chain",
                config.daily_budget_cap_usd,
                today_cost,
            )
            return False
        return True

    def check_human_pause(self, repo: str, issue_number: int) -> bool:
        """Return ``True`` when the chain is NOT paused for *issue_number*.

        The human-pause mechanism uses a ``status='paused'`` row in the
        ``sprint_chain_state`` DB table.  A human (or external tool) can insert
        or update such a row to halt the chain before the next issue is labeled.

        Normal flow (no state row, or ``status='processed'``) returns ``True``
        (safe to proceed).

        Args:
            repo:         Repository slug.
            issue_number: The *next* issue that would be labeled.

        Returns:
            ``True`` when safe to proceed; ``False`` when explicitly paused.
        """
        state = self._db.get_sprint_chain_state(repo, issue_number)
        if state and state.get("status") == "paused":
            logger.info(
                "sprint_chain: issue #%d in %r is marked paused — chain halted",
                issue_number,
                repo,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def mark_processed(
        self,
        repo: str,
        issue_number: int,
        run_id: Optional[str],
        score: Optional[float],
    ) -> None:
        """Persist *issue_number* as processed in the ``sprint_chain_state`` table.

        Idempotent: a second call for the same ``(repo, issue_number)`` pair
        updates the existing row in place.

        Args:
            repo:         Repository slug.
            issue_number: Issue number that was just merged.
            run_id:       Pipeline run_id associated with the merge.
            score:        Confidence score at the time of processing.
        """
        self._db.upsert_sprint_chain_state(
            repo=repo,
            issue_number=issue_number,
            status="processed",
            run_id=run_id,
            score=score,
        )

    # ------------------------------------------------------------------
    # GitHub actions
    # ------------------------------------------------------------------

    def label_next_issue(self, repo: str, issue_number: int) -> bool:
        """Apply the ``pipeline-ready`` label to *issue_number* via ``gh`` CLI.

        Args:
            repo:         Repository slug.
            issue_number: GitHub issue number to label.

        Returns:
            ``True`` on success; ``False`` on failure (already logged).
        """
        from .issue_automation import add_github_label  # noqa: PLC0415

        return add_github_label(repo, issue_number, "pipeline-ready")

    def post_queue_comment(
        self,
        repo: str,
        next_issue: int,
        previous_issue: int,
        comment_template: str,
    ) -> Optional[str]:
        """Post a queue-advancement comment on *next_issue*.

        Uses ``{previous_issue}`` and ``{next_issue}`` as template variables.

        Args:
            repo:             Repository slug.
            next_issue:       Issue number being labeled.
            previous_issue:   Issue number that was just merged.
            comment_template: Template string from :class:`SprintQueueConfig`.

        Returns:
            Comment HTML URL on success; ``None`` on failure.
        """
        from .issue_automation import post_github_comment  # noqa: PLC0415

        body = comment_template.format(
            previous_issue=previous_issue,
            next_issue=next_issue,
        )
        return post_github_comment(repo=repo, issue_number=next_issue, body=body)

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def trigger_next(
        self,
        repo: str,
        current_issue: int,
        run_id: str,
        score: Optional[float],
        queue_config_path: str,
    ) -> TriggerResult:
        """Orchestrate post-merge chain advancement.

        Performs the full sequence:

        1. Load queue config from *queue_config_path*.
        2. Check the score guard.
        3. Query DB for already-processed issues.
        4. Find the next unprocessed issue.
        5. Check the daily-budget guard.
        6. Check the human-pause guard.
        7. Mark *current_issue* as processed.
        8. Label the next issue ``pipeline-ready``.
        9. Post a queue comment on the next issue.

        All guard failures return a :class:`TriggerResult` with
        ``triggered=False`` and a descriptive ``reason``.  The label-apply
        step is the only one that can set ``triggered=False`` after guards
        pass; a comment-post failure is non-fatal (``triggered=True`` still).

        Args:
            repo:              Repository slug.
            current_issue:     Issue number just merged.
            run_id:            Pipeline run_id of the merge.
            score:             Confidence score from the routing decision.
            queue_config_path: Absolute path to the sprint_queue.yaml file.

        Returns:
            :class:`TriggerResult` describing what happened.
        """
        # -- 1. Load config --
        try:
            config = self.load_queue_config(queue_config_path)
        except Exception as exc:  # noqa: BLE001
            return TriggerResult(
                triggered=False,
                reason=f"config_load_failed: {exc}",
            )

        # -- 2. Score guard --
        if not self.check_score_guard(score, config.score_threshold):
            logger.info(
                "sprint_chain: score %.4f below threshold %.4f for run %s "
                "— chain stopped",
                score or 0.0,
                config.score_threshold,
                run_id,
            )
            return TriggerResult(
                triggered=False,
                reason=f"score_below_threshold: {score} < {config.score_threshold}",
            )

        # -- 3. Get processed issues --
        try:
            processed = self._db.get_sprint_processed_issues(repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sprint_chain: DB read failed: %s", exc)
            processed = []

        # -- 4. Find next issue --
        # Treat current_issue as already-processed when scanning the queue so
        # that we advance past it even though mark_processed hasn't run yet.
        next_issue = self.get_next_issue(config, processed + [current_issue])
        if next_issue is None:
            logger.info(
                "sprint_chain: all issues in queue processed for %r", repo
            )
            return TriggerResult(triggered=False, reason="queue_exhausted")

        # -- 5. Budget guard --
        if not self.check_budget_guard(config):
            return TriggerResult(
                triggered=False,
                next_issue=next_issue,
                reason="daily_budget_cap_reached",
            )

        # -- 6. Human-pause guard --
        if not self.check_human_pause(repo, next_issue):
            return TriggerResult(
                triggered=False,
                next_issue=next_issue,
                reason="human_paused",
            )

        # -- 7. Mark current issue processed (before labeling next) --
        try:
            self.mark_processed(repo, current_issue, run_id, score)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sprint_chain: mark_processed failed (non-fatal): %s", exc
            )

        # -- 8. Label next issue --
        labeled = self.label_next_issue(repo, next_issue)
        if not labeled:
            return TriggerResult(
                triggered=False,
                next_issue=next_issue,
                reason="label_apply_failed",
            )

        # -- 9. Post comment (non-fatal) --
        comment_url: Optional[str] = None
        try:
            comment_url = self.post_queue_comment(
                repo=repo,
                next_issue=next_issue,
                previous_issue=current_issue,
                comment_template=config.comment_template,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sprint_chain: post_queue_comment failed (non-fatal): %s", exc
            )

        logger.info(
            "sprint_chain: labeled issue #%d pipeline-ready in %r "
            "(previous: #%d, score=%.4f)",
            next_issue,
            repo,
            current_issue,
            score or 0.0,
        )
        return TriggerResult(
            triggered=True,
            next_issue=next_issue,
            reason="ok",
            comment_url=comment_url,
        )
