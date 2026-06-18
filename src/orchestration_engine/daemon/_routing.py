"""Daemon confidence-routing / auto-merge / post-pipeline-review cluster.

Confidence computation, routing-action dispatch, PR auto-merge (with repo
allowlist / protected-branch / dry-run guards), sprint-chain advancement, the
post-pipeline review-analysis + reviewer-calibration step, and the reject-comment
poster.  Extracted verbatim from :mod:`orchestration_engine.daemon` (wave d of
#1034); the public surface is re-exported by the package facade, so callers
continue to import these names from ``orchestration_engine.daemon``.

Patch hazard (#1041): several of these functions are monkeypatched by tests on
the facade path (``orchestration_engine.daemon._dispatch_auto_merge`` /
``_dispatch_routing_action``) while *also* being called by a sibling function in
this same module.  A bare-name intra-module call would bind to this module's own
copy and bypass the facade patch.  To preserve the patch semantics, those two
intra-cluster call sites late-bind through the facade via
``import orchestration_engine.daemon as _d`` and call ``_d.<fn>(...)`` — the
attribute is read at call time, so the in-progress module object returned during
``daemon/__init__`` import is harmless (mirrors the #942 ``_cli.`` / 950b pattern).
Intra-cluster calls to NON-patched siblings stay as bare names.
"""

# ruff: noqa: E501

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import orchestration_engine.daemon as _d

