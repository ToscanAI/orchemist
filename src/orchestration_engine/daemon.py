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
import subprocess
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

    # --- Instantiate CostTracker (Issue #5.2.2) ---
    try:
        from .cost_tracker import CostTracker
        _cost_tracker = CostTracker(db)
    except Exception as exc:
        logger.warning("CostTracker init failed (non-fatal): %s", exc)
        _cost_tracker = None

    # --- Preflight: Definition of Ready (Issue #476) ---
    try:
        from .preflight import PreflightChecker
        _preflight = PreflightChecker(
            initial_input,
            db=db,
            budget_config=template.budget,
            cost_tracker=_cost_tracker,
        )
        _preflight_result = _preflight.run_all()
        logger.info("Preflight checks:\n%s", _preflight_result.summary())
        if not _preflight_result.passed:
            _fail(
                db, run_id, pid_path,
                f"Preflight FAILED (Definition of Ready not met):\n"
                f"{chr(10).join(_preflight_result.errors)}"
            )
            return
        if _preflight_result.warnings:
            for w in _preflight_result.warnings:
                logger.warning("Preflight warning: %s", w)
    except ImportError:
        logger.debug("Preflight module not available, skipping")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Preflight infra check failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.error("Preflight raised unexpected error — failing safe: %s", exc)
        _fail(db, run_id, pid_path, f"Preflight error (fail-safe): {exc}")
        return

    # --- Extract trust routing context (Issue #4.2.3) ---
    # template_id comes from the run record; repo and task_type from initial_input.
    # repo_url like "https://github.com/owner/repo" is converted to "owner/repo".
    _trust_template_id: str = run.get('template_id', '')
    _trust_repo: str = _extract_repo_slug(
        initial_input.get('repo_url', '') or initial_input.get('repo', '')
    )
    _trust_task_type: str = initial_input.get('task_type', '')

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

        # Issue #4.1.6: structured summary for review phases.
        # The sequencer's _record_review_outcome hook fires automatically
        # (because run_id + db are now passed at construction); this block
        # only adds a structured log line for observability.
        if _is_review_phase(phase_id, phase_result):
            _verdict = phase_result.get("verdict", phase_result.get("decision", ""))
            _confidence = phase_result.get("confidence", "")
            logger.info(
                "Review phase '%s' complete: state=%s verdict=%r confidence=%s  "
                "(outcome recorded by sequencer hook)",
                phase_id, state_val, _verdict, _confidence,
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
            run_id=run_id,  # Issue #4.1.6: enables _record_review_outcome hook
            db=db,          # Issue #4.1.6: required alongside run_id for DB writes
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
        _diagnosis = None
        try:
            from .diagnosis import DiagnosisEngine
            _diag_executor = runner.executors[0] if runner.executors else None
            if _diag_executor is not None:
                _diag_engine = DiagnosisEngine(executor=_diag_executor, db=db)
                _diagnosis = _diag_engine.diagnose(
                    run_id,
                    error_message=msg,
                    output_dir=str(output_dir),
                    template_id=run.get('template_id'),
                )
                logger.info("Diagnosis complete for run %s", run_id)
        except Exception as _diag_exc:
            logger.warning("Diagnosis failed (non-fatal): %s", _diag_exc)

        # --- Adaptive retry (#3.2.3) ---
        if _diagnosis is not None:
            try:
                from .adaptive_retry import AdaptiveRetryEngine
                _retry_engine = AdaptiveRetryEngine(db=db, db_path=db_path)
                _retry_engine.plan_and_execute(_diagnosis, run, run_id)
            except Exception as _retry_exc:
                logger.warning("Adaptive retry failed (non-fatal): %s", _retry_exc)

        # --- Post failure result to GitHub (Issue #5.1.4) ---
        _post_github_result_hook(
            run_id=run_id,
            db=db,
            initial_input=initial_input,
            phase_outputs=phase_outputs,
            final_status='failed',
            error_message=msg,
            diagnosis=_diagnosis,
            output_dir=output_dir,
        )

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

    # --- Postflight: Definition of Done (Issue #476) ---
    try:
        from .postflight import PostflightChecker
        _elapsed = None
        try:
            _started = run.get('started_at')
            if _started:
                from datetime import datetime as _dt
                _elapsed = (datetime.now() - _dt.fromisoformat(_started)).total_seconds()
        except Exception:
            pass
        _postflight = PostflightChecker(
            input_data=initial_input,
            run_id=run_id,
            output_dir=output_dir,
            scoring_passed=_scoring_passed,
            scoring_score=_scoring_score_val,
            completed_phases=completed_phases,
            elapsed_seconds=_elapsed,
        )
        _postflight_result = _postflight.run_all()
        logger.info("Postflight checks:\n%s", _postflight_result.summary())
        if _postflight_result.warnings:
            for w in _postflight_result.warnings:
                logger.warning("Postflight warning: %s", w)
    except ImportError:
        logger.debug("Postflight module not available, skipping")
    except Exception as exc:
        logger.warning("Postflight checks failed (non-fatal): %s", exc)

    # --- Routing dispatch (Issue #331.3 / #4.1.6) ---
    # Compute confidence, route to action tier, persist decision, and dispatch.
    # Returns (possibly updated) final_status: 'pending_review' or 'rejected'
    # when routing selects those actions on a successful run.
    # Non-fatal: all errors are caught inside _compute_and_dispatch_routing.
    # Pass the primary executor so AuditPhase and dynamic weight calibration
    # can run with live LLM access (Issue #4.1.6).
    _routing_executor = runner.executors[0] if runner.executors else None
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
        executor=_routing_executor,
        repo=_trust_repo,
        template_id=_trust_template_id,
        task_type=_trust_task_type,
    )

    db.update_pipeline_run(
        run_id,
        status=_final_status,
        completed_at=datetime.now().isoformat(),
        completed_phases=json.dumps(completed_phases),
        phase_outputs=json.dumps(phase_outputs, default=str),
    )

    # --- Post success result to GitHub (Issue #5.1.4) ---
    _post_github_result_hook(
        run_id=run_id,
        db=db,
        initial_input=initial_input,
        phase_outputs=phase_outputs,
        final_status=_final_status,
        error_message=None,
        diagnosis=None,
        output_dir=output_dir,
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


def _extract_repo_slug(repo_url: str) -> str:
    """Extract an ``owner/repo`` slug from a full GitHub URL or pass-through.

    Converts common GitHub URL formats to a plain ``owner/repo`` slug:

    * ``https://github.com/owner/repo``  →  ``owner/repo``
    * ``https://github.com/owner/repo.git``  →  ``owner/repo``
    * ``git@github.com:owner/repo.git``  →  ``owner/repo``
    * ``owner/repo``  →  ``owner/repo``  (passed through unchanged)

    An empty or unrecognised string is returned as-is so callers can treat an
    empty string as "no repo context available".

    Args:
        repo_url: Raw repository URL or slug from the pipeline input.

    Returns:
        An ``owner/repo`` slug string, or the original value if it could not
        be parsed.
    """
    if not repo_url:
        return ""
    url = repo_url.strip()
    # HTTPS GitHub URL
    if "github.com/" in url:
        idx = url.index("github.com/") + len("github.com/")
        slug = url[idx:].rstrip("/").removesuffix(".git")
        return slug
    # SSH GitHub URL: git@github.com:owner/repo.git
    if url.startswith("git@github.com:"):
        slug = url[len("git@github.com:"):].rstrip("/").removesuffix(".git")
        return slug
    # Already a slug or something else — return as-is
    return url


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
        from .schemas import TaskSpec, TaskType, ModelTier  # noqa: PLC0415
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
            len(review_outcomes), run_id,
        )
    except Exception as _ro_exc:
        logger.warning(
            "PostReviewAnalysis: could not fetch review outcomes for run '%s' "
            "(non-fatal): %s",
            run_id, _ro_exc,
        )

    # 2. Fetch historical calibration outcomes from DB (Issue #4.1.5)
    calibration_outcomes: list = []
    try:
        calibration_outcomes = db.list_review_outcomes(limit=500) or []
        logger.info(
            "PostReviewAnalysis: fetched %d calibration outcome(s) for dynamic weights",
            len(calibration_outcomes),
        )
    except Exception as _co_exc:
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
            from .audit import AuditPhase  # noqa: PLC0415
            _prompt_executor = _PromptExecutorAdapter(executor)
            _audit_model = _prompt_executor.model
            _auditor = AuditPhase(executor=_prompt_executor, model=_audit_model)

            # Provide code_diff from phase outputs when available so the
            # adversarial auditor can review the actual diff rather than
            # only the original issue list (improves catch rate).
            _code_diff: Optional[str] = None
            for _pid, _pout in phase_outputs.items():
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
        except Exception as _audit_exc:
            logger.warning(
                "PostReviewAnalysis: AuditPhase failed for run '%s' (non-fatal): %s",
                run_id, _audit_exc,
            )

    # 4. Persist calibration snapshots post-audit (Issue #4.1.6)
    # calibrate_and_save() writes per-model CalibrationMetrics rows to the DB.
    # Uses all available calibration outcomes (including current run's outcomes)
    # so the snapshot reflects the updated longitudinal accuracy.
    if calibration_outcomes:
        try:
            from .reviewer_calibration import ReviewerCalibrator  # noqa: PLC0415
            _calibrator = ReviewerCalibrator(db=db)
            _calibrator.calibrate_and_save(calibration_outcomes)
            logger.info(
                "PostReviewAnalysis: calibration snapshot persisted for run '%s' "
                "(%d outcome(s))",
                run_id, len(calibration_outcomes),
            )
        except Exception as _cal_exc:
            logger.warning(
                "PostReviewAnalysis: calibrate_and_save failed for run '%s' "
                "(non-fatal): %s",
                run_id, _cal_exc,
            )

    return review_outcomes, audit_results, calibration_outcomes


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
    executor: Optional[Any] = None,
    repo: str = "",
    template_id: str = "",
    task_type: str = "",
) -> str:
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
        5. If the pipeline succeeded, dispatch the resolved action.

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
        The (possibly modified) final_status string.  Routing may update this
        to ``'pending_review'`` or ``'rejected'`` when dispatching those actions
        on a successful run.
    """
    try:
        # 1. Run post-pipeline review analysis (audit + calibration update).
        review_outcomes, audit_results, calibration_outcomes = (
            _run_post_pipeline_review_analysis(
                run_id=run_id,
                db=db,
                phase_outputs=phase_outputs,
                executor=executor,
            )
        )

        # 2. Compute composite confidence from output directory, wiring all signals
        confidence_result = ConfidenceCalculator().compute_confidence(
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
        _routing_cfg = routing_config or DEFAULT_ROUTING_CONFIG
        decision = RoutingEngine(_routing_cfg).evaluate(
            confidence_result,
            repo=repo,
            template_id=template_id,
            task_type=task_type,
            db=db,
        )

        logger.info(
            "Routing decision for run '%s': tier='%s' strategy='%s' score=%.4f",
            run_id, decision.tier, decision.strategy, decision.score,
        )

        # 3a. Map strategy → action
        action = _strategy_to_action(decision.strategy)

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

        # 5. Only dispatch action if pipeline succeeded (don't auto-merge a failing run)
        if final_status not in ('success',):
            logger.info(
                "Routing dispatch skipped for run '%s': final_status='%s' "
                "(only dispatching on success)",
                run_id, final_status,
            )
            return final_status

        # 6. Determine updated final_status from routing action before dispatch
        if action == "human_review":
            final_status = 'pending_review'
        elif action == "reject":
            final_status = 'rejected'

        # 7. Dispatch action (status already updated above; dispatch does I/O only)
        _dispatch_routing_action(
            run_id=run_id,
            action=action,
            decision=decision,
            confidence_result=confidence_result,
            auto_merge_config=auto_merge_config,
            phase_outputs=phase_outputs,
            repo=repo,
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
            _dispatch_auto_merge(
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
                run_id, decision.tier, decision.score,
            )
            try:
                from .notifications import NotificationDispatcher
                from .git_integration import GitContext as _GitContextHR

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
    import os as _os

    # Derive effective config values; auto_merge_config is no longer required.
    strategy = auto_merge_config.strategy if auto_merge_config else "squash"
    require_approve = (
        auto_merge_config.require_approve
        if auto_merge_config is not None
        else False
    )
    review_phase_id = (
        auto_merge_config.review_phase_id
        if auto_merge_config is not None
        else "review"
    )

    # --- Safety guard 1: repo allowlist ---
    _allowed_raw = _os.environ.get("ORCH_AUTO_MERGE_ALLOWED_REPOS", "").strip()
    if _allowed_raw and repo:
        allowed_repos = {r.strip() for r in _allowed_raw.split(",") if r.strip()}
        if allowed_repos and repo not in allowed_repos:
            logger.info(
                "Auto-merge BLOCKED for run '%s': repo '%s' is not in "
                "ORCH_AUTO_MERGE_ALLOWED_REPOS allowlist (%s).",
                run_id, repo, ", ".join(sorted(allowed_repos)),
            )
            return

    # --- Honour require_approve check (delegated from template config) ---
    if require_approve:
        review_out = phase_outputs.get(review_phase_id)
        if review_out is None:
            logger.info(
                "Auto-merge skipped for run '%s': review phase '%s' not found "
                "(require_approve=True).",
                run_id, review_phase_id,
            )
            return
        review_text = _extract_output_text(review_out).strip()
        from .review_parser import extract_verdict as _extract_verdict  # noqa: PLC0415
        _verdict = _extract_verdict(review_text)
        if _verdict != "APPROVE":
            logger.info(
                "Auto-merge skipped for run '%s': review phase '%s' did not "
                "return APPROVE verdict (got: %r).",
                run_id, review_phase_id,
                _verdict,
            )
            return

    # Load gate file to resolve branch name (required for safety checks and merge).
    from .git_integration import GitContext as _GitContext  # noqa: PLC0415

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
            run_id, branch_name,
        )
        return

    # --- Safety guard 3: dry-run mode ---
    def _is_truthy(val: str) -> bool:
        return val.strip().lower() in ("1", "true", "yes")

    if _is_truthy(_os.environ.get("ORCH_AUTO_MERGE_DRY_RUN", "")):
        logger.info(
            "Auto-merge DRY-RUN for run '%s': would merge branch '%s' "
            "with strategy='%s' (set ORCH_AUTO_MERGE_DRY_RUN=0 to activate).",
            run_id, branch_name, strategy,
        )
        return

    # --- Execute merge ---
    logger.info(
        "Auto-merge TRIGGERED for run '%s': score=%.4f, branch='%s', strategy='%s'.",
        run_id, decision.score, branch_name, strategy,
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
    except Exception as _ge:
        logger.warning("Auto-merge: could not update gate status: %s", _ge)

    # --- Dispatch notification after successful merge ---
    try:
        from .notifications import NotificationDispatcher  # noqa: PLC0415
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
    except Exception as _ne:
        logger.warning(
            "Auto-merge notification dispatch failed for run '%s' (non-fatal): %s",
            run_id, _ne,
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
    from .regression import SafetyGuard, RegressionStatus  # noqa: PLC0415

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
        except Exception as _ue:
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


def _post_github_result_hook(
    run_id: str,
    db: Any,
    initial_input: Dict[str, Any],
    phase_outputs: Dict[str, Any],
    final_status: str,
    error_message: Optional[str],
    diagnosis: Any,
    output_dir: Any,
) -> None:
    """Post pipeline results back to the originating GitHub issue (Issue #5.1.4).

    Resolves the triggering issue context from *initial_input* and the DB,
    then dispatches to the appropriate :mod:`issue_automation` function based
    on *final_status* and the pipeline's ``classification_type``:

    - ``failed`` → :func:`~orchestration_engine.issue_automation.post_failure_summary_comment`
    - ``bug`` / ``feature`` / ``refactor`` (success) → :func:`~orchestration_engine.issue_automation.create_pr_for_issue`
    - ``content`` / ``docs`` / ``research`` (success) → :func:`~orchestration_engine.issue_automation.post_pipeline_result_comment`

    All exceptions are caught and logged as warnings so this hook is
    completely non-fatal — the pipeline run has already been persisted with
    its final status before this function is called.

    Args:
        run_id:        Pipeline run ID.
        db:            Open :class:`~orchestration_engine.db.Database` instance.
        initial_input: Parsed ``input_json`` for the run (issue_number, repo, …).
        phase_outputs: Phase output dict keyed by phase ID.
        final_status:  Terminal status string (e.g. ``"success"``, ``"failed"``).
        error_message: Error/abort message string, or ``None`` on success.
        diagnosis:     Diagnosis result object/dict, or ``None``.
        output_dir:    Pipeline output directory (:class:`pathlib.Path`).
    """
    try:
        from .issue_automation import (
            create_pr_for_issue,
            post_pipeline_result_comment,
            post_failure_summary_comment,
        )

        # --- Resolve issue context ---
        issue_number: Optional[int] = initial_input.get('issue_number')
        repo: str = _extract_repo_slug(
            initial_input.get('repo_url', '') or initial_input.get('repo', '')
        )

        if not issue_number or not repo:
            # Not triggered by a GitHub issue — nothing to post.
            logger.debug(
                "_post_github_result_hook: no issue_number/repo in input — skipping"
            )
            return

        issue_number = int(issue_number)

        # --- Look up classification_type via DB (more authoritative than input) ---
        classification_type: Optional[str] = None
        ipm_row_id: Optional[int] = None
        try:
            ipm_row = db.get_issue_classification_by_run_id(run_id)
            if ipm_row:
                classification_type = ipm_row.get('classification_type')
                ipm_row_id = ipm_row.get('id')
        except Exception as _db_exc:
            logger.warning(
                "_post_github_result_hook: DB lookup failed (non-fatal): %s", _db_exc
            )

        # Fall back to initial_input if DB lookup missed.
        if not classification_type:
            classification_type = initial_input.get('classification_type', 'feature')

        # --- Dispatch ---
        if final_status == 'failed':
            url = post_failure_summary_comment(
                repo=repo,
                issue_number=issue_number,
                error_message=error_message or 'Unknown error',
                run_id=run_id,
                diagnosis=diagnosis,
            )
            if url:
                logger.info(
                    "_post_github_result_hook: failure comment posted → %s", url
                )
                if ipm_row_id is not None:
                    db.update_issue_classification_status(ipm_row_id, 'completed')
            else:
                logger.warning(
                    "_post_github_result_hook: failure comment post returned None"
                )

        elif classification_type in ('bug', 'feature', 'refactor'):
            # Code pipeline — open a PR.
            branch_name: str = (
                initial_input.get('branch_name')
                or initial_input.get('feature_branch')
                or f"feat/issue-{issue_number}"
            )
            pr_title = initial_input.get('pr_title') or f"Pipeline result for #{issue_number}"
            # Collect a short summary from the last phase output.
            _last_text = ""
            if phase_outputs:
                _last_phase = list(phase_outputs.values())[-1]
                _last_text = (
                    _last_phase.get('output', '')
                    or _last_phase.get('text', '')
                    or ''
                )[:500]
            pr_body = _last_text or f"Automated result from pipeline run `{run_id}`."

            # --- Safety net: ensure branch exists on remote before PR creation ---
            # Sub-agents sometimes commit locally without pushing.  Push now so
            # `gh pr create` doesn't fail with "No commits between main and branch".
            from .postflight import ensure_branch_pushed  # noqa: PLC0415
            _repo_path = initial_input.get('repo_path', '')
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
                logger.info(
                    "_post_github_result_hook: PR created → %s", url
                )
                if ipm_row_id is not None:
                    db.update_issue_classification_status(ipm_row_id, 'completed')
            else:
                logger.warning(
                    "_post_github_result_hook: PR creation returned None"
                )

        elif classification_type in ('content', 'docs', 'research'):
            # Non-code pipeline — post output as comment (truncated to 65k chars).
            _result_text = ""
            if phase_outputs:
                _last_phase = list(phase_outputs.values())[-1]
                _result_text = (
                    _last_phase.get('output', '')
                    or _last_phase.get('text', '')
                    or ''
                )
            _result_text = (_result_text or "*(No output text captured.)*")[:65_000]
            url = post_pipeline_result_comment(
                repo=repo,
                issue_number=issue_number,
                classification_type=classification_type,
                result_text=_result_text,
                run_id=run_id,
            )
            if url:
                logger.info(
                    "_post_github_result_hook: result comment posted → %s", url
                )
                if ipm_row_id is not None:
                    db.update_issue_classification_status(ipm_row_id, 'completed')
            else:
                logger.warning(
                    "_post_github_result_hook: result comment post returned None"
                )
        else:
            logger.debug(
                "_post_github_result_hook: unrecognised classification_type=%r — skipping",
                classification_type,
            )

    except Exception as exc:
        logger.warning(
            "_post_github_result_hook: unexpected error (non-fatal): %s", exc
        )


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
