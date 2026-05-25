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
from typing import Any, Dict, List, Optional

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

# Module-level executor reference (Issue #488).
# Set by run_daemon() after the PipelineRunner is built so the SIGTERM handler
# can propagate the shutdown signal into the executor's polling loop without
# waiting for the current sub-agent session to complete naturally.
# Accessed only from the SIGTERM handler and the post-SIGTERM cleanup block;
# no locking required because Python's GIL serialises the signal delivery and
# the post-signal read occurs in the main thread after sequencer.execute()
# returns.
_active_executor: Any = None


def _sigterm_handler(signum: int, frame: Any) -> None:
    """Handle SIGTERM: request graceful shutdown.

    Sets the module-level ``_shutdown_requested`` flag and, when an executor
    is active, calls its ``request_shutdown()`` method so the polling loop
    in ``_run_session()`` exits on the next iteration instead of blocking
    until the sub-agent session completes or times out (Issue #488).
    """
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("SIGTERM received — requesting graceful shutdown")

    # Signal the active executor (if any) to break out of its polling loop.
    if _active_executor is not None:
        _session = getattr(_active_executor, "_active_session_key", None)
        logger.warning(
            "SIGTERM: signalling executor to stop polling "
            "(active session: %s)",
            _session or "none",
        )
        try:
            _active_executor.request_shutdown()
        except Exception as exc:
            logger.warning("SIGTERM: executor request_shutdown failed (non-fatal): %s", exc)


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
# Retry cap helpers
# ---------------------------------------------------------------------------


def apply_config_schema_defaults(config: Dict[str, Any], config_schema: Any) -> None:
    """Fill missing keys in *config* from *config_schema* property defaults.

    Mutates ``config`` in place. For every key declared under
    ``config_schema.properties.<key>.default``, the key is added to
    ``config`` if (and only if) it is not already present.

    Why this exists (#835):
        Prompt templates render via ``str.format(config=_SafeDict(config), …)``.
        Without applying schema defaults, an existing consumer who has not
        migrated their config dict to include a newly-added optional field
        would see the literal string ``<MISSING:fieldname>`` substituted into
        the rendered prompt (``_SafeDict.__missing__`` fallback) — a silent
        backward-compat regression. Filling defaults here keeps non-migrated
        consumers running cleanly when the YAML adds new optional fields.

    Safety:
        - Existing keys in ``config`` are never overwritten.
        - Properties without a ``default`` are not touched.
        - Non-dict ``config_schema``, non-dict ``properties``, and missing
          ``properties`` are all no-ops (defensive — schemas are operator-
          editable YAML).
    """
    if not isinstance(config_schema, dict):
        return
    props = config_schema.get('properties')
    if not isinstance(props, dict):
        return
    for key, spec in props.items():
        if (
            isinstance(spec, dict)
            and 'default' in spec
            and key not in config
        ):
            config[key] = spec['default']


def _get_effective_max_retries(template: Any) -> int:
    """Compute the effective max_retries cap from the template's routing config.

    Reads all routing tiers with ``strategy == 'retry'`` and applies the
    lowest non-zero cap across all such tiers. Returns:

    - ``0`` if all retry tiers explicitly set ``max_retries=0`` (no retries).
    - ``1`` if no retry tiers are defined at all (safe default).
    - The lowest non-zero cap among all retry tiers otherwise.

    Falls back to ``DEFAULT_ROUTING_CONFIG`` when the template has no
    ``routing_config`` attribute or it is ``None``.

    Args:
        template: A loaded pipeline template object.

    Returns:
        Integer effective max_retries cap (>= 0).
    """
    _DEFAULT = 1
    routing_config = getattr(template, "routing_config", None)
    if routing_config is None:
        routing_config = DEFAULT_ROUTING_CONFIG
    retry_tiers = [t for t in routing_config.tiers if t.strategy == "retry"]
    if not retry_tiers:
        return _DEFAULT
    caps = [t.max_retries for t in retry_tiers]
    nonzero = [c for c in caps if c > 0]
    if not nonzero:
        return 0  # All tiers explicitly cap retries at 0
    return min(nonzero)