from ..output_utils import (
    extract_output_text as _extract_output_text,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _do_auto_merge(
    run_id: str,
    auto_merge_config: Any,
    scoring_score: Optional[float],
) -> None:
    """Execute the actual PR merge for a run.

    Extracted from ``_try_auto_merge`` to allow both the legacy criteria-based
    path and the new confidence-routing path (Issue #331.3) to share the same
    merge execution logic.

    Loads the gate file, resolves the branch name, and calls
    ``_GitContext.auto_merge_pr``.  Failures are logged and re-raised so
    the caller can decide whether to swallow them.

    Args:
        run_id:            The pipeline run identifier.
        auto_merge_config: The AutoMergeConfig instance (for strategy).  When
                           ``None``, a default strategy of ``"merge"`` is used.
        scoring_score:     The scoring score used for the log message.  May be
                           ``None`` when called from the routing path.
    """
    from ..git_integration import GitContext as _GitContext  # noqa: PLC0415

    strategy = auto_merge_config.strategy if auto_merge_config else "merge"
    score_str = f"{scoring_score:.4f}" if scoring_score is not None else "n/a"

    gate_data = _GitContext.load_gate(run_id)
    if gate_data is None:
        logger.warning(
            "Auto-merge: no gate file found for run '%s' — "
            "cannot determine branch name.  Is git.enabled=true in the template?",
            run_id,
        )
        return

    branch_name = gate_data.get("branch", "")
    if not branch_name:
        logger.warning(
            "Auto-merge: gate file for run '%s' has no 'branch' field — skipping.",
            run_id,
        )
        return

    logger.info(
        "Auto-merge TRIGGERED for run '%s': score=%s, branch='%s', strategy='%s'.",
        run_id,
        score_str,
        branch_name,
        strategy,
    )

    _GitContext.auto_merge_pr(
        run_id=run_id,
        branch_name=branch_name,
        strategy=strategy,
    )

    # Update gate status to merged
    try:
        _GitContext.update_gate_status(
            run_id,
            status="merged",
            message=f"Auto-merged by orchestrator (score={score_str})",
        )
    except Exception as _ge:  # noqa: BLE001
        logger.warning("Auto-merge: could not update gate status: %s", _ge)


def _is_review_phase(phase_id: str, phase_result: dict) -> bool:
    """Return True if *phase_id* / *phase_result* represents a review phase.

    Mirrors :meth:`~confidence.ConfidenceCalculator._is_review_task` but
    operates on daemon-level phase identifiers and result dicts instead of
    task-file names.

    A phase is classified as a review phase when:
    - Its ``task_type`` field is ``"review"`` or ``"judge"``, OR
    - The phase_id contains the substring ``"review"`` (case-insensitive).

    Args:
        phase_id:     Identifier of the phase (e.g. ``"review"``, ``"qa"``).
        phase_result: Phase result dict as stored in phase_outputs.

    Returns:
        ``True`` if the phase is a review/judge phase, ``False`` otherwise.
    """
    task_type = phase_result.get("task_type", "")
    if task_type in ("review", "judge"):
        return True
    if "review" in phase_id.lower():
        return True
    return False


def _strategy_to_action(strategy: str) -> str:
    """Map a RoutingTier strategy string to a dispatch action.

    Mapping:
        "merge"        → "auto_merge"
        "reject"       → "reject"
        everything else → "human_review"  (queue_review, retry, review, unrouted)

    Args:
        strategy: Strategy string from a :class:`~routing.RoutingTier`.

    Returns:
        One of ``"auto_merge"``, ``"reject"``, or ``"human_review"``.
    """
    if strategy == "merge":
        return "auto_merge"
    if strategy == "reject":
        return "reject"
    return "human_review"


class _PromptExecutorAdapter:
    """Adapter bridging the string-prompt executor interface expected by
    :class:`~audit.AuditPhase` and the :class:`~runner.TaskExecutor` ABC used
    by :class:`~pipeline_runner.PipelineRunner`.

    ``AuditPhase._call_executor`` calls ``executor.execute(prompt: str) -> str``.
    ``TaskExecutor.execute`` expects ``(task: TaskSpec, worker_id: str, ...)``.
    This adapter wraps a ``TaskExecutor`` and exposes the simple string interface
    by constructing a minimal ``TaskSpec`` (type=REVIEW, payload={"prompt": ...})
    and extracting the text output from the returned ``TaskResult``.

    Args:
        task_executor: The underlying :class:`~runner.TaskExecutor` instance.
        worker_id:     Worker identifier forwarded to ``TaskExecutor.execute``.
    """

    def __init__(self, task_executor: Any, worker_id: str = "audit-worker") -> None:
        self._executor = task_executor
        self._worker_id = worker_id
        # Expose model for AuditPhase to embed in AuditResult
        self.model: str = getattr(task_executor, "model", "audit-model")

    def execute(self, prompt: str) -> str:
        """Execute a plain string prompt and return the text response.

        Wraps the prompt in a :class:`~schemas.TaskSpec` with
        ``type=TaskType.REVIEW`` and ``payload={"prompt": prompt}``, then
        extracts and returns the text content from the resulting
        :class:`~schemas.TaskResult`.
        """
        from ..schemas import ModelTier, TaskSpec, TaskType  # noqa: PLC0415

        task = TaskSpec(
            type=TaskType.REVIEW,
            payload={"prompt": prompt},
            preferred_model=ModelTier.OPUS,
        )
        result = self._executor.execute(task, self._worker_id)
        # Extract text from TaskResult.result dict (set by AnthropicExecutor /
        # OpenClawExecutor as {"text": ...} or {"output": ...}).
        if hasattr(result, "result") and isinstance(result.result, dict):
            for key in ("text", "output", "content", "message"):
                val = result.result.get(key)
                if val:
                    return str(val)
        if hasattr(result, "result"):
            return str(result.result)
        return str(result)


def _run_post_pipeline_review_analysis(
    run_id: str,
    db: Any,
    phase_outputs: Dict[str, Any],
    executor: Optional[Any] = None,
) -> tuple:
    """Fetch review data, run AuditPhase, and persist calibration snapshots.

    Extracted from :func:`_compute_and_dispatch_routing` so that the review
    analysis logic is independently testable and separated from routing
    concerns (Issue #4.1.6).

    Steps:
        1. Fetch run-specific review outcomes from the DB.
        2. Fetch historical calibration outcomes (last 500) from the DB.
        3. Run AuditPhase on the most recent review outcome (if executor is
           available and review outcomes exist).
        4. Call :meth:`~reviewer_calibration.ReviewerCalibrator.calibrate_and_save`
           on all calibration outcomes (including the new audit result) to
           persist per-model accuracy snapshots to the DB.

    All steps are non-fatal: exceptions are caught and logged, and the
    corresponding result defaults to an empty list.

    Args:
        run_id:       Pipeline run identifier.
        db:           Database instance.
        phase_outputs: Dict of phase_id → phase result dict; used to extract
                       a code diff for the AuditPhase.
        executor:     Optional pipeline executor for AuditPhase.  When
                      ``None``, AuditPhase is skipped.

    Returns:
        A 3-tuple ``(review_outcomes, audit_results, calibration_outcomes)``
        where each element is a list (possibly empty).
    """
    # 1. Fetch run-specific review outcomes from DB (Issue #4.1.3)
    review_outcomes: list = []
    try:
        review_outcomes = db.get_review_outcomes_for_run(run_id) or []
        logger.info(
            "PostReviewAnalysis: fetched %d review outcome(s) for run '%s'",
            len(review_outcomes),
            run_id,
        )
    except Exception as _ro_exc:  # noqa: BLE001
        logger.warning(
            "PostReviewAnalysis: could not fetch review outcomes for run '%s' " "(non-fatal): %s",
            run_id,
            _ro_exc,
        )

    # 2. Fetch historical calibration outcomes from DB (Issue #4.1.5)
    calibration_outcomes: list = []
    try:
        calibration_outcomes = db.list_review_outcomes(limit=500) or []
        logger.info(
            "PostReviewAnalysis: fetched %d calibration outcome(s) for dynamic weights",
            len(calibration_outcomes),
        )
    except Exception as _co_exc:  # noqa: BLE001
        logger.warning(
            "PostReviewAnalysis: could not fetch calibration outcomes (non-fatal): %s",
            _co_exc,
        )

    # 3. Run AuditPhase on the most recent review outcome (Issue #4.1.4)
    # The executor is wrapped in _PromptExecutorAdapter so AuditPhase
    # (which calls executor.execute(prompt: str)) works correctly with the
    # pipeline's TaskExecutor (whose execute() expects a TaskSpec).
    audit_results: list = []
    if executor is not None and review_outcomes:
        try:
            from ..audit import AuditPhase  # noqa: PLC0415

            _prompt_executor = _PromptExecutorAdapter(executor)
            _audit_model = _prompt_executor.model
            _auditor = AuditPhase(executor=_prompt_executor, model=_audit_model)

            # Provide code_diff from phase outputs when available so the
            # adversarial auditor can review the actual diff rather than
            # only the original issue list (improves catch rate).
            _code_diff: Optional[str] = None
            for _pout in phase_outputs.values():
                _txt = _extract_output_text(_pout).strip()
                if _txt:
                    _code_diff = _txt
                    break

            _audit_result = _auditor.run(
                run_id=run_id,
                review_outcome=review_outcomes[0],
                code_diff=_code_diff,
            )
            audit_results = [_audit_result.to_dict()]
            logger.info(
                "PostReviewAnalysis: AuditPhase complete for run '%s': "
                "reviewer_accuracy_score=%.4f  false_approval=%s",
                run_id,
                _audit_result.reviewer_accuracy_score,
                _audit_result.false_approval,
            )
        except Exception as _audit_exc:  # noqa: BLE001
            logger.warning(
                "PostReviewAnalysis: AuditPhase failed for run '%s' (non-fatal): %s",
                run_id,
                _audit_exc,
            )

    # 4. Persist calibration snapshots post-audit (Issue #4.1.6)
    # calibrate_and_save() writes per-model CalibrationMetrics rows to the DB.
    # Uses all available calibration outcomes (including current run's outcomes)
    # so the snapshot reflects the updated longitudinal accuracy.
    if calibration_outcomes:
        try:
            from ..reviewer_calibration import ReviewerCalibrator  # noqa: PLC0415

            _calibrator = ReviewerCalibrator(db=db)
            _calibrator.calibrate_and_save(calibration_outcomes)
            logger.info(
                "PostReviewAnalysis: calibration snapshot persisted for run '%s' "
                "(%d outcome(s))",
                run_id,
                len(calibration_outcomes),
            )
        except Exception as _cal_exc:  # noqa: BLE001
            logger.warning(
                "PostReviewAnalysis: calibrate_and_save failed for run '%s' " "(non-fatal): %s",
                run_id,
                _cal_exc,
            )

    return review_outcomes, audit_results, calibration_outcomes


def _compute_and_dispatch_routing(
    run_id: str,
    output_dir: Path,
    db: Any,
    auto_merge_config: Any,
    routing_config: Any,
    scoring_passed: bool,  # noqa: ARG001
    scoring_score: Optional[float],  # noqa: ARG001
    phase_outputs: Dict[str, Any],
    final_status: str,
    executor: Optional[Any] = None,
    repo: str = "",
    template_id: str = "",
    task_type: str = "",
) -> "tuple[str, Optional[Dict[str, Any]]]":
    """Compute confidence, route to action tier, persist decision, dispatch action.

    Called after pipeline execution and scoring complete.  Non-fatal: any
    exception is caught, logged, and the pipeline final_status is not changed.

    Steps:
        1. Run post-pipeline review analysis (fetch review outcomes, run
           AuditPhase, persist calibration snapshots) via
           :func:`_run_post_pipeline_review_analysis`.
        2. Compute composite confidence from output directory artefacts, wiring
           in review outcomes, audit results, and calibration data for full
           signal coverage (Issue #4.1.6).
        3. Evaluate routing config to produce a :class:`~routing.RoutingDecision`.
        4. Persist the decision to the ``routing_decisions`` DB table.
        5. If the pipeline succeeded, dispatch the resolved action.  For the
           ``auto_merge`` action the merge is **deferred** — a merge intent dict
           is returned instead of executing immediately, so the caller can first
           create the PR via :func:`_post_github_result_hook` (Issue #499).

    Args:
        run_id:           Pipeline run identifier.
        output_dir:       Path to output directory containing phase JSON files.
        db:               Database instance.
        auto_merge_config: AutoMergeConfig from template (or None).
        routing_config:   Custom RoutingConfig from template (or None -> default).
        scoring_passed:   Whether auto-scoring passed.
        scoring_score:    Composite scoring score (0-1), or None.
        phase_outputs:    Dict of phase_id -> phase result dict.
        final_status:     Current intended final status of the pipeline run.
        executor:         Optional pipeline executor, used to run AuditPhase.
                          When ``None``, AuditPhase is skipped (stub mode).
        repo:             Git repository slug (e.g. ``"owner/repo"``).  Used to
                          update the trust profile via :class:`~trust.TrustCalibrator`
                          after the routing decision is persisted.  When empty, the
                          trust update is skipped (non-fatal).
        template_id:      Pipeline template identifier for trust profile lookup.
        task_type:        Task type string (e.g. ``"bugfix"``) for trust profile
                          lookup.  Defaults to ``""`` (empty).

    Returns:
        A ``(final_status, merge_intent)`` tuple.  *final_status* is the
        (possibly modified) status string — routing may update it to
        ``'pending_review'`` or ``'rejected'``.  *merge_intent* is a dict
        containing the arguments for :func:`_dispatch_auto_merge` when routing
        selected ``auto_merge``, or ``None`` otherwise.  The caller is
        responsible for executing the deferred merge after PR creation.
    """
    try:
        # 1. Run post-pipeline review analysis (audit + calibration update).
        review_outcomes, audit_results, calibration_outcomes = (
            _d._run_post_pipeline_review_analysis(
                run_id=run_id,
                db=db,
                phase_outputs=phase_outputs,
                executor=executor,
            )
        )

        # 2. Compute composite confidence from output directory, wiring all signals
        confidence_result = _d.ConfidenceCalculator().compute_confidence(
            output_dir,
            review_outcomes=review_outcomes or None,
            audit_results=audit_results or None,
            calibration_outcomes=calibration_outcomes or None,
        )

        logger.info(
            "Confidence computed for run '%s': score=%.4f tier=%s",
            run_id,
            confidence_result.composite_score,
            confidence_result.confidence_level.value,
        )

        # 3. Evaluate routing (use template config if provided, else default)
        _routing_cfg = routing_config or _d.DEFAULT_ROUTING_CONFIG
        decision = _d.RoutingEngine(_routing_cfg).evaluate(
            confidence_result,
            repo=repo,
            template_id=template_id,
            task_type=task_type,
            db=db,
        )

        logger.info(
            "Routing decision for run '%s': tier='%s' strategy='%s' score=%.4f",
            run_id,
            decision.tier,
            decision.strategy,
            decision.score,
        )

        # 3a. Map strategy → action
        action = _d._strategy_to_action(decision.strategy)

        # 3b. Build signals_json from confidence result signals
        signals_dict: Dict[str, Any] = {
            s.name: {
                "value": s.value,
                "weight": s.weight,
                "raw_value": s.raw_value,
                "source": s.source,
            }
            for s in confidence_result.signals
        }
        signals_json = json.dumps(signals_dict, default=str)

        # 4. Persist routing decision to DB (audit trail)
        db.insert_routing_decision(
            {
                "run_id": run_id,
                "confidence_score": confidence_result.composite_score,
                "tier_name": decision.tier,
                "action": action,
                "justification": confidence_result.explanation,
                "signals_json": signals_json,
            }
        )

        logger.info(
            "Routing decision persisted for run '%s': action='%s'",
            run_id,
            action,
        )

        # 5. Only dispatch action if pipeline succeeded (don't auto-merge a failing run)
        if final_status not in ("success",):
            logger.info(
                "Routing dispatch skipped for run '%s': final_status='%s' "
                "(only dispatching on success)",
                run_id,
                final_status,
            )
            return final_status, None

        # 6. Determine updated final_status from routing action before dispatch
        if action == "human_review":
            final_status = "pending_review"
        elif action == "reject":
            final_status = "rejected"

        # 7. Dispatch action (status already updated above; dispatch does I/O only).
        #    For auto_merge, defer execution until after _post_github_result_hook so
        #    that the PR is created before the merge is attempted (Issue #499).
        if action == "auto_merge":
            merge_intent: Optional[Dict[str, Any]] = {
                "run_id": run_id,
                "auto_merge_config": auto_merge_config,
                "decision": decision,
                "phase_outputs": phase_outputs,
                "repo": repo,
            }
        else:
            merge_intent = None
            _d._dispatch_routing_action(
                run_id=run_id,
                action=action,
                decision=decision,
                confidence_result=confidence_result,
                auto_merge_config=auto_merge_config,
                phase_outputs=phase_outputs,
                repo=repo,
            )

        return final_status, merge_intent

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Confidence/routing integration failed for run '%s' (non-fatal): %s",
            run_id,
            exc,
        )
        return final_status, None


