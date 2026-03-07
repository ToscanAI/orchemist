"""Regression data model for the Orchestration Engine.

Provides types for tracking CI regression events detected on main:
what commit caused the break, which files are affected, diagnosis summary,
fix attempt status, and resolution lifecycle.

Also provides :class:`RegressionDetector` which correlates CI error logs with
the git commit history to identify the most likely breaking commit.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RegressionStatus(str, Enum):
    """Lifecycle status of a detected regression.

    Inherits from ``str`` so values can be stored/compared as plain strings
    (consistent with the rest of the codebase, e.g. ``FailureClass``,
    ``TaskState``).
    """

    DETECTED = "detected"
    """Regression identified; no diagnosis or fix attempted yet."""

    DIAGNOSING = "diagnosing"
    """Automated diagnosis is running to classify the failure."""

    FIXING = "fixing"
    """A fix pipeline run has been spawned."""

    FIXED = "fixed"
    """Fix was applied and verified; regression resolved."""

    ESCALATED = "escalated"
    """Automated fix failed or was not feasible; escalated to human."""


@dataclass
class Regression:
    """A detected regression event on the main branch.

    Attributes:
        id:                Unique UUID for this regression record.
        commit_sha:        Git SHA of the commit that introduced the regression.
        ci_run_url:        URL of the failing CI run (GitHub Actions, etc.).
        failure_type:      Short label classifying the failure (e.g. 'test_failure',
                           'build_error', 'lint_error').
        affected_files:    List of file paths implicated in the failure.
        diagnosis:         Human-readable or LLM-produced diagnosis summary.
        fix_run_id:        run_id of the spawned fix pipeline run (if any).
        status:            Current lifecycle status (see RegressionStatus).
        fix_attempt_count: Number of fix pipeline runs spawned so far.
        created_at:        UTC datetime when the regression was first detected.
    """

    commit_sha: str
    ci_run_url: str
    failure_type: str

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    affected_files: List[str] = field(default_factory=list)
    diagnosis: Optional[str] = field(default=None)
    fix_run_id: Optional[str] = field(default=None)
    status: RegressionStatus = field(default=RegressionStatus.DETECTED)
    fix_attempt_count: int = field(default=0)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for DB insertion.

        JSON-encodes ``affected_files`` so it can be stored as a TEXT column
        and round-tripped via ``Database._row_to_dict``.

        Returns:
            Dict with all fields serialised to DB-compatible types.
        """
        return {
            "id": self.id,
            "commit_sha": self.commit_sha,
            "ci_run_url": self.ci_run_url,
            "failure_type": self.failure_type,
            "affected_files": json.dumps(self.affected_files),
            "diagnosis": self.diagnosis,
            "fix_run_id": self.fix_run_id,
            "status": self.status.value if isinstance(self.status, RegressionStatus) else self.status,
            "fix_attempt_count": self.fix_attempt_count,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# RegressionDetector
# ---------------------------------------------------------------------------


class RegressionDetector:
    """Identifies the most likely breaking commit in a CI failure range.

    Algorithm:

    1. Enumerate the commits in ``last_green_sha..head_sha`` via
       :meth:`~orchestration_engine.git_integration.GitContext.get_commit_range`.
    2. For each commit, retrieve the files it touched via
       :meth:`~orchestration_engine.git_integration.GitContext.get_commit_files`.
    3. Score each commit by file-path overlap with the CI error log text.
    4. Persist a :class:`Regression` record pointing at the top-scoring commit
       and return it.

    If no commits are found in the range, ``detect`` returns ``None`` and logs
    a warning.  All git failures are handled gracefully (soft failure → empty
    list), so the detector never raises due to transient git errors.

    Args:
        db:          :class:`~orchestration_engine.db.Database` instance for
                     persisting regression records.
        git_context: A
                     :class:`~orchestration_engine.git_integration.GitContext`
                     instance (only the two new helper methods are used; the
                     instance does not need an active feature branch).
    """

    def __init__(self, db: Any, git_context: Any) -> None:
        self._db = db
        self._git = git_context

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        last_green_sha: str,
        head_sha: str,
        ci_error_log: str,
        ci_run_url: str,
        failure_type: str,
        repo_path: Path,
    ) -> Optional["Regression"]:
        """Detect the likely breaking commit and persist a Regression record.

        Args:
            last_green_sha: SHA of the last known-green commit (exclusive range
                            start, as in ``git log A..B``).
            head_sha:       SHA of the failing HEAD commit (inclusive range end).
            ci_error_log:   Raw CI log text used for file-overlap scoring.
            ci_run_url:     URL of the failing CI run (e.g. GitHub Actions URL).
            failure_type:   Short label for the failure category (e.g.
                            ``"test_failure"``, ``"build_error"``).
            repo_path:      Path to the git repository root.

        Returns:
            A :class:`Regression` dataclass instance that has been persisted to
            the database, or ``None`` if no commits were found in the range.
        """
        shas = self._git.get_commit_range(last_green_sha, head_sha, repo_path)
        if not shas:
            logger.warning(
                "RegressionDetector: no commits found in range %s..%s",
                last_green_sha,
                head_sha,
            )
            return None

        best_sha, best_files, best_score = self._find_best_commit(
            shas, ci_error_log, repo_path
        )

        regression = Regression(
            commit_sha=best_sha,
            ci_run_url=ci_run_url,
            failure_type=failure_type,
            affected_files=best_files,
            status=RegressionStatus.DETECTED,
        )
        self._db.insert_regression(regression.to_dict())
        logger.info(
            "RegressionDetector: persisted regression %s → commit %s "
            "(score=%d, affected_files=%d)",
            regression.id,
            best_sha[:8],
            best_score,
            len(best_files),
        )
        return regression

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_best_commit(
        self,
        shas: List[str],
        ci_error_log: str,
        repo_path: Path,
    ) -> Tuple[str, List[str], int]:
        """Score each commit in *shas* and return the top-scoring one.

        When multiple commits share the highest score the newest one (first in
        *shas*, which is git-log newest-first order) is preferred.

        Args:
            shas:         Commit SHAs ordered newest-first.
            ci_error_log: Raw CI error log text.
            repo_path:    Repository root path.

        Returns:
            Tuple of ``(sha, files_changed, score)`` for the best commit.
        """
        best_sha: str = shas[0]
        best_files: List[str] = []
        best_score: int = -1

        for sha in shas:
            files = self._git.get_commit_files(sha, repo_path)
            score = self._score_commit(files, ci_error_log)
            logger.debug(
                "RegressionDetector: commit %s score=%d files=%s",
                sha[:8],
                score,
                files,
            )
            if score > best_score:
                best_score = score
                best_sha = sha
                best_files = files

        return best_sha, best_files, best_score

    @staticmethod
    def _score_commit(files: List[str], ci_error_log: str) -> int:
        """Count how many of *files* appear as substrings in the error log.

        Extracts file-path-like tokens from ``ci_error_log`` using a regex
        that matches common source-file extensions, then checks each commit
        file against those tokens.  A file scores a point if its basename or
        full path overlaps with at least one log token.

        Args:
            files:        List of file paths touched by the commit.
            ci_error_log: Raw CI error log text.

        Returns:
            Non-negative integer overlap count.  ``0`` if either list is empty
            or no overlap is found.
        """
        if not files or not ci_error_log:
            return 0

        # Extract source-file-like tokens from the log
        log_tokens: set = set(
            re.findall(
                r"[\w/\-\.]+\.(?:py|js|ts|go|java|rb|cpp|c|h|"
                r"yaml|yml|json|md|sh|txt|rs|kt|cs|swift)",
                ci_error_log,
            )
        )
        if not log_tokens:
            return 0

        score = 0
        for f in files:
            fname = Path(f).name  # basename only (e.g. "scorer.py")
            for token in log_tokens:
                token_basename = Path(token).name
                if fname == token_basename or f == token or token == f:
                    score += 1
                    break  # each file scores at most 1 point

        return score