# ---------------------------------------------------------------------------
# Daemon notification suppression (Issue #660)
# ---------------------------------------------------------------------------


def _apply_daemon_notification_suppression() -> None:
    """Suppress OpenClaw notifications for the daemon process.

    Force-clears ``NOTIFY_OPENCLAW_ENABLED`` in the current process environment
    so that every subsequent :meth:`~orchestration_engine.notifications.NotificationDispatcher.from_env`
    call within the daemon produces a dispatcher with the OpenClaw backend
    disabled.

    This prevents daemon pipeline events (``human_review``, ``auto_merge``)
    from triggering ``sessions_send`` calls to the Claude Code / TUI session,
    which was causing rogue agent spawning that interfered with running
    pipelines.

    Telegram and Webhook backends are unaffected — those notify humans, not AI
    agents.

    **Opt-in:** set ``NOTIFY_OPENCLAW_DAEMON_ENABLED=1`` (or ``true`` / ``yes``)
    to re-enable OpenClaw notifications from daemon runs if explicitly desired.

    Any failure to apply the suppression is logged as a WARNING and silently
    swallowed so the daemon continues operating (non-fatal).
    """
    daemon_flag = os.environ.get("NOTIFY_OPENCLAW_DAEMON_ENABLED", "")
    if str(daemon_flag).strip().lower() in ("1", "true", "yes"):
        logger.info(
            "NOTIFY_OPENCLAW_DAEMON_ENABLED=%r — OpenClaw notifications retained "
            "for daemon process",
            daemon_flag,
        )
        return

    try:
        os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""
        logger.info(
            "OpenClaw notifications suppressed for daemon process "
            "(set NOTIFY_OPENCLAW_DAEMON_ENABLED=1 to re-enable)"
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Failed to suppress OpenClaw notifications (non-fatal): %s", exc
        )


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

    # --- Suppress OpenClaw notifications in daemon process (Issue #660) ---
    _apply_daemon_notification_suppression()

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
            # Read executor_type stored in input_json (set by pipeline_launch CLI).
            # Falls back to 'auto' for runs created before this feature was added.
            _raw_input = json.loads(run['input_json']) if run.get('input_json') else {}
            executor_type = _raw_input.pop('_executor_type', 'auto')
            runner = PipelineRunner.standalone(api_key=api_key, executor_type=executor_type)
        elif mode == 'openclaw':
            gateway_url = run.get('gateway_url') or _os.environ.get('OPENCLAW_GATEWAY_URL')
            gateway_token = _os.environ.get('OPENCLAW_GATEWAY_TOKEN')
            runner = PipelineRunner.openclaw(
                gateway_url=gateway_url,
                gateway_token=gateway_token,
            )
        elif mode == 'openrouter':
            _or_key = _os.environ.get('OPENROUTER_API_KEY', '')
            runner = PipelineRunner.openrouter(api_key=_or_key)
        else:  # dry-run
            runner = PipelineRunner.dry_run()
    except Exception as exc:
        _fail(db, run_id, pid_path, f"PipelineRunner init error: {exc}")
        return

    # --- Store executor reference for SIGTERM handler (Issue #488) ---
    # The SIGTERM handler reads this module-level variable to call
    # request_shutdown() on the active executor, breaking the polling loop
    # in _run_session() immediately rather than waiting for timeout.
    global _active_executor
    _active_executor = runner.executors[0] if runner.executors else None
    if _active_executor is not None:
        logger.debug(
            "Active executor registered for graceful shutdown: %s",
            type(_active_executor).__name__,
        )

    # --- Parse input ---
    try:
        initial_input: Dict[str, Any] = json.loads(run['input_json'])
    except Exception as exc:
        _fail(db, run_id, pid_path, f"Input JSON parse error: {exc}")
        return

    # --- Apply config_schema defaults (#835) ---
    apply_config_schema_defaults(initial_input, getattr(template, 'config_schema', None))

    # --- Instantiate CostTracker (Issue #5.2.2) ---
    try:
        from .cost_tracker import CostTracker
        _cost_tracker = CostTracker(db)
    except Exception as exc:
        logger.warning("CostTracker init failed (non-fatal): %s", exc)
        _cost_tracker = None

    # --- Preflight: Definition of Ready (Issue #476, #576) ---
    try:
        from .preflight import PreflightChecker

        # Extract required_fields and category from template (Issue #576).
        # config_schema.required overrides the hardcoded REQUIRED_INPUT_FIELDS
        # when it is a list (may be empty []).  Any non-list value (None,
        # string, int, …) triggers the existing default coding-field fallback.
        _config_schema = getattr(template, 'config_schema', None) or {}
        _schema_required = (
            _config_schema.get('required')
            if isinstance(_config_schema, dict)
            else None
        )
        _required_fields: Optional[List[str]] = (
            _schema_required if isinstance(_schema_required, list) else None
        )
        _category: str = getattr(template, 'category', '') or ''

        _preflight = PreflightChecker(
            initial_input,
            db=db,
            required_fields=_required_fields,
            category=_category,
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

    # --- Create gate file for git-enabled pipelines (Issue #495) ---
    # The daemon never instantiates a full GitContext, so it must explicitly
    # create the gate file here — before sequencer.execute() — so that the
    # auto-merge path (load_gate / update_gate_scoring) can find the run.
    _gate_branch: str = (
        initial_input.get('branch_name') or initial_input.get('branch') or ''
    )
    if _gate_branch:
        try:
            from .git_integration import GitContext as _GitContext  # noqa: PLC0415
            _gate_repo_path: Optional[str] = (
                initial_input.get('repo_path') or initial_input.get('repo') or None
            )
            _gate_issue_raw = initial_input.get('issue_number')
            _gate_issue: Optional[int] = int(_gate_issue_raw) if _gate_issue_raw is not None else None
            _gate_pipeline_id: str = run.get('template_id', '')
            _GitContext.create_gate(
                run_id=run_id,
                branch_name=_gate_branch,
                repo_path=_gate_repo_path,
                pipeline_id=_gate_pipeline_id,
                output_dir=str(output_dir),
                issue_number=_gate_issue,
            )
            logger.info("Gate file created for run_id=%s branch=%s", run_id, _gate_branch)
        except Exception as exc:  # pragma: no cover
            logger.warning("Gate file creation failed (non-fatal): %s", exc)
    else:
        logger.debug("No branch_name in initial_input — skipping gate file creation")

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
    # Mutable flag — set to BudgetExceededError instance when per-run budget
    # is breached so the post-execution block can use 'budget_exceeded' status.
    _budget_exceeded_flag: list = []  # holds at most one BudgetExceededError

    # Track phase start times for elapsed_seconds calculation
    _phase_start_times: Dict[str, float] = {}

    def _on_phase_start(phase_id: str, phase: Any, wave_index: int) -> None:
        """Emit a phase_started event to the DB for SSE streaming (Issue #258)."""
        if _shutdown_requested:
            return
        import time as _time
        _phase_start_times[phase_id] = _time.monotonic()
        logger.info("Phase start: %s  wave=%d", phase_id, wave_index)

        # Enrich with model_tier and phase_name (#747)
        start_metadata: Dict[str, Any] = {}
        if hasattr(phase, 'model_tier'):
            tier = phase.model_tier
            start_metadata['model_tier'] = tier.value if hasattr(tier, 'value') else str(tier)
        if hasattr(phase, 'name'):
            start_metadata['phase_name'] = str(phase.name)
        if hasattr(phase, 'thinking_level'):
            tl = phase.thinking_level
            start_metadata['thinking_level'] = tl.value if hasattr(tl, 'value') else str(tl) if tl else None

        _write_phase_event(db, run_id, phase_id, "phase_started",
                          extra_metadata=start_metadata)

    def _on_phase_complete(phase_id: str, phase_result: dict) -> None:
        """Update DB after each phase completes."""
        if _shutdown_requested:
            return

        _st = phase_result.get('state', 'unknown')
        state_val = _st.value if hasattr(_st, 'value') else str(_st)
        logger.info("Phase complete: %s  state=%s", phase_id, state_val)

        completed_phases.append(phase_id)
        phase_outputs[phase_id] = phase_result

        # Write phase output to disk (iteration-aware — Issue #648a)
        try:
            safe_pid = re.sub(r'[^\w\-]', '_', phase_id)
            phase_text = _extract_output_text(phase_result)
            if phase_text:
                out_path = output_dir / f"{safe_pid}.md"
                new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"

                # Determine iteration number from metadata (BC-1 through BC-7)
                iteration_num = phase_result.get('metadata', {}).get('iteration', 1)
                if not isinstance(iteration_num, int) or iteration_num < 1:
                    iteration_num = 1

                # BC-3 / BC-3b: copy primary → round1 before first overwrite
                if iteration_num == 2 and out_path.exists():
                    round1_path = output_dir / f"{safe_pid}_round1.md"
                    if not round1_path.exists():
                        try:
                            round1_path.write_text(out_path.read_text())
                        except Exception as _exc:
                            logger.warning(
                                "Failed to copy round1 file for phase '%s': %s",
                                phase_id, _exc,
                            )

                # BC-1 / BC-2: write primary file (existing size-check behaviour preserved)
                _safe_write_phase_output(out_path, new_content, phase_id)

                # BC-2: write round-indexed snapshot for N > 1 (always fresh, bypasses size check)
                if iteration_num > 1:
                    try:
                        round_path = output_dir / f"{safe_pid}_round{iteration_num}.md"
                        round_path.write_text(new_content)
                    except Exception as _exc:
                        logger.warning(
                            "Failed to write round-indexed file for phase '%s' "
                            "iteration %d: %s",
                            phase_id, iteration_num, _exc,
                        )

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

        # Emit phase_completed event for SSE streaming (Issue #258, enriched #747)
        tokens = phase_result.get('tokens_consumed')
        cost = phase_result.get('cost_usd')

        # Compute elapsed time (#747)
        import time as _time
        complete_metadata: Dict[str, Any] = {}
        start_t = _phase_start_times.pop(phase_id, None)
        if start_t is not None:
            complete_metadata['elapsed_seconds'] = round(_time.monotonic() - start_t, 2)

        # Enrich with model info (#747)
        model_used = phase_result.get('model_used')
        if model_used:
            complete_metadata['model_used'] = str(model_used)
        model_tier = phase_result.get('model_tier')
        if model_tier:
            complete_metadata['model_tier'] = model_tier.value if hasattr(model_tier, 'value') else str(model_tier)

        # Token breakdown (#747)
        tokens_in = phase_result.get('tokens_in') or phase_result.get('prompt_tokens')
        tokens_out = phase_result.get('tokens_out') or phase_result.get('completion_tokens')
        if tokens_in is not None:
            complete_metadata['tokens_in'] = int(tokens_in)
        if tokens_out is not None:
            complete_metadata['tokens_out'] = int(tokens_out)

        _write_phase_event(
            db, run_id, phase_id, "phase_completed",
            phase_result=phase_result,
            tokens_consumed=int(tokens) if tokens is not None else None,
            cost_usd=float(cost) if cost is not None else None,
            state=state_val,
            extra_metadata=complete_metadata,
        )

        # --- Record phase cost and enforce per-run budget (Issue #496) ---
        if _cost_tracker is not None:
            try:
                _model = phase_result.get('model_used') or 'unknown'
                _total_tokens = phase_result.get('tokens_consumed') or 0
                if _total_tokens > 0:
                    # The executor reports tokens_consumed as a single total
                    # (no input/output split available).  Attribute all tokens
                    # as input — this is conservative (input pricing >= output
                    # pricing for most models) and safe for budget enforcement.
                    _cost_record = _cost_tracker.record_phase(
                        run_id=run_id,
                        phase_id=phase_id,
                        model=_model,
                        input_tokens=_total_tokens,
                        output_tokens=0,
                    )
                    logger.info(
                        "Cost recorded for phase '%s': $%.6f "
                        "(tokens=%d, model=%s)",
                        phase_id,
                        _cost_record['cost_usd'],
                        _total_tokens,
                        _model,
                    )
            except Exception as _cost_exc:
                logger.warning(
                    "Cost recording failed for phase '%s' (non-fatal): %s",
                    phase_id,
                    _cost_exc,
                )

            # Check per-run budget after recording (Issue #496).
            # Only enforced when template.budget.max_cost_per_run is set.
            if (
                template.budget is not None
                and template.budget.max_cost_per_run is not None
                and not _budget_exceeded_flag
            ):
                try:
                    from .cost_tracker import BudgetExceededError
                    _cost_tracker.check_budget(
                        run_id=run_id,
                        budget_usd=template.budget.max_cost_per_run,
                    )
                except BudgetExceededError as _budget_exc:  # noqa: PERF203
                    _budget_exceeded_flag.append(_budget_exc)
                    logger.error(
                        "Budget exceeded for run '%s': %s — aborting pipeline",
                        run_id,
                        _budget_exc,
                    )
                    raise  # propagate to sequencer so execution stops

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

    # ── Git handoff for spec loop (Issue #674) ──────────────────────────
    _git_handoff = None
    _repo_path = initial_input.get('repo_path') if initial_input else None
    if _repo_path and output_dir:
        try:
            from .git_handoff import GitHandoff
            _git_handoff = GitHandoff(repo_path=Path(_repo_path), run_id=run_id)
            logger.info("GitHandoff created for run_id=%s", run_id)
        except Exception as exc:
            logger.warning("GitHandoff creation failed (non-fatal): %s", exc)
            _git_handoff = None

    # Only pass git_handoff to StateMachineSequencer (PhaseSequencer doesn't support it)
    _extra_kwargs = {}
    if _git_handoff is not None and _SequencerClass is StateMachineSequencer:
        _extra_kwargs["git_handoff"] = _git_handoff

    with runner:
        sequencer = _SequencerClass(
            template, runner, config=initial_input,
            on_phase_complete=_on_phase_complete,
            on_phase_start=_on_phase_start,
            output_dir=output_dir,
            run_id=run_id,  # Issue #4.1.6: enables _record_review_outcome hook
            db=db,          # Issue #4.1.6: required alongside run_id for DB writes
            **_extra_kwargs,
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

        # Best-effort cancellation of any orphaned sub-agent session (Issue #488).
        # cancel_active_session() logs the orphaned session key and attempts
        # sessions_stop via the gateway API.  Failures are swallowed so the
        # daemon always exits cleanly after SIGTERM.
        if _active_executor is not None:
            _orphaned = getattr(_active_executor, "_active_session_key", None)
            if _orphaned:
                logger.warning(
                    "Graceful shutdown: orphaned session key = %s "
                    "(attempting cancellation)",
                    _orphaned,
                )
            try:
                _active_executor.cancel_active_session()
            except Exception as exc:
                logger.warning(
                    "Graceful shutdown: cancel_active_session failed (non-fatal): %s",
                    exc,
                )

        db.update_pipeline_run(
            run_id,
            status='cancelled',
            completed_at=datetime.now().isoformat(),
            error_message='Cancelled by SIGTERM',
        )
        _remove_pid_file(pid_path)
        return

    # --- Budget-exceeded: distinct status so callers can distinguish from
    # generic failures (Issue #496).  Checked before the general aborted path.
    if _budget_exceeded_flag:
        _budget_exc = _budget_exceeded_flag[0]
        _budget_msg = str(_budget_exc)
        logger.error("Run '%s' terminated: %s", run_id, _budget_msg)
        db.update_pipeline_run(
            run_id,
            status='budget_exceeded',
            completed_at=datetime.now().isoformat(),
            error_message=_budget_msg,
        )
        _remove_pid_file(pid_path)
        sys.exit(3)

    if aborted or (result and result.get('aborted')):
        failed_phase = (result or {}).get('failed_phase', 'unknown') if result else 'unknown'
        base_msg = error_message or f"Pipeline aborted at phase '{failed_phase}'"
        _finding_analysis = (result or {}).get('finding_analysis', '') if result else ''
        msg = f"{base_msg} {_finding_analysis}".strip() if _finding_analysis else base_msg
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
                _max_retries = _get_effective_max_retries(template)
                _retry_engine.plan_and_execute(_diagnosis, run, run_id, max_retries=_max_retries)
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
            template_category=_category,  # Issue #578
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
    #
    # Gate routing/auto-merge by template category (Issue #578).
    # Content, research, and docs pipelines must never be auto-merged —
    # routing is irrelevant for these categories.
    _NON_CODE_CATEGORIES_ROUTING = frozenset({'content', 'research', 'docs'})
    _normalised_category_routing = (_category or '').lower().strip()

    if _normalised_category_routing in _NON_CODE_CATEGORIES_ROUTING:
        logger.info(
            "Routing skipped for run '%s': template category=%r is non-code "
            "(Issue #578); final_status set to 'pending_review'.",
            run_id, _category,
        )
        _final_status = 'pending_review'
        _merge_intent = None
    else:
        # _compute_and_dispatch_routing returns (final_status, merge_intent).
        # merge_intent is non-None when routing selected auto_merge — execution is
        # deferred until after _post_github_result_hook so the PR exists first
        # (Issue #499).
        _routing_executor = runner.executors[0] if runner.executors else None
        _final_status, _merge_intent = _compute_and_dispatch_routing(
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
    # Must run BEFORE deferred auto-merge so the PR exists when gh pr merge fires.
    _post_github_result_hook(
        run_id=run_id,
        db=db,
        initial_input=initial_input,
        phase_outputs=phase_outputs,
        final_status=_final_status,
        error_message=None,
        diagnosis=None,
        output_dir=output_dir,
        template_category=_category,  # Issue #578
    )

    # --- Deferred auto-merge (Issue #499) ---
    # Execute after _post_github_result_hook so the PR has been created.
    if _merge_intent is not None:
        try:
            _dispatch_auto_merge(
                run_id=_merge_intent["run_id"],
                auto_merge_config=_merge_intent["auto_merge_config"],
                decision=_merge_intent["decision"],
                phase_outputs=_merge_intent["phase_outputs"],
                repo=_merge_intent["repo"],
            )
        except Exception as _merge_exc:
            logger.warning(
                "Deferred auto-merge failed for run '%s' (non-fatal): %s",
                run_id, _merge_exc,
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
    extra_metadata: Optional[Dict[str, Any]] = None,
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
        if extra_metadata:
            metadata.update(extra_metadata)
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
            return final_status, None

        # 6. Determine updated final_status from routing action before dispatch
        if action == "human_review":
            final_status = 'pending_review'
        elif action == "reject":
            final_status = 'rejected'

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
            _dispatch_routing_action(
                run_id=run_id,
                action=action,
                decision=decision,
                confidence_result=confidence_result,
                auto_merge_config=auto_merge_config,
                phase_outputs=phase_outputs,
                repo=repo,
            )

        return final_status, merge_intent

    except Exception as exc:
        logger.warning(
            "Confidence/routing integration failed for run '%s' (non-fatal): %s",
            run_id, exc,
        )
        return final_status, None


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
        # Issue #687: canonical verdict extractor lives in verdict_parser and
        # returns lowercase ("approve" / "request_changes" / "abort" / None).
        from .verdict_parser import extract_verdict as _extract_verdict  # noqa: PLC0415
        _verdict = _extract_verdict(text=review_text)
        if _verdict != "approve":
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

    # --- Sprint chain advancement (Issue #514) ---
    _sprint_issue_number = gate_data.get("issue_number")
    _sprint_queue_config_path = _os.environ.get("ORCH_SPRINT_QUEUE_CONFIG", "").strip()
    _trigger_sprint_chain_next(
        run_id=run_id,
        repo=repo,
        issue_number=_sprint_issue_number,
        score=decision.score if hasattr(decision, "score") else None,
        queue_config_path=_sprint_queue_config_path,
        db_path=str(Path.home() / ".orchestration-engine" / "engine.db"),
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
        logger.debug(
            "sprint_chain: no issue_number for run %s — skipping", run_id
        )
        return
    try:
        from .sprint_chain import SprintChainManager  # noqa: PLC0415
        from .db import Database  # noqa: PLC0415
        from .cost_tracker import CostTracker  # noqa: PLC0415

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
        from .issue_automation import (
            create_pr_for_issue,
            create_content_pr,
            post_pipeline_result_comment,
            post_failure_summary_comment,
        )

        # --- Non-code category dispatch (Issue #578) ---
        # Must run BEFORE the issue_number guard so content/docs pipelines
        # without an issue_number are not silently dropped.
        _NON_CODE_CATEGORIES = frozenset({'content', 'research', 'docs'})
        _CONTENT_PR_CATEGORIES = frozenset({'content', 'docs'})
        _normalised_category = (template_category or '').lower().strip()

        if _normalised_category in _NON_CODE_CATEGORIES:
            # Research pipelines: no PR, no comment — always skip.
            if _normalised_category == 'research':
                logger.debug(
                    "_post_github_result_hook: research pipeline run='%s' — "
                    "skipping postflight (no PR, no comment)",
                    run_id,
                )
                return

            # Content / docs failure: no orphan comments, no PR.
            if final_status == 'failed':
                logger.debug(
                    "_post_github_result_hook: %s pipeline run='%s' failed — "
                    "skipping postflight (no comment)",
                    _normalised_category, run_id,
                )
                return

            # Content / docs success: create a PR (no issue_number needed).
            _content_repo: str = _extract_repo_slug(
                initial_input.get('repo_url', '') or initial_input.get('repo', '')
            )
            _content_branch: str = (
                initial_input.get('branch_name')
                or initial_input.get('branch')
                or ''
            )
            if not _content_repo or not _content_branch:
                logger.debug(
                    "_post_github_result_hook: %s pipeline run='%s' missing "
                    "repo=%r or branch_name=%r — skipping PR",
                    _normalised_category, run_id, _content_repo, _content_branch,
                )
                return

            _content_topic: str = (
                initial_input.get('topic')
                or initial_input.get('title')
                or 'content'
            )
            _last_text = ""
            if phase_outputs:
                _last_phase = list(phase_outputs.values())[-1]
                _last_text = (
                    _last_phase.get('output', '')
                    or _last_phase.get('text', '')
                    or ''
                )[:500]
            _pr_body = _last_text or f"Automated content pipeline run `{run_id}`."

            try:
                url = create_content_pr(
                    repo=_content_repo,
                    branch_name=_content_branch,
                    topic=_content_topic,
                    body=_pr_body,
                    run_id=run_id,
                )
                if url:
                    logger.info(
                        "_post_github_result_hook: content PR created → %s", url
                    )
                else:
                    logger.warning(
                        "_post_github_result_hook: content PR creation returned None"
                        " for run='%s'", run_id,
                    )
            except Exception as _pr_exc:
                logger.warning(
                    "_post_github_result_hook: content PR creation failed (non-fatal)"
                    " for run='%s': %s", run_id, _pr_exc,
                )
            return

        # --- Existing code path (category is empty / 'code' / unrecognised) ---
        # Resolve issue context.
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
            _base_body = _last_text or f"Automated result from pipeline run `{run_id}`."
            # Include Closes #N so GitHub auto-links the issue (Issue #578).
            pr_body = f"{_base_body}\n\nCloses #{issue_number}"

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