def _dispatch_routing_action(  # noqa: C901
    run_id: str,
    action: str,
    decision: Any,
    confidence_result: Any,
    auto_merge_config: Any,
    phase_outputs: Dict[str, Any],
    repo: str = "",
) -> None:
    """Execute the routing action determined by RoutingEngine.

    Three actions are supported:
    - ``"auto_merge"``   — attempt PR merge via GitContext.
    - ``"human_review"`` — log for manual follow-up (status update handled by caller).
    - ``"reject"``       — optionally post a GitHub comment explaining the rejection.

    Status updates (``pending_review`` / ``rejected``) are managed by the caller
    (:func:`_compute_and_dispatch_routing` returns the updated final_status, and
    ``run_daemon()`` persists it in the terminal ``db.update_pipeline_run`` call).

    All failures are logged and swallowed — routing dispatch never aborts
    the pipeline.

    Args:
        run_id:            Pipeline run identifier.
        action:            One of ``"auto_merge"``, ``"human_review"``, ``"reject"``.
        decision:          :class:`~routing.RoutingDecision` from RoutingEngine.
        confidence_result: :class:`~confidence.ConfidenceResult` from ConfidenceCalculator.
        auto_merge_config: AutoMergeConfig from template (or None).
        phase_outputs:     Dict of phase_id → phase result dict.
        repo:              Git repository slug (e.g. ``"owner/repo"``).  Used for
                           allowlist checks in auto-merge.  Optional.
    """
    try:
        if action == "auto_merge":
            _d._dispatch_auto_merge(
                run_id=run_id,
                auto_merge_config=auto_merge_config,
                decision=decision,
                phase_outputs=phase_outputs,
                repo=repo,
            )
        elif action == "human_review":
            logger.info(
                "Routing action 'human_review' for run '%s': tier='%s' score=%.4f "
                "— queued for manual review (status will be set to pending_review).",
                run_id,
                decision.tier,
                decision.score,
            )
            try:
                from ..git_integration import GitContext as _GitContextHR  # noqa: PLC0415
                from ..notifications import NotificationDispatcher  # noqa: PLC0415

                # Enrich notification with issue context from the gate file
                _gate_data = _GitContextHR.load_gate(run_id) or {}
                _issue_number = _gate_data.get("issue_number")
                _pr_url = _gate_data.get("pr_url", "")

                # Extract a one-line summary from the last completed phase output
                _summary = ""
                for _pid, _pout in reversed(list(phase_outputs.items())):
                    _raw = _extract_output_text(_pout).strip()
                    if _raw:
                        # Take first non-empty line, truncate to 120 chars
                        for _line in _raw.splitlines():
                            _line = _line.strip()
                            if _line:
                                _summary = _line[:120]
                                break
                    if _summary:
                        break

                # Confidence level string (e.g. "medium")
                _confidence = ""
                try:
                    _confidence = confidence_result.confidence_level.value
                except AttributeError:
                    pass

                dispatcher = NotificationDispatcher.from_env()
                dispatcher.dispatch(
                    event="human_review",
                    run_id=run_id,
                    tier=decision.tier,
                    score=decision.score,
                    justification=getattr(confidence_result, "explanation", ""),
                    issue_number=_issue_number,
                    summary=_summary,
                    confidence=_confidence,
                    pr_url=_pr_url,
                )
            except Exception as _ne:  # noqa: BLE001
                logger.warning(
                    "Notification dispatch failed for run '%s' (non-fatal): %s",
                    run_id,
                    _ne,
                )
        elif action == "reject":
            logger.info(
                "Routing action 'reject' for run '%s': tier='%s' score=%.4f "
                "— run will be marked as rejected.",
                run_id,
                decision.tier,
                decision.score,
            )
            _post_reject_comment(
                run_id=run_id,
                decision=decision,
                confidence_result=confidence_result,
            )
        else:
            logger.warning(
                "Unknown routing action '%s' for run '%s' — treated as human_review.",
                action,
                run_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Routing dispatch failed for run '%s' action='%s' (non-fatal): %s",
            run_id,
            action,
            exc,
        )


