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
import os
import re
import subprocess
import sys
import tempfile
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

    NEEDS_REVIEW = "needs_review"
    """Fix pipeline ran but confidence score is below threshold; awaits human decision."""


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


# ---------------------------------------------------------------------------
# RegressionWebhookHandler
# ---------------------------------------------------------------------------


class RegressionWebhookHandler:
    """Processes GitHub ``check_suite.completed`` webhook events.

    Wires the Sprint 2 webhook trigger framework to :class:`RegressionDetector`
    and to GitHub issue creation.

    Lifecycle:

    * **failure** conclusion → fetch last-green SHA from DB, run
      :meth:`RegressionDetector.detect`, open a GitHub issue via
      ``gh issue create``, return the :class:`Regression` object.
    * **success** conclusion → update the last-green SHA in the DB (so the
      next failure has an accurate baseline), return ``None``.
    * Any other conclusion (``cancelled``, ``neutral``, ``skipped``, …) →
      no-op, return ``None``.

    Args:
        db:          Database instance with ``store_green_sha`` /
                     ``get_last_green_sha`` / ``insert_regression``.
        git_context: :class:`~orchestration_engine.git_integration.GitContext`
                     instance (used by the detector).
        detector:    Pre-built :class:`RegressionDetector` to invoke.
        repo_path:   Filesystem path to the git repository root.
        repo_slug:   GitHub repo identifier (``"owner/repo"``) used with
                     ``gh issue create``.
    """

    def __init__(
        self,
        db: Any,
        git_context: Any,
        detector: "RegressionDetector",
        repo_path: Path,
        repo_slug: str,
    ) -> None:
        self._db = db
        self._git = git_context
        self._detector = detector
        self._repo_path = repo_path
        self._repo_slug = repo_slug

    # ------------------------------------------------------------------
    # Public entry point (matches Sprint 2 trigger interface)
    # ------------------------------------------------------------------

    def handle_ci_failure(self, event_payload: dict) -> Optional["Regression"]:
        """Process a GitHub ``check_suite.completed`` webhook payload.

        This is the entry point called by the webhook trigger framework.

        Args:
            event_payload: Parsed JSON body of the ``check_suite.completed``
                           webhook event.

        Returns:
            The created :class:`Regression` on a failure conclusion, or
            ``None`` for success (green-SHA update only), no-op conclusions,
            and error cases.
        """
        try:
            check_suite = event_payload.get("check_suite", {})
            conclusion = check_suite.get("conclusion")
            head_sha = (
                check_suite.get("head_sha")
                or check_suite.get("head_commit", {}).get("id")
            )
            ci_run_url = check_suite.get("url", "")

            if conclusion == "success":
                # Update the baseline so next failure has a valid range.
                if head_sha:
                    self._db.store_green_sha(self._repo_slug, head_sha)
                    logger.info(
                        "RegressionWebhookHandler: CI passed — stored green SHA %s for %s",
                        head_sha[:8] if head_sha else "?",
                        self._repo_slug,
                    )
                return None

            if conclusion != "failure":
                # cancelled, neutral, skipped, stale, etc. — ignore.
                logger.debug(
                    "RegressionWebhookHandler: ignoring conclusion=%r for %s",
                    conclusion,
                    self._repo_slug,
                )
                return None

            # --- CI failed ---
            if not head_sha:
                logger.warning(
                    "RegressionWebhookHandler: no head_sha in payload for %s",
                    self._repo_slug,
                )
                return None

            last_green_sha = self._db.get_last_green_sha(self._repo_slug)
            if not last_green_sha:
                logger.warning(
                    "RegressionWebhookHandler: no known-green SHA for %s; "
                    "skipping detector (no baseline yet)",
                    self._repo_slug,
                )
                return None

            # Extract CI error log from the payload (best-effort).
            ci_error_log = self._extract_ci_error_log(event_payload)

            regression = self._detector.detect(
                last_green_sha=last_green_sha,
                head_sha=head_sha,
                ci_error_log=ci_error_log,
                ci_run_url=ci_run_url,
                failure_type="ci_failure",
                repo_path=self._repo_path,
            )
            if regression is None:
                return None

            # Open a GitHub issue to surface the regression.
            issue_url = self._open_github_issue(regression)
            if issue_url:
                logger.info(
                    "RegressionWebhookHandler: opened GitHub issue %s for regression %s",
                    issue_url,
                    regression.id,
                )

            return regression

        except Exception:
            logger.exception(
                "RegressionWebhookHandler: unexpected error processing payload for %s",
                self._repo_slug,
            )
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ci_error_log(event_payload: dict) -> str:
        """Best-effort extraction of CI error text from the payload.

        Looks for common fields that carry CI output in GitHub payloads.
        Returns an empty string when none are present.

        Args:
            event_payload: The raw webhook event dict.

        Returns:
            A string that will be used for file-overlap scoring.
        """
        check_suite = event_payload.get("check_suite", {})
        # GitHub populates check_runs when check_suite contains run data.
        check_runs = check_suite.get("check_runs", []) or []
        parts: List[str] = []
        for run in check_runs:
            output = run.get("output") or {}
            for key in ("title", "summary", "text"):
                value = output.get(key) or ""
                if value:
                    parts.append(value)
        # Also include any top-level "body" or "log" field.
        for key in ("body", "log", "error_log"):
            value = event_payload.get(key) or ""
            if value:
                parts.append(value)
        return "\n".join(parts)

    def _open_github_issue(self, regression: "Regression") -> Optional[str]:
        """Open a GitHub issue for a detected regression via ``gh issue create``.

        The issue title and body include the culprit SHA, the CI run URL,
        and the list of affected files.  The ``regression-auto`` label is
        applied so issues can be filtered programmatically.

        Args:
            regression: The :class:`Regression` record to report.

        Returns:
            The URL of the newly created GitHub issue, or ``None`` on error.
        """
        affected = "\n".join(f"- `{f}`" for f in regression.affected_files) or "_unknown_"
        body = (
            f"## Regression Detected\n\n"
            f"**Culprit commit:** `{regression.commit_sha}`\n"
            f"**CI run:** {regression.ci_run_url}\n"
            f"**Regression ID:** `{regression.id}`\n\n"
            f"### Affected files\n\n"
            f"{affected}\n\n"
            f"_Opened automatically by the orchestration engine._"
        )
        title = f"[regression] CI failure — culprit commit {regression.commit_sha[:8]}"
        cmd = [
            "gh", "issue", "create",
            "--repo", self._repo_slug,
            "--title", title,
            "--body", body,
            "--label", "regression-auto",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "RegressionWebhookHandler: gh issue create failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            # gh prints the issue URL on stdout.
            return result.stdout.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning(
                "RegressionWebhookHandler: could not run gh CLI: %s",
                exc,
            )
            return None


# ---------------------------------------------------------------------------
# RegressionFixer
# ---------------------------------------------------------------------------


class RegressionFixer:
    """Spawns a coding pipeline run to fix a detected regression.

    Translates a :class:`Regression` record into a ``coding-pipeline-v1``
    pipeline launch via the ``orch launch`` CLI subprocess, updates the
    regression status to ``FIXING``, and stores the returned run_id.

    Args:
        repo_path:  Absolute path to the git repository root.
        repo_url:   GitHub repo URL (e.g. ``"https://github.com/owner/repo"``).
        repo_slug:  GitHub repo slug (``"owner/repo"``) used to build branch
                    names and issue references.
    """

    TEMPLATE_ID = "coding-pipeline-v1"
    CONFIDENCE_THRESHOLD = 0.95

    def __init__(self, repo_path: Path, repo_url: str, repo_slug: str) -> None:
        self._repo_path = repo_path
        self._repo_url = repo_url
        self._repo_slug = repo_slug

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def spawn_fix(
        self,
        regression: "Regression",
        db: Any,
        db_path: Optional[Path],
    ) -> Optional[str]:
        """Build a fix input, launch ``coding-pipeline-v1``, and update the regression.

        Steps:
        1. Build the pipeline input dict via :meth:`_build_fix_input`.
        2. Launch the pipeline subprocess via :meth:`_launch_pipeline`.
        3. On success, update the :class:`Regression` DB record:
           ``status → FIXING``, ``fix_run_id → run_id``,
           ``fix_attempt_count → previous_count + 1``.

        Args:
            regression: The :class:`Regression` to fix.
            db:         Database instance for updating the regression record.
            db_path:    Filesystem path to the DB file; forwarded to
                        ``orch launch --db-path`` so the spawned daemon writes
                        to the same DB.

        Returns:
            The ``run_id`` string returned by ``orch launch``, or ``None`` on
            any failure (subprocess error, parse failure, DB update failure).
        """
        fix_input = self._build_fix_input(regression)
        run_id = self._launch_pipeline(fix_input, db_path)
        if run_id is None:
            logger.warning(
                "RegressionFixer: pipeline launch failed for regression %s",
                regression.id,
            )
            return None

        try:
            db.update_regression(
                regression.id,
                status=RegressionStatus.FIXING.value,
                fix_run_id=run_id,
                fix_attempt_count=regression.fix_attempt_count + 1,
            )
        except Exception:
            logger.exception(
                "RegressionFixer: DB update failed for regression %s",
                regression.id,
            )
            return None

        logger.info(
            "RegressionFixer: spawned fix run %s for regression %s "
            "(attempt #%d)",
            run_id,
            regression.id,
            regression.fix_attempt_count + 1,
        )
        return run_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_fix_input(self, regression: "Regression") -> dict:
        """Construct the pipeline input payload for a fix run.

        The branch name follows the convention
        ``fix/regression-{commit_sha[:8]}-{regression_id[:8]}``.

        Args:
            regression: The regression to build input for.

        Returns:
            Dict suitable for JSON-serialisation and passing to ``orch launch``
            via ``--input-file``.
        """
        branch = (
            f"fix/regression-{regression.commit_sha[:8]}-{regression.id[:8]}"
        )
        affected = (
            "\n".join(f"- {f}" for f in regression.affected_files)
            or "_unknown_"
        )
        task_description = (
            f"Fix a regression introduced by commit `{regression.commit_sha}`.\n\n"
            f"**Failure type:** {regression.failure_type}\n"
            f"**CI run:** {regression.ci_run_url}\n\n"
            f"**Affected files:**\n{affected}\n"
        )
        if regression.diagnosis:
            task_description += f"\n**Diagnosis:**\n{regression.diagnosis}\n"

        return {
            "task_description": task_description,
            "branch_name": branch,
            "repo_url": self._repo_url,
            "repo_path": str(self._repo_path),
            "regression_id": regression.id,
            "affected_files": regression.affected_files,
        }

    def _launch_pipeline(
        self,
        fix_input: dict,
        db_path: Optional[Path],
    ) -> Optional[str]:
        """Write fix_input to a temp file, invoke ``orch launch``, return run_id.

        The subprocess is invoked as
        ``python -m orchestration_engine.cli launch coding-pipeline-v1
        --input-file <tmp> [--db-path <db_path>]``.

        The temp file is always removed after the subprocess returns (even on
        failure or exception).

        Args:
            fix_input: Dict to serialise as the pipeline input.
            db_path:   Optional path forwarded as ``--db-path`` to the CLI.

        Returns:
            The parsed run_id string, or ``None`` on any error.
        """
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
                prefix="regression-fix-",
            ) as tmp:
                json.dump(fix_input, tmp)
                tmp_path = tmp.name
        except OSError:
            logger.exception("RegressionFixer: could not write temp input file")
            return None

        cmd = [
            sys.executable,
            "-m",
            "orchestration_engine.cli",
            "launch",
            self.TEMPLATE_ID,
            "--input-file",
            tmp_path,
        ]
        if db_path is not None:
            cmd.extend(["--db-path", str(db_path)])

        env = os.environ.copy()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self._repo_path),
                env=env,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("RegressionFixer: subprocess error: %s", exc)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if result.returncode != 0:
            logger.warning(
                "RegressionFixer: orch launch failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return None

        return self._parse_run_id(result.stdout)

    def handle_fix_completion(
        self, regression_id: str, fix_run: dict, db: Any
    ) -> str:
        """Evaluate a completed fix run and auto-merge or flag for review.

        Reads ``scoring_score`` and ``scoring_status`` from *fix_run*.  If
        ``score >= CONFIDENCE_THRESHOLD`` **and** ``scoring_status == "passed"``
        the PR is merged automatically and the regression is marked ``FIXED``.
        On merge failure or when the gate does not pass the regression is
        marked ``NEEDS_REVIEW``.

        Args:
            regression_id: UUID of the regression record.
            fix_run:       Dict with at least ``scoring_score`` (float | None)
                           and ``scoring_status`` (str | None) keys.
            db:            Database instance (``get_regression`` /
                           ``update_regression``).

        Returns:
            The new status string (``"fixed"`` or ``"needs_review"``).
        """
        score = fix_run.get("scoring_score")
        scoring_status = fix_run.get("scoring_status")

        gate_passed = (
            score is not None
            and score >= self.CONFIDENCE_THRESHOLD
            and scoring_status == "passed"
        )

        logger.info(
            "RegressionFixer.handle_fix_completion: regression=%s score=%s "
            "scoring_status=%r gate_passed=%s",
            regression_id,
            score,
            scoring_status,
            gate_passed,
        )

        if gate_passed:
            # Reconstruct branch name from the regression record.
            try:
                regression_record = db.get_regression(regression_id)
            except Exception:
                logger.exception(
                    "RegressionFixer.handle_fix_completion: could not fetch "
                    "regression %s from DB; falling back to needs_review",
                    regression_id,
                )
                self._safe_update_db(db, regression_id, RegressionStatus.NEEDS_REVIEW.value)
                return RegressionStatus.NEEDS_REVIEW.value

            if not regression_record:
                logger.warning(
                    "RegressionFixer.handle_fix_completion: regression %s not "
                    "found in DB; falling back to needs_review",
                    regression_id,
                )
                self._safe_update_db(db, regression_id, RegressionStatus.NEEDS_REVIEW.value)
                return RegressionStatus.NEEDS_REVIEW.value

            commit_sha = regression_record.get("commit_sha", "")
            branch = (
                f"fix/regression-{commit_sha[:8]}-{regression_id[:8]}"
            )
            logger.info(
                "RegressionFixer.handle_fix_completion: attempting PR merge "
                "for branch %r (regression %s)",
                branch,
                regression_id,
            )
            merged = self._merge_pr(branch)
            if not merged:
                logger.warning(
                    "RegressionFixer.handle_fix_completion: PR merge failed "
                    "for branch %r; marking needs_review",
                    branch,
                )
                gate_passed = False

        new_status = (
            RegressionStatus.FIXED.value
            if gate_passed
            else RegressionStatus.NEEDS_REVIEW.value
        )
        logger.info(
            "RegressionFixer.handle_fix_completion: updating regression %s → %s",
            regression_id,
            new_status,
        )
        self._safe_update_db(db, regression_id, new_status)
        return new_status

    @staticmethod
    def _safe_update_db(db: Any, regression_id: str, status: str) -> None:
        """Update the regression status in the DB, swallowing any exception.

        Args:
            db:            Database instance.
            regression_id: ID of the regression to update.
            status:        New status string.
        """
        try:
            db.update_regression(regression_id, status=status)
        except Exception:
            logger.exception(
                "RegressionFixer._safe_update_db: DB update failed for "
                "regression %s (status=%r); ignoring",
                regression_id,
                status,
            )

    @staticmethod
    def _parse_run_id(stdout: str) -> Optional[str]:
        """Extract the run_id from ``orch launch`` stdout.

        The ``orch launch`` command prints a line of the form::

            Run ID:  <run_id>

        Args:
            stdout: The full stdout text from the ``orch launch`` subprocess.

        Returns:
            The run_id string (stripped), or ``None`` if the expected line is
            not present.
        """
        for line in stdout.splitlines():
            if "Run ID:" in line:
                parts = line.split("Run ID:", 1)
                if len(parts) == 2:
                    run_id = parts[1].strip()
                    if run_id:
                        return run_id
        logger.warning(
            "RegressionFixer: could not parse run_id from stdout: %r",
            stdout[:200],
        )
        return None

    def _merge_pr(self, branch: str) -> bool:
        """Merge the fix PR for *branch* via ``gh pr merge --squash``.

        Args:
            branch: Branch name (e.g. ``fix/regression-abcdef12-12345678``).

        Returns:
            ``True`` if the merge command succeeded (rc=0), ``False`` otherwise.
        """
        cmd = [
            "gh", "pr", "merge", branch,
            "--squash",
            "--repo", self._repo_slug,
            "--yes",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self._repo_path),
            )
            if result.returncode != 0:
                logger.warning(
                    "RegressionFixer: gh pr merge failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False
            logger.info(
                "RegressionFixer: PR merged for branch %r", branch
            )
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                "RegressionFixer: gh pr merge timed out for branch %r", branch
            )
            return False
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "RegressionFixer: could not run gh CLI for branch %r: %s",
                branch,
                exc,
            )
            return False


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def register_regression_trigger(
    db: Any,
    trigger_id: str,
    template_id: str,
) -> Any:
    """Create and persist a TriggerConfig wiring ``check_suite.completed`` events.

    The trigger filters on the ``check_suite.completed`` GitHub event and
    is set to ``fire_and_forget`` mode so the webhook endpoint responds
    immediately without waiting for the regression pipeline to finish.

    The ``input_map`` passes the full raw payload through as ``event_payload``
    so that :meth:`RegressionWebhookHandler.handle_ci_failure` receives it.

    Args:
        db:         Database instance — the trigger is persisted via
                    ``db.create_trigger``.
        trigger_id: Unique trigger identifier (must satisfy TriggerConfig
                    validation: 3-64 chars, alphanumeric/hyphens/underscores).
        template_id: Pipeline template ID to associate with the trigger.

    Returns:
        The created :class:`~orchestration_engine.webhooks.TriggerConfig`
        instance.

    Raises:
        TriggerValidationError: If *trigger_id* or *template_id* fails
            TriggerConfig validation.
        sqlite3.IntegrityError: If a trigger with *trigger_id* already exists.
    """
    from orchestration_engine.webhooks import TriggerConfig  # local import to avoid circular deps

    trigger = TriggerConfig(
        id=trigger_id,
        template_id=template_id,
        mode="fire_and_forget",
        filters=[{"action": "completed"}],
        input_map={"event_payload": "{{payload}}"},
    )
    db.create_trigger(trigger.to_dict())
    logger.info(
        "register_regression_trigger: created trigger %r → template %r",
        trigger_id,
        template_id,
    )
    return trigger
