"""Daemon GitHub-result hook and self-healing regression-fix dispatch.

Posts pipeline outcomes back to the originating GitHub issue
(:func:`_post_github_result_hook`) and gates self-healing regression fixes
through SafetyGuard before spawning them
(:func:`dispatch_regression_fix_safely`).  Extracted verbatim from
:mod:`orchestration_engine.daemon` (wave c of #1034); the public surface is
re-exported by the package facade, so callers continue to import these names
from ``orchestration_engine.daemon``.
"""

# ruff: noqa: E501

import logging
from typing import Any, Dict, Optional

from ._events import _extract_repo_slug

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Self-healing regression fix dispatch (Issue #429.4)
# ---------------------------------------------------------------------------


def dispatch_regression_fix_safely(
    regression: Any,
    db: Any,
    db_path: Any,
    fixer: Any,
) -> Optional[str]:
    """Gate a regression fix attempt through SafetyGuard then spawn via RegressionFixer.

    This is the authoritative fix-dispatch path for the self-healing chain.
    It must be called instead of calling :meth:`~regression.RegressionFixer.spawn_fix`
    directly so that SafetyGuard loop-prevention and exclusion checks are always
    enforced.

    Steps:

    1. Instantiate :class:`~regression.SafetyGuard` and call
       :meth:`~regression.SafetyGuard.should_attempt_fix`.
    2. If the guard blocks the attempt, update the regression status to
       ``ESCALATED`` and return ``None``.
    3. If the guard allows the attempt, delegate to
       :meth:`~regression.RegressionFixer.spawn_fix` and return the run_id.

    All DB failures inside the guard status update are caught and logged so
    that a DB error never prevents the method from returning cleanly.

    Args:
        regression: A :class:`~regression.Regression` instance describing the
                    detected failure.
        db:         Database instance (used by SafetyGuard for oscillation
                    checks and by the fixer for regression status updates).
        db_path:    Filesystem path to the DB file forwarded to the spawned
                    daemon process via ``--db-path``.  May be ``None`` when
                    running in-memory.
        fixer:      A :class:`~regression.RegressionFixer` instance used to
                    launch the fix pipeline subprocess.

    Returns:
        The ``run_id`` string of the spawned fix pipeline, or ``None`` when
        the SafetyGuard blocked the attempt or when the pipeline launch failed.
    """
    from ..regression import RegressionStatus, SafetyGuard  # noqa: PLC0415

    guard = SafetyGuard()
    allowed, reason = guard.should_attempt_fix(regression, db)
    if not allowed:
        logger.warning(
            "dispatch_regression_fix_safely: SafetyGuard blocked fix attempt "
            "for regression %s: %s",
            regression.id,
            reason,
        )
        try:
            db.update_regression(
                regression.id,
                status=RegressionStatus.ESCALATED.value,
            )
        except Exception as _ue:  # noqa: BLE001
            logger.warning(
                "dispatch_regression_fix_safely: could not update regression %s "
                "to ESCALATED (non-fatal): %s",
                regression.id,
                _ue,
            )
        return None

    logger.info(
        "dispatch_regression_fix_safely: SafetyGuard approved fix attempt "
        "for regression %s — spawning fix pipeline",
        regression.id,
    )
    return fixer.spawn_fix(regression, db, db_path)


# ---------------------------------------------------------------------------
# _post_github_result_hook — post pipeline outcome back to GitHub issue
# ---------------------------------------------------------------------------


def _post_github_result_hook(  # noqa: C901
    run_id: str,
    db: Any,
    initial_input: Dict[str, Any],
    phase_outputs: Dict[str, Any],
    final_status: str,
    error_message: Optional[str],
    diagnosis: Any,
    output_dir: Any,  # noqa: ARG001
    template_category: str = "",
) -> None:
    """Post pipeline results back to the originating GitHub issue (Issue #5.1.4).

    Resolves the triggering issue context from *initial_input* and the DB,
    then dispatches to the appropriate :mod:`issue_automation` function based
    on *final_status*, *template_category*, and the pipeline's
    ``classification_type``.

    For non-code template categories (Issue #578):

    - ``content`` / ``docs`` success → :func:`~orchestration_engine.issue_automation.create_content_pr`
      (no issue_number required; PR title uses ``content: {topic}`` format)
    - ``content`` / ``docs`` failure → skipped (no orphan comments)
    - ``research`` → always skipped (no PR, no comment)

    For code / empty / unrecognised categories (backward-compatible):

    - ``failed`` → :func:`~orchestration_engine.issue_automation.post_failure_summary_comment`
    - ``bug`` / ``feature`` / ``refactor`` (success) → :func:`~orchestration_engine.issue_automation.create_pr_for_issue`
    - ``content`` / ``docs`` / ``research`` classification (success) → :func:`~orchestration_engine.issue_automation.post_pipeline_result_comment`

    All exceptions are caught and logged as warnings so this hook is
    completely non-fatal — the pipeline run has already been persisted with
    its final status before this function is called.

    Args:
        run_id:            Pipeline run ID.
        db:                Open :class:`~orchestration_engine.db.Database` instance.
        initial_input:     Parsed ``input_json`` for the run (issue_number, repo, …).
        phase_outputs:     Phase output dict keyed by phase ID.
        final_status:      Terminal status string (e.g. ``"success"``, ``"failed"``).
        error_message:     Error/abort message string, or ``None`` on success.
        diagnosis:         Diagnosis result object/dict, or ``None``.
        output_dir:        Pipeline output directory (:class:`pathlib.Path`).
        template_category: Template ``category`` field value (Issue #578).
                           Defaults to ``""`` (backward-compatible coding path).
    """
    try:
        from ..issue_automation import (  # noqa: PLC0415
            create_content_pr,
            create_pr_for_issue,
            post_failure_summary_comment,
            post_pipeline_result_comment,
        )

        # --- Non-code category dispatch (Issue #578) ---
        # Must run BEFORE the issue_number guard so content/docs pipelines
        # without an issue_number are not silently dropped.
        _NON_CODE_CATEGORIES = frozenset({"content", "research", "docs"})  # noqa: N806
        _CONTENT_PR_CATEGORIES = frozenset({"content", "docs"})  # noqa: N806
        _normalised_category = (template_category or "").lower().strip()

        if _normalised_category in _NON_CODE_CATEGORIES:
            # Research pipelines: no PR, no comment — always skip.
            if _normalised_category == "research":
                logger.debug(
                    "_post_github_result_hook: research pipeline run='%s' — "
                    "skipping postflight (no PR, no comment)",
                    run_id,
                )
                return

            # Content / docs failure: no orphan comments, no PR.
            if final_status == "failed":
                logger.debug(
                    "_post_github_result_hook: %s pipeline run='%s' failed — "
                    "skipping postflight (no comment)",
                    _normalised_category,
                    run_id,
                )
                return

            # Content / docs success: create a PR (no issue_number needed).
            _content_repo: str = _extract_repo_slug(
                initial_input.get("repo_url", "") or initial_input.get("repo", "")
            )
            _content_branch: str = (
                initial_input.get("branch_name") or initial_input.get("branch") or ""
            )
            if not _content_repo or not _content_branch:
                logger.debug(
                    "_post_github_result_hook: %s pipeline run='%s' missing "
                    "repo=%r or branch_name=%r — skipping PR",
                    _normalised_category,
                    run_id,
                    _content_repo,
                    _content_branch,
                )
                return

            _content_topic: str = (
                initial_input.get("topic")
                or initial_input.get("title")
                or initial_input.get("doc_title")
                or f"pipeline run {run_id}"
            )
            _last_text = ""
            if phase_outputs:
                _last_phase = list(phase_outputs.values())[-1]
                _last_text = (_last_phase.get("output", "") or _last_phase.get("text", "") or "")[
                    :500
                ]
            _pr_body = _last_text or f"Automated content pipeline run `{run_id}`."

            _pr_prefix = "docs" if _normalised_category == "docs" else "content"

            try:
                url = create_content_pr(
                    repo=_content_repo,
                    branch_name=_content_branch,
                    topic=_content_topic,
                    body=_pr_body,
                    run_id=run_id,
                    issue_number=initial_input.get("issue_number"),
                    prefix=_pr_prefix,
                )
                if url:
                    logger.info("_post_github_result_hook: content PR created → %s", url)
                else:
                    logger.warning(
                        "_post_github_result_hook: content PR creation returned None"
                        " for run='%s'",
                        run_id,
                    )
            except Exception as _pr_exc:  # noqa: BLE001
                logger.warning(
                    "_post_github_result_hook: content PR creation failed (non-fatal)"
                    " for run='%s': %s",
                    run_id,
                    _pr_exc,
                )
            return

        # --- Existing code path (category is empty / 'code' / unrecognised) ---
        # Resolve issue context.
        issue_number: Optional[int] = initial_input.get("issue_number")
        repo: str = _extract_repo_slug(
            initial_input.get("repo_url", "") or initial_input.get("repo", "")
        )

        if not issue_number or not repo:
            # Not triggered by a GitHub issue — nothing to post.
            logger.debug("_post_github_result_hook: no issue_number/repo in input — skipping")
            return

        issue_number = int(issue_number)

        # --- Look up classification_type via DB (more authoritative than input) ---
        classification_type: Optional[str] = None
        ipm_row_id: Optional[int] = None
        try:
            ipm_row = db.get_issue_classification_by_run_id(run_id)
            if ipm_row:
                classification_type = ipm_row.get("classification_type")
                ipm_row_id = ipm_row.get("id")
        except Exception as _db_exc:  # noqa: BLE001
            logger.warning("_post_github_result_hook: DB lookup failed (non-fatal): %s", _db_exc)

        # Fall back to initial_input if DB lookup missed.
        if not classification_type:
            classification_type = initial_input.get("classification_type", "feature")

        # --- Dispatch ---
        if final_status == "failed":
            url = post_failure_summary_comment(
                repo=repo,
                issue_number=issue_number,
                error_message=error_message or "Unknown error",
                run_id=run_id,
                diagnosis=diagnosis,
            )
            if url:
                logger.info("_post_github_result_hook: failure comment posted → %s", url)
                if ipm_row_id is not None:
                    db.update_issue_classification_status(ipm_row_id, "completed")
            else:
                logger.warning("_post_github_result_hook: failure comment post returned None")

        elif classification_type in ("bug", "feature", "refactor"):
            # Code pipeline — open a PR.
            branch_name: str = (
                initial_input.get("branch_name")
                or initial_input.get("feature_branch")
                or f"feat/issue-{issue_number}"
            )
            pr_title = initial_input.get("pr_title") or f"Pipeline result for #{issue_number}"
            # Collect a short summary from the last phase output.
            _last_text = ""
            if phase_outputs:
                _last_phase = list(phase_outputs.values())[-1]
                _last_text = (_last_phase.get("output", "") or _last_phase.get("text", "") or "")[
                    :500
                ]
            _base_body = _last_text or f"Automated result from pipeline run `{run_id}`."
            # Include Closes #N so GitHub auto-links the issue (Issue #578).
            pr_body = f"{_base_body}\n\nCloses #{issue_number}"

            # --- Safety net: ensure branch exists on remote before PR creation ---
            # Sub-agents sometimes commit locally without pushing.  Push now so
            # `gh pr create` doesn't fail with "No commits between main and branch".
            from ..postflight import ensure_branch_pushed  # noqa: PLC0415

            _repo_path = initial_input.get("repo_path", "")
            if _repo_path:
                _pushed = ensure_branch_pushed(_repo_path, branch_name)
                if not _pushed:
                    logger.warning(
                        "_post_github_result_hook: ensure_branch_pushed failed for %r"
                        " — skipping PR creation",
                        branch_name,
                    )
                    return
            else:
                logger.debug(
                    "_post_github_result_hook: no repo_path in input — skipping "
                    "ensure_branch_pushed"
                )

            url = create_pr_for_issue(
                repo=repo,
                issue_number=issue_number,
                branch_name=branch_name,
                title=pr_title,
                body=pr_body,
            )
            if url:
                logger.info("_post_github_result_hook: PR created → %s", url)
                if ipm_row_id is not None:
                    db.update_issue_classification_status(ipm_row_id, "completed")
            else:
                logger.warning("_post_github_result_hook: PR creation returned None")

        elif classification_type in ("content", "docs", "research"):
            # Non-code pipeline — post output as comment (truncated to 65k chars).
            _result_text = ""
            if phase_outputs:
                _last_phase = list(phase_outputs.values())[-1]
                _result_text = _last_phase.get("output", "") or _last_phase.get("text", "") or ""
            _result_text = (_result_text or "*(No output text captured.)*")[:65_000]
            url = post_pipeline_result_comment(
                repo=repo,
                issue_number=issue_number,
                classification_type=classification_type,
                result_text=_result_text,
                run_id=run_id,
            )
            if url:
                logger.info("_post_github_result_hook: result comment posted → %s", url)
                if ipm_row_id is not None:
                    db.update_issue_classification_status(ipm_row_id, "completed")
            else:
                logger.warning("_post_github_result_hook: result comment post returned None")
        else:
            logger.debug(
                "_post_github_result_hook: unrecognised classification_type=%r — skipping",
                classification_type,
            )

    except Exception as exc:  # noqa: BLE001
        logger.warning("_post_github_result_hook: unexpected error (non-fatal): %s", exc)