def _dispatch_auto_merge(  # noqa: C901
    run_id: str,
    auto_merge_config: Any,
    decision: Any,
    phase_outputs: Dict[str, Any],
    repo: str = "",
) -> None:
    """Attempt PR auto-merge when routing selects the auto_merge action.

    Driven by the routing decision rather than a binary score/threshold check
    (that check is now in :class:`~routing.RoutingEngine`).  The
    ``auto_merge_config`` is consulted for ``require_approve`` and ``strategy``
    but is **no longer required** — when ``None`` or ``enabled=False`` the
    merge proceeds with sensible defaults (strategy=``"squash"``).

    Safety guards (evaluated before calling ``gh pr merge``):

    * **Repo allowlist** — when ``ORCH_AUTO_MERGE_ALLOWED_REPOS`` is set to a
      non-empty comma-separated list, only repos explicitly listed are allowed
      to auto-merge.  An empty env var (the default) permits all repos.
    * **Protected branch guard** — branches named ``main``, ``master``,
      ``develop``, or any name listed in ``ORCH_AUTO_MERGE_PROTECTED_BRANCHES``
      are never merged automatically.  Override by setting
      ``ORCH_AUTO_MERGE_PROTECTED_BRANCHES`` to a comma-separated list.
    * **Dry-run mode** — when ``ORCH_AUTO_MERGE_DRY_RUN=1`` (or ``true``/``yes``)
      the merge is logged but ``gh pr merge`` is **not** called.

    A Telegram notification is dispatched after a successful merge when
    ``NOTIFY_TELEGRAM_ENABLED=1``.

    Args:
        run_id:            Pipeline run identifier.
        auto_merge_config: AutoMergeConfig from template (or None).  When
                           ``None``, defaults to strategy ``"squash"`` and
                           ``require_approve=False``.
        decision:          :class:`~routing.RoutingDecision` from RoutingEngine.
        phase_outputs:     Dict of phase_id → phase result dict.
        repo:              Git repository slug (e.g. ``"owner/repo"``).  Used
                           for the allowlist check.  Optional.
    """
    import os as _os  # noqa: PLC0415

    from ..db import default_db_path  # noqa: PLC0415

    # Derive effective config values; auto_merge_config is no longer required.
    strategy = auto_merge_config.strategy if auto_merge_config else "squash"
    require_approve = auto_merge_config.require_approve if auto_merge_config is not None else False
    review_phase_id = (
        auto_merge_config.review_phase_id if auto_merge_config is not None else "review"
    )

    # --- Safety guard 1: repo allowlist ---
    _allowed_raw = _os.environ.get("ORCH_AUTO_MERGE_ALLOWED_REPOS", "").strip()
    if _allowed_raw and repo:
        allowed_repos = {r.strip() for r in _allowed_raw.split(",") if r.strip()}
        if allowed_repos and repo not in allowed_repos:
            logger.info(
                "Auto-merge BLOCKED for run '%s': repo '%s' is not in "
                "ORCH_AUTO_MERGE_ALLOWED_REPOS allowlist (%s).",
                run_id,
                repo,
                ", ".join(sorted(allowed_repos)),
            )
            return

    # --- Honour require_approve check (delegated from template config) ---
    if require_approve:
        review_out = phase_outputs.get(review_phase_id)
        if review_out is None:
            logger.info(
                "Auto-merge skipped for run '%s': review phase '%s' not found "
                "(require_approve=True).",
                run_id,
                review_phase_id,
            )
            return
        review_text = _extract_output_text(review_out).strip()
        # Issue #687: canonical verdict extractor lives in verdict_parser and
        # returns lowercase ("approve" / "request_changes" / "abort" / None).
        from ..verdict_parser import extract_verdict as _extract_verdict  # noqa: PLC0415

        _verdict = _extract_verdict(text=review_text)
        if _verdict != "approve":
            logger.info(
                "Auto-merge skipped for run '%s': review phase '%s' did not "
                "return APPROVE verdict (got: %r).",
                run_id,
                review_phase_id,
                _verdict,
            )
            return

    # Load gate file to resolve branch name (required for safety checks and merge).
    from ..git_integration import GitContext as _GitContext  # noqa: PLC0415

    gate_data = _GitContext.load_gate(run_id)
    if gate_data is None:
        logger.warning(
            "Auto-merge: no gate file found for run '%s' — "
            "cannot determine branch name.  Is git.enabled=true in the template?",
            run_id,
        )
        return

    branch_name = gate_data.get("branch", "")
    if not branch_name:
        logger.warning(
            "Auto-merge: gate file for run '%s' has no 'branch' field — skipping.",
            run_id,
        )
        return

    # --- Safety guard 2: protected branch ---
    _default_protected = {"main", "master", "develop"}
    _protected_raw = _os.environ.get("ORCH_AUTO_MERGE_PROTECTED_BRANCHES", "").strip()
    protected_branches: set[str]
    if _protected_raw:
        protected_branches = {b.strip() for b in _protected_raw.split(",") if b.strip()}
    else:
        protected_branches = _default_protected

    if branch_name in protected_branches:
        logger.warning(
            "Auto-merge BLOCKED for run '%s': branch '%s' is in the protected "
            "branches list — refusing to auto-merge.  "
            "Override via ORCH_AUTO_MERGE_PROTECTED_BRANCHES env var.",
            run_id,
            branch_name,
        )
        return

    # --- Safety guard 3: dry-run mode ---
    def _is_truthy(val: str) -> bool:
        return val.strip().lower() in ("1", "true", "yes")

    if _is_truthy(_os.environ.get("ORCH_AUTO_MERGE_DRY_RUN", "")):
        logger.info(
            "Auto-merge DRY-RUN for run '%s': would merge branch '%s' "
            "with strategy='%s' (set ORCH_AUTO_MERGE_DRY_RUN=0 to activate).",
            run_id,
            branch_name,
            strategy,
        )
        return

    # --- Execute merge ---
    logger.info(
        "Auto-merge TRIGGERED for run '%s': score=%.4f, branch='%s', strategy='%s'.",
        run_id,
        decision.score,
        branch_name,
        strategy,
    )

    _GitContext.auto_merge_pr(
        run_id=run_id,
        branch_name=branch_name,
        strategy=strategy,
    )

    # Update gate status to merged
    try:
        _GitContext.update_gate_status(
            run_id,
            status="merged",
            message=f"Auto-merged by orchestrator (score={decision.score:.4f})",
        )
    except Exception as _ge:  # noqa: BLE001
        logger.warning("Auto-merge: could not update gate status: %s", _ge)

    # --- Dispatch notification after successful merge ---
    try:
        from ..notifications import NotificationDispatcher  # noqa: PLC0415

        _notifier = NotificationDispatcher.from_env()
        _notifier.dispatch(
            event="auto_merge",
            run_id=run_id,
            tier=decision.tier,
            score=decision.score,
            branch=branch_name,
            repo=repo or "unknown",
            strategy=strategy,
        )
    except Exception as _ne:  # noqa: BLE001
        logger.warning(
            "Auto-merge notification dispatch failed for run '%s' (non-fatal): %s",
            run_id,
            _ne,
        )

    # --- Sprint chain advancement (Issue #514) ---
    _sprint_issue_number = gate_data.get("issue_number")
    _sprint_queue_config_path = _os.environ.get("ORCH_SPRINT_QUEUE_CONFIG", "").strip()
    _trigger_sprint_chain_next(
        run_id=run_id,
        repo=repo,
        issue_number=_sprint_issue_number,
        score=decision.score if hasattr(decision, "score") else None,
        queue_config_path=_sprint_queue_config_path,
        db_path=str(default_db_path()),
    )


