"""Background daemon process for async pipeline execution.

This module is the entry point for the background process spawned by
``orch launch``.  It reads the run configuration from the ``pipeline_runs``
table, auto-selects between :class:`~orchestration_engine.sequencer.PhaseSequencer`
and :class:`~orchestration_engine.sequencer.StateMachineSequencer` based on
whether the template defines ``transitions`` or ``default_transitions``, drives
execution through all phases, and writes progress back to the database so
``orch status`` can report it.

Usage (internal — spawned by cli.py):
    python -m orchestration_engine.daemon <run_id> <db_path>
"""

import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .confidence import ConfidenceCalculator
from .routing import RoutingEngine, DEFAULT_ROUTING_CONFIG

# ---------------------------------------------------------------------------
# Logging bootstrap — daemon writes to output_dir/.orch-daemon.log
# We set up root logger after we know the output_dir from the DB record.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graceful shutdown flag (set by SIGTERM handler)
# ---------------------------------------------------------------------------
_shutdown_requested = False


def _sigterm_handler(signum: int, frame: Any) -> None:
    """Handle SIGTERM: request graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("SIGTERM received — requesting graceful shutdown")


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def _write_pid_file(output_dir: Path) -> Path:
    """Write current PID to output_dir/.orch-daemon.pid and return the path."""
    pid_path = output_dir / ".orch-daemon.pid"
    pid_path.write_text(str(os.getpid()))
    return pid_path


def _remove_pid_file(pid_path: Path) -> None:
    """Remove the PID file, ignoring errors."""
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


def is_process_alive(pid: int) -> bool:
    """Return True if a process with *pid* is running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it.
        return True


# ---------------------------------------------------------------------------
# Core daemon function
# ---------------------------------------------------------------------------