def _trigger_sprint_chain_next(
    run_id: str,
    repo: str,
    issue_number: Optional[int],
    score: Optional[float],
    queue_config_path: str,
    db_path: str,
) -> None:
    """Invoke sprint chain advancement after a successful auto-merge (non-fatal).

    Called from :func:`_dispatch_auto_merge` after a successful merge.  All
    exceptions are caught and logged as warnings so that a chain-automation
    bug never fails the pipeline run.

    When ``queue_config_path`` is empty or ``issue_number`` is ``None`` the
    function returns immediately (no-op), making the feature entirely opt-in.

    Args:
        run_id:            Pipeline run identifier.
        repo:              Repository slug (e.g. ``"owner/repo"``).
        issue_number:      GitHub issue number from the gate file, or ``None``.
        score:             Confidence score from the routing decision, or ``None``.
        queue_config_path: Absolute path to sprint_queue.yaml; empty → disabled.
        db_path:           Path to the orchestration engine SQLite database.
    """
    if not queue_config_path:
        return
    if not issue_number:
        logger.debug("sprint_chain: no issue_number for run %s — skipping", run_id)
        return
    try:
        from ..cost_tracker import CostTracker  # noqa: PLC0415
        from ..db import Database  # noqa: PLC0415
        from ..sprint_chain import SprintChainManager  # noqa: PLC0415

        db = Database(Path(db_path))
        tracker = CostTracker(db)
        manager = SprintChainManager(db=db, cost_tracker=tracker)
        result = manager.trigger_next(
            repo=repo,
            current_issue=issue_number,
            run_id=run_id,
            score=score,
            queue_config_path=queue_config_path,
        )
        if result.triggered:
            logger.info(
                "sprint_chain: triggered next issue #%d in %r (run=%s)",
                result.next_issue,
                repo,
                run_id,
            )
        else:
            logger.info(
                "sprint_chain: chain not advanced for run %s: %s",
                run_id,
                result.reason,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sprint_chain: _trigger_sprint_chain_next failed (non-fatal): %s",
            exc,
        )


def _post_reject_comment(
    run_id: str,
    decision: Any,
    confidence_result: Any,
) -> None:
    """Post a GitHub PR comment explaining the rejection, if configured.

    Silently skips if no gate file exists (git not configured for this run),
    or if :meth:`~git_integration.GitContext.post_pr_comment` is not implemented.

    Args:
        run_id:            Pipeline run identifier.
        decision:          :class:`~routing.RoutingDecision` from RoutingEngine.
        confidence_result: :class:`~confidence.ConfidenceResult` for explanation text.
    """
    try:
        from ..git_integration import GitContext as _GitContext  # noqa: PLC0415

        gate_data = _GitContext.load_gate(run_id)
        if gate_data is None:
            return

        branch_name = gate_data.get("branch", "")
        if not branch_name:
            return

        comment_body = (
            f"## ❌ Pipeline Rejected\n\n"
            f"**Run ID:** `{run_id}`\n"
            f"**Confidence Score:** {decision.score:.4f} (tier: `{decision.tier}`)\n\n"
            f"### Reason\n{confidence_result.explanation}\n\n"
            f"*This run was automatically rejected by the orchestration engine. "
            f"Please review the signal breakdown above and resubmit when the "
            f"issues are resolved.*"
        )

        if not hasattr(_GitContext, "post_pr_comment"):
            # post_pr_comment not yet implemented — log and skip gracefully
            logger.info(
                "Reject comment for run '%s' not posted — GitContext.post_pr_comment "
                "not available. Comment would have been:\n%s",
                run_id,
                comment_body,
            )
            # Still update gate status to 'rejected' so orch gate info reflects it
            try:
                _GitContext.update_gate_status(
                    run_id,
                    status="rejected",
                    message=(f"Rejected by routing engine " f"(confidence={decision.score:.4f})"),
                )
            except Exception as _ge:  # noqa: BLE001
                logger.warning(
                    "Could not update gate status for rejected run '%s': %s",
                    run_id,
                    _ge,
                )
            return

        _GitContext.post_pr_comment(
            run_id=run_id,
            branch_name=branch_name,
            comment=comment_body,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not post reject comment for run '%s' (non-fatal): %s",
            run_id,
            exc,
        )