def run_daemon(run_id: str, db_path: str) -> None:
    """Main daemon entry point.  Called by __main__ after argument parsing."""
    from .db import Database

    # Open the persistent DB (on-disk file, not :memory:)
    db = Database(Path(db_path))

    # --- Fetch run configuration ---
    run = db.get_pipeline_run(run_id)
    if run is None:
        print(f"[daemon] ERROR: run_id '{run_id}' not found in DB '{db_path}'",
              file=sys.stderr)
        sys.exit(1)

    output_dir = Path(run['output_dir'])
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Set up file logging ---
    log_path = output_dir / ".orch-daemon.log"
    _setup_logging(log_path)

    logger.info("Daemon starting: run_id=%s  db=%s", run_id, db_path)
    logger.info("Template: %s", run['template_path'])
    logger.info("Mode:     %s", run['mode'])
    logger.info("Output:   %s", output_dir)

    # --- Write PID file ---
    pid_path = _write_pid_file(output_dir)
    logger.info("PID file: %s  (pid=%d)", pid_path, os.getpid())

    # --- Register SIGTERM handler ---
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # --- Update DB: mark as running ---
    db.update_pipeline_run(
        run_id,
        status='running',
        pid=os.getpid(),
        started_at=datetime.now().isoformat(),
    )

    # --- Load template ---
    try:
        from .templates import TemplateEngine
        engine = TemplateEngine()
        template_path = Path(run['template_path'])
        template = engine.load_template(template_path)
    except Exception as exc:
        _fail(db, run_id, pid_path, f"Template load error: {exc}")
        return

    # --- Build PipelineRunner ---
    mode = run['mode']
    try:
        from .pipeline_runner import PipelineRunner
        import os as _os

        if mode == 'standalone':
            api_key = _os.environ.get('ANTHROPIC_API_KEY')
            runner = PipelineRunner.standalone(api_key=api_key)
        elif mode == 'openclaw':
            gateway_url = run.get('gateway_url') or _os.environ.get('OPENCLAW_GATEWAY_URL')
            gateway_token = _os.environ.get('OPENCLAW_GATEWAY_TOKEN')
            runner = PipelineRunner.openclaw(
                gateway_url=gateway_url,
                gateway_token=gateway_token,
            )
        else:  # dry-run
            runner = PipelineRunner.dry_run()
    except Exception as exc:
        _fail(db, run_id, pid_path, f"PipelineRunner init error: {exc}")
        return

    # --- Parse input ---
    try:
        initial_input: Dict[str, Any] = json.loads(run['input_json'])
    except Exception as exc:
        _fail(db, run_id, pid_path, f"Input JSON parse error: {exc}")
        return

    # --- Build callbacks ---
    completed_phases: list = []
    phase_outputs: Dict[str, Any] = {}

    def _on_phase_start(phase_id: str, phase: Any, wave_index: int) -> None:
        """Emit a phase_started event to the DB for SSE streaming (Issue #258)."""
        if _shutdown_requested:
            return
        logger.info("Phase start: %s  wave=%d", phase_id, wave_index)
        _write_phase_event(db, run_id, phase_id, "phase_started")

    def _on_phase_complete(phase_id: str, phase_result: dict) -> None:
        """Update DB after each phase completes."""
        if _shutdown_requested:
            return

        _st = phase_result.get('state', 'unknown')
        state_val = _st.value if hasattr(_st, 'value') else str(_st)
        logger.info("Phase complete: %s  state=%s", phase_id, state_val)

        completed_phases.append(phase_id)
        phase_outputs[phase_id] = phase_result

        # Write phase output to disk
        try:
            safe_pid = re.sub(r'[^\w\-]', '_', phase_id)
            phase_text = _extract_output_text(phase_result)
            if phase_text:
                out_path = output_dir / f"{safe_pid}.md"
                new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
                _safe_write_phase_output(out_path, new_content, phase_id)
            # Write JSON
            (output_dir / f"{safe_pid}.json").write_text(
                json.dumps(phase_result, indent=2, default=str)
            )
        except Exception as exc:
            logger.warning("Failed to write phase output to disk: %s", exc)

        # Persist progress to DB
        db.update_pipeline_run(
            run_id,
            current_phase=phase_id,
            completed_phases=json.dumps(completed_phases),
            phase_outputs=json.dumps(phase_outputs, default=str),
        )

        # Emit phase_completed event for SSE streaming (Issue #258)
        tokens = phase_result.get('tokens_consumed')
        cost = phase_result.get('cost_usd')
        _write_phase_event(
            db, run_id, phase_id, "phase_completed",
            phase_result=phase_result,
            tokens_consumed=int(tokens) if tokens is not None else None,
            cost_usd=float(cost) if cost is not None else None,
            state=state_val,
        )

    # --- Execute pipeline ---
    from .sequencer import PhaseSequencer, StateMachineSequencer

    # Auto-select sequencer: use StateMachineSequencer when the template
    # declares transitions on any phase or at the template level.
    # Mirrors the same detection logic used in cli.py (orch run / orch score).
    _has_transitions = any(p.transitions for p in template.phases) or bool(
        template.default_transitions
    )
    _SequencerClass = StateMachineSequencer if _has_transitions else PhaseSequencer

    logger.info("Starting %s.execute()", _SequencerClass.__name__)

    aborted = False
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None

    with runner:
        sequencer = _SequencerClass(
            template, runner, config=initial_input,
            on_phase_complete=_on_phase_complete,
            on_phase_start=_on_phase_start,
            output_dir=output_dir,
        )

        try:
            result = sequencer.execute(initial_input)
        except Exception as exc:
            logger.exception("Pipeline execution raised: %s", exc)
            error_message = str(exc)
            aborted = True

    # Check for SIGTERM shutdown
    if _shutdown_requested:
        logger.info("Graceful shutdown: marking run as cancelled")
        db.update_pipeline_run(
            run_id,
            status='cancelled',
            completed_at=datetime.now().isoformat(),
            error_message='Cancelled by SIGTERM',
        )
        _remove_pid_file(pid_path)
        return

    if aborted or (result and result.get('aborted')):
        failed_phase = (result or {}).get('failed_phase', 'unknown') if result else 'unknown'
        msg = error_message or f"Pipeline aborted at phase '{failed_phase}'"
        logger.error("Pipeline FAILED: %s", msg)
        db.update_pipeline_run(
            run_id,
            status='failed',
            completed_at=datetime.now().isoformat(),
            error_message=msg,
        )
        # --- Diagnose failure (non-fatal) ---
        try:
            from .diagnosis import DiagnosisEngine
            _diag_executor = runner.executors[0] if runner.executors else None
            if _diag_executor is not None:
                _diag_engine = DiagnosisEngine(executor=_diag_executor, db=db)
                _diag_engine.diagnose(
                    run_id,
                    error_message=msg,
                    output_dir=str(output_dir),
                    template_id=run.get('template_id'),
                )
                logger.info("Diagnosis complete for run %s", run_id)
        except Exception as _diag_exc:
            logger.warning("Diagnosis failed (non-fatal): %s", _diag_exc)
        _remove_pid_file(pid_path)
        sys.exit(2)

    # --- Success ---
    logger.info("Pipeline SUCCESS: %d phases completed", len(completed_phases))

    # Write summary files
    try:
        _write_summary(output_dir, template, result or {}, mode, run_id)
    except Exception as exc:
        logger.warning("Failed to write summary: %s", exc)

    # Auto-scoring (optional)
    # When scoring runs and fails, the pipeline run is marked 'scoring_failed'
    # instead of 'success' so that `orch wait` can propagate the failure to
    # callers (e.g. CI/CD pipelines).  Issue #288.
    skip_scoring = bool(run.get('skip_scoring', 0))
    _final_status = 'success'
    _scoring_passed: bool = False        # tracks scoring outcome for auto-merge check
    _scoring_score_val: Optional[float] = None
    if not skip_scoring and template.scenario:
        try:
            from rich.console import Console
            from .scoring import run_scoring as _run_scoring
            console = Console(highlight=False, force_terminal=False, no_color=True)
            # Forward the pipeline executor so LLM judge criteria route
            # through the same auth path (e.g. OpenClaw subscription token).
            # Issue #272.
            _scoring_executor = runner.executors[0] if runner.executors else None
            scoring_passed, scoring_score = _run_scoring(
                template, output_dir=output_dir, console=console,
                template_file=template_path, exit_on_failure=False,
                executor=_scoring_executor,
            )
            _scoring_passed = bool(scoring_passed)
            _scoring_score_val = scoring_score
            _scoring_status = 'passed' if scoring_passed else 'failed'
            logger.info("Auto-scoring complete: %s  score=%s", _scoring_status, f"{scoring_score:.4f}" if scoring_score is not None else "n/a")
            db.update_pipeline_run(
                run_id,
                scoring_status=_scoring_status,
                scoring_score=scoring_score,
            )
            # Persist scoring results to the gate file so orch gate info/approve
            # can enforce the score gate (Issue #289)
            try:
                from .git_integration import GitContext as _GitContext
                _GitContext.update_gate_scoring(run_id, _scoring_status, scoring_score)
            except Exception as _ge:
                logger.warning("Could not update gate file with scoring: %s", _ge)
            # Gate final pipeline status on scoring outcome (Issue #288)
            if not scoring_passed:
                _final_status = 'scoring_failed'
                logger.warning(
                    "Scoring FAILED (score=%.4f) — marking run as 'scoring_failed'",
                    scoring_score,
                )
        except Exception as exc:
            logger.warning("Auto-scoring raised an exception: %s", exc)
            db.update_pipeline_run(run_id, scoring_status='error')
            # Design decision: scoring infrastructure errors do NOT block the pipeline
            # (scoring_status='error' vs 'failed'). A scoring exception means the
            # judge/LLM infra failed, not that the pipeline output was low quality.
            # _final_status remains 'success'. Gate approve will warn but allow.
            # Mark gate with error status on scoring exception (Issue #289)
            try:
                from .git_integration import GitContext as _GitContext
                _GitContext.update_gate_scoring(run_id, 'error', None)
            except Exception as _ge:
                logger.warning("Could not update gate file with scoring error: %s", _ge)

    # --- Routing dispatch (Issue #331.3) ---
    # Compute confidence, route to action tier, persist decision, and dispatch.
    # Returns (possibly updated) final_status: 'pending_review' or 'rejected'
    # when routing selects those actions on a successful run.
    # Non-fatal: all errors are caught inside _compute_and_dispatch_routing.
    _final_status = _compute_and_dispatch_routing(
        run_id=run_id,
        output_dir=output_dir,
        db=db,
        auto_merge_config=getattr(template, "auto_merge", None),
        routing_config=getattr(template, "routing_config", None),
        scoring_passed=_scoring_passed,
        scoring_score=_scoring_score_val,
        phase_outputs=phase_outputs,
        final_status=_final_status,
    )

    db.update_pipeline_run(
        run_id,
        status=_final_status,
        completed_at=datetime.now().isoformat(),
        completed_phases=json.dumps(completed_phases),
        phase_outputs=json.dumps(phase_outputs, default=str),
    )

    # --- Chain execution (Issue #330.2) ---
    # After the run's terminal status is persisted, evaluate on_complete entries
    # and spawn any configured child pipelines.  Failures here are non-fatal:
    # the parent run has already been marked with its final status.
    try:
        from .chains import evaluate_on_complete, spawn_chain_runs
        child_configs = evaluate_on_complete(
            template=template,
            run=run,
            result=result or {},
            final_status=_final_status,
        )
        if child_configs:
            spawned = spawn_chain_runs(
                child_configs=child_configs,
                db=db,
                db_path=db_path,
                parent_run_id=run_id,
            )
            logger.info(
                "Chain execution: spawned %d child run(s): %s",
                len(spawned), spawned,
            )
    except Exception as exc:
        logger.warning("Chain execution failed (non-fatal): %s", exc)

    _remove_pid_file(pid_path)
    logger.info("Daemon exiting cleanly")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_phase_event(
    db: Any,
    run_id: str,
    phase_id: str,
    event_type: str,
    phase_result: Optional[Dict[str, Any]] = None,
    tokens_consumed: Optional[int] = None,
    cost_usd: Optional[float] = None,
    state: Optional[str] = None,
) -> None:
    """Write a phase lifecycle event to the DB for SSE live-progress streaming.

    Writes a row to ``pipeline_run_events`` so the SSE endpoint can emit
    fine-grained ``phase_started`` / ``phase_completed`` events to connected
    clients.  Failures are logged and swallowed so that a DB write error
    never aborts the pipeline.

    Args:
        db: The :class:`~orchestration_engine.db.Database` instance.
        run_id: The pipeline run identifier.
        phase_id: The phase identifier.
        event_type: One of ``'phase_started'`` or ``'phase_completed'``.
        phase_result: Raw phase result dict (used for metadata).  May be
            ``None`` for ``phase_started`` events.
        tokens_consumed: Override for token count (pre-extracted by caller).
        cost_usd: Override for cost in USD (pre-extracted by caller).
        state: Serialised state string (pre-extracted by caller).
    """
    try:
        metadata: Dict[str, Any] = {}
        if phase_result:
            # Capture a lightweight summary rather than the full result blob
            result_inner = phase_result.get('result', {})
            if isinstance(result_inner, dict):
                metadata['word_count'] = len(
                    str(result_inner.get('output') or '').split()
                )
        db.insert_pipeline_run_event(
            run_id=run_id,
            event_type=event_type,
            phase_id=phase_id,
            tokens_consumed=tokens_consumed,
            cost_usd=cost_usd,
            state=state,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning(
            "Could not write phase event (run=%s phase=%s type=%s): %s",
            run_id, phase_id, event_type, exc,
        )


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
    from .git_integration import GitContext as _GitContext

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
        run_id, score_str, branch_name, strategy,
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
    except Exception as _ge:
        logger.warning("Auto-merge: could not update gate status: %s", _ge)


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


def _compute_and_dispatch_routing(
    run_id: str,
    output_dir: Path,
    db: Any,
    auto_merge_config: Any,
    routing_config: Any,
    scoring_passed: bool,
    scoring_score: Optional[float],
    phase_outputs: Dict[str, Any],
    final_status: str,
) -> str:
    """Compute confidence, route to action tier, persist decision, dispatch action.

    Called after pipeline execution and scoring complete.  Non-fatal: any
    exception is caught, logged, and the pipeline final_status is not changed.

    Steps:
        1. Compute composite confidence from output directory artefacts.
        2. Evaluate routing config to produce a :class:`~routing.RoutingDecision`.
        3. Persist the decision to the ``routing_decisions`` DB table.
        4. If the pipeline succeeded, dispatch the resolved action.

    Args:
        run_id:           Pipeline run identifier.
        output_dir:       Path to output directory containing phase JSON files.
        db:               Database instance.
        auto_merge_config: AutoMergeConfig from template (or None).
        routing_config:   Custom RoutingConfig from template (or None → default).
        scoring_passed:   Whether auto-scoring passed.
        scoring_score:    Composite scoring score (0–1), or None.
        phase_outputs:    Dict of phase_id → phase result dict.
        final_status:     Current intended final status of the pipeline run.

    Returns:
        The (possibly modified) final_status string.  Routing may update this
        to ``'pending_review'`` or ``'rejected'`` when dispatching those actions
        on a successful run.
    """
    try:
        # 1. Compute composite confidence from output directory
        confidence_result = ConfidenceCalculator().compute_confidence(output_dir)

        logger.info(
            "Confidence computed for run '%s': score=%.4f tier=%s",
            run_id,
            confidence_result.composite_score,
            confidence_result.confidence_level.value,
        )

        # 2. Evaluate routing (use template config if provided, else default)
        _routing_cfg = routing_config or DEFAULT_ROUTING_CONFIG
        decision = RoutingEngine(_routing_cfg).route(confidence_result)

        logger.info(
            "Routing decision for run '%s': tier='%s' strategy='%s' score=%.4f",
            run_id, decision.tier, decision.strategy, decision.score,
        )

        # 3. Map strategy → action
        action = _strategy_to_action(decision.strategy)

        # 4. Build signals_json from confidence result signals
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

        # 5. Persist routing decision to DB (audit trail)
        db.insert_routing_decision({
            "run_id": run_id,
            "confidence_score": confidence_result.composite_score,
            "tier_name": decision.tier,
            "action": action,
            "justification": confidence_result.explanation,
            "signals_json": signals_json,
        })

        logger.info(
            "Routing decision persisted for run '%s': action='%s'",
            run_id, action,
        )

        # 6. Only dispatch action if pipeline succeeded (don't auto-merge a failing run)
        if final_status not in ('success',):
            logger.info(
                "Routing dispatch skipped for run '%s': final_status='%s' "
                "(only dispatching on success)",
                run_id, final_status,
            )
            return final_status

        # 7. Determine updated final_status from routing action before dispatch
        if action == "human_review":
            final_status = 'pending_review'
        elif action == "reject":
            final_status = 'rejected'

        # 8. Dispatch action (status already updated above; dispatch does I/O only)
        _dispatch_routing_action(
            run_id=run_id,
            action=action,
            decision=decision,
            confidence_result=confidence_result,
            auto_merge_config=auto_merge_config,
            phase_outputs=phase_outputs,
        )

        return final_status

    except Exception as exc:
        logger.warning(
            "Confidence/routing integration failed for run '%s' (non-fatal): %s",
            run_id, exc,
        )
        return final_status


def _dispatch_routing_action(
    run_id: str,
    action: str,
    decision: Any,
    confidence_result: Any,
    auto_merge_config: Any,
    phase_outputs: Dict[str, Any],
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
    """
    try:
        if action == "auto_merge":
            _dispatch_auto_merge(
                run_id=run_id,
                auto_merge_config=auto_merge_config,
                decision=decision,
                phase_outputs=phase_outputs,
            )
        elif action == "human_review":
            logger.info(
                "Routing action 'human_review' for run '%s': tier='%s' score=%.4f "
                "— queued for manual review (status will be set to pending_review).",
                run_id, decision.tier, decision.score,
            )
            try:
                from .notifications import NotificationDispatcher
                dispatcher = NotificationDispatcher.from_env()
                dispatcher.dispatch(
                    event="human_review",
                    run_id=run_id,
                    tier=decision.tier,
                    score=decision.score,
                    justification=getattr(confidence_result, "explanation", ""),
                )
            except Exception as _ne:
                logger.warning(
                    "Notification dispatch failed for run '%s' (non-fatal): %s",
                    run_id,
                    _ne,
                )
        elif action == "reject":
            logger.info(
                "Routing action 'reject' for run '%s': tier='%s' score=%.4f "
                "— run will be marked as rejected.",
                run_id, decision.tier, decision.score,
            )
            _post_reject_comment(
                run_id=run_id,
                decision=decision,
                confidence_result=confidence_result,
            )
        else:
            logger.warning(
                "Unknown routing action '%s' for run '%s' — treated as human_review.",
                action, run_id,
            )
    except Exception as exc:
        logger.warning(
            "Routing dispatch failed for run '%s' action='%s' (non-fatal): %s",
            run_id, action, exc,
        )


def _dispatch_auto_merge(
    run_id: str,
    auto_merge_config: Any,
    decision: Any,
    phase_outputs: Dict[str, Any],
) -> None:
    """Attempt PR auto-merge when routing selects the auto_merge action.

    Mirrors the former ``_try_auto_merge()`` logic but is driven by the routing
    decision rather than a binary score/threshold check (that check is now in
    :class:`~routing.RoutingEngine`).  The ``auto_merge_config`` is still
    consulted for ``require_approve`` and ``strategy``.

    When ``auto_merge_config`` is ``None`` or ``enabled=False``, logs and returns.

    Args:
        run_id:            Pipeline run identifier.
        auto_merge_config: AutoMergeConfig from template (or None).
        decision:          :class:`~routing.RoutingDecision` from RoutingEngine.
        phase_outputs:     Dict of phase_id → phase result dict.
    """
    if auto_merge_config is None or not auto_merge_config.enabled:
        logger.info(
            "Routing selected auto_merge for run '%s' but template has no "
            "auto_merge config (or enabled=false) — skipping PR merge.",
            run_id,
        )
        return

    am = auto_merge_config

    # Honour require_approve check (delegated from template config)
    if am.require_approve:
        review_out = phase_outputs.get(am.review_phase_id)
        if review_out is None:
            logger.info(
                "Auto-merge skipped for run '%s': review phase '%s' not found "
                "(require_approve=True).",
                run_id, am.review_phase_id,
            )
            return
        review_text = _extract_output_text(review_out).strip()
        first_line = review_text.split('\n')[0].strip().upper()
        if first_line != "APPROVE":
            logger.info(
                "Auto-merge skipped for run '%s': review phase '%s' did not "
                "return APPROVE on first line (got: %r).",
                run_id, am.review_phase_id, first_line[:80],
            )
            return

    # Delegate to shared merge execution helper
    _do_auto_merge(
        run_id=run_id,
        auto_merge_config=am,
        scoring_score=decision.score,
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
        from .git_integration import GitContext as _GitContext
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

        if not hasattr(_GitContext, 'post_pr_comment'):
            # post_pr_comment not yet implemented — log and skip gracefully
            logger.info(
                "Reject comment for run '%s' not posted — GitContext.post_pr_comment "
                "not available. Comment would have been:\n%s",
                run_id, comment_body,
            )
            # Still update gate status to 'rejected' so orch gate info reflects it
            try:
                _GitContext.update_gate_status(
                    run_id,
                    status="rejected",
                    message=(
                        f"Rejected by routing engine "
                        f"(confidence={decision.score:.4f})"
                    ),
                )
            except Exception as _ge:
                logger.warning(
                    "Could not update gate status for rejected run '%s': %s",
                    run_id, _ge,
                )
            return

        _GitContext.post_pr_comment(
            run_id=run_id,
            branch_name=branch_name,
            comment=comment_body,
        )
    except Exception as exc:
        logger.warning(
            "Could not post reject comment for run '%s' (non-fatal): %s",
            run_id, exc,
        )


def _fail(db: Any, run_id: str, pid_path: Path, message: str) -> None:
    """Mark run as failed and exit."""
    logger.error("FAIL: %s", message)
    try:
        db.update_pipeline_run(
            run_id,
            status='failed',
            completed_at=datetime.now().isoformat(),
            error_message=message,
        )
    except Exception as exc:
        logger.warning("Could not update DB on fail: %s", exc)
    _remove_pid_file(pid_path)
    sys.exit(1)


def _setup_logging(log_path: Path) -> None:
    """Configure root logger to write to the daemon log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(log_path), mode='a', encoding='utf-8')
    handler.setFormatter(
        logging.Formatter('%(asctime)s  %(levelname)-8s  %(name)s  %(message)s')
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _extract_output_text(phase_out: Dict[str, Any]) -> str:
    """Extract human-readable text from a phase output dict."""
    inner = phase_out.get('result', {})
    if not isinstance(inner, dict):
        return str(inner)
    for key in ('output', 'text', 'content', 'message'):
        if key in inner:
            val = inner[key]
            if isinstance(val, list):
                texts = []
                for block in val:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        texts.append(block.get('text', ''))
                    elif isinstance(block, str):
                        texts.append(block)
                return '\n\n'.join(t for t in texts if t)
            return str(val)
    if inner:
        return json.dumps(inner, indent=2, default=str)
    return ""


def _safe_write_phase_output(out_path: Path, new_content: str, phase_id: str) -> None:
    """Write new_content to out_path unless an existing file is larger."""
    if out_path.exists() and out_path.stat().st_size > len(new_content.encode('utf-8')):
        logger.info(
            "Phase '%s': keeping agent-written file (%d bytes) over captured output (%d bytes)",
            phase_id, out_path.stat().st_size, len(new_content.encode('utf-8')),
        )
    else:
        out_path.write_text(new_content)


def _write_summary(
    output_dir: Path,
    template: Any,
    result: Dict[str, Any],
    mode: str,
    run_id: str,
) -> None:
    """Write _final_output.json, _final_output.md, and _summary.md."""
    completed_phases = list(result.get('phase_outputs', {}).keys())
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # _final_output.json
    (output_dir / "_final_output.json").write_text(
        json.dumps(result.get('final_output', {}), indent=2, default=str)
    )

    # _final_output.md
    final_text = _extract_output_text(result.get('final_output', {}))
    (output_dir / "_final_output.md").write_text(f"# Final Output\n\n{final_text}\n")

    # _summary.md
    total_tokens = 0
    total_cost = 0.0
    summary_lines = [
        f"# Run Summary: {template.name}",
        "",
        f"**Date:** {run_date}",
        f"**Run ID:** {run_id}",
        f"**Template ID:** {template.id}",
        f"**Mode:** {mode}",
        "",
        "## Phases Completed",
        "",
        "| Phase | State | Tokens | Cost |",
        "|-------|-------|--------|------|",
    ]
    for phase_id in completed_phases:
        out = result['phase_outputs'][phase_id]
        _state = out.get('state', 'unknown')
        state = _state.value if hasattr(_state, 'value') else str(_state)
        tokens = out.get('tokens_consumed', 0)
        cost = out.get('cost_usd', 0)
        cost_float = float(cost) if cost else 0.0
        cost_str = f"${cost_float:.4f}" if cost else "n/a"
        safe_id = re.sub(r'[^\w\-]', '_', phase_id)
        total_tokens += tokens
        total_cost += cost_float
        summary_lines.append(f"| {safe_id} | {state} | {tokens} | {cost_str} |")
    summary_lines += [
        "",
        f"**Total Tokens:** {total_tokens}",
        f"**Total Cost:** ${total_cost:.4f}",
        "",
    ]
    (output_dir / "_summary.md").write_text("\n".join(summary_lines))


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: python -m orchestration_engine.daemon <run_id> <db_path>",
              file=sys.stderr)
        sys.exit(1)

    _run_id = sys.argv[1]
    _db_path = sys.argv[2]
    run_daemon(_run_id, _db_path)
