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

# E501 residuals here are long RST/`:func:` references inside docstrings that
# black cannot wrap; a line-level noqa is inert inside a string literal.
# ruff: noqa: E501

import json
import logging
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ConfidenceCalculator / RoutingEngine / DEFAULT_ROUTING_CONFIG are now used only
# inside daemon/_routing.py, but they MUST remain module-level names here: tests
# patch them on the facade (orchestration_engine.daemon.ConfidenceCalculator /
# .RoutingEngine) and _routing late-binds them via `_d.<name>` so those patches
# still intercept (#1041).  default_db_path likewise stays eager — it is the
# SINGLE eager ``from ..db`` edge that keeps the db<->daemon cycle broken and is
# part of the re-exported facade surface.  Hence the F401 re-export markers.
from ..confidence import ConfidenceCalculator  # noqa: F401
from ..db import default_db_path  # noqa: F401
from ..output_utils import (
    extract_output_text as _extract_output_text,
)
from ..output_utils import (
    safe_write_phase_output as _safe_write_phase_output,
)
from ..routing import DEFAULT_ROUTING_CONFIG, RoutingEngine  # noqa: F401
from ..timestamps import now_utc

# Clusters extracted into sub-modules (wave a of #1034).  Re-imported here so
# (a) the retained inline run_daemon and its helpers resolve these by bare name
# and (b) the package facade re-exports the EXACT prior public surface — every
# public name AND every private _name a caller/test imports from
# ``orchestration_engine.daemon`` still resolves here.  These sub-modules
# eagerly import only ..routing / ..timestamps / stdlib; the SINGLE eager db
# edge is ``default_db_path`` above — do NOT add another eager ``from ..db``
# here or in the sub-modules or the db<->daemon import cycle re-forms.
from ._config import (  # noqa: F401  # re-exported public + private surface
    _HAPPY_KEYS,
    _get_effective_max_retries,
    _happy_path_phase_ids,
    apply_config_schema_defaults,
)
from ._events import (  # noqa: F401  # re-exported public + private surface
    _extract_repo_slug,
    _persist_phase_complete,
    _persist_phase_start,
    _write_phase_event,
    _write_summary,
)
from ._github_hook import (  # noqa: F401  # re-exported public + private surface
    _post_github_result_hook,
    dispatch_regression_fix_safely,
)
from ._lifecycle import (  # noqa: F401  # re-exported public + private surface
    _fail,
    _remove_pid_file,
    _setup_logging,
    _write_pid_file,
    is_process_alive,
)
from ._notify import (  # noqa: F401  # re-exported private surface
    _apply_daemon_notification_suppression,
)
from ._routing import (  # noqa: F401  # re-exported private surface
    _compute_and_dispatch_routing,
    _dispatch_auto_merge,
    _dispatch_routing_action,
    _do_auto_merge,
    _is_review_phase,
    _post_reject_comment,
    _PromptExecutorAdapter,
    _run_post_pipeline_review_analysis,
    _strategy_to_action,
    _trigger_sprint_chain_next,
)

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


def _sigterm_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
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
            "SIGTERM: signalling executor to stop polling " "(active session: %s)",
            _session or "none",
        )
        try:
            _active_executor.request_shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIGTERM: executor request_shutdown failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Core daemon function
# ---------------------------------------------------------------------------


def run_daemon(run_id: str, db_path: str) -> None:  # noqa: C901
    """Main daemon entry point.  Called by __main__ after argument parsing."""
    from ..db import Database  # noqa: PLC0415

    # Open the persistent DB (on-disk file, not :memory:)
    db = Database(Path(db_path))

    # --- Fetch run configuration ---
    run = db.get_pipeline_run(run_id)
    if run is None:
        logger.error("run_id %r not found in DB %r", run_id, db_path)
        sys.exit(1)

    output_dir = Path(run["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Set up file logging ---
    log_path = output_dir / ".orch-daemon.log"
    _setup_logging(log_path)

    logger.info("Daemon starting: run_id=%s  db=%s", run_id, db_path)
    logger.info("Template: %s", run["template_path"])
    logger.info("Mode:     %s", run["mode"])
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
        status="running",
        pid=os.getpid(),
        started_at=now_utc().isoformat(),
    )

    # --- Load template ---
    try:
        from ..templates import TemplateEngine  # noqa: PLC0415

        engine = TemplateEngine()
        template_path = Path(run["template_path"])
        template = engine.load_template(template_path)
    except Exception as exc:  # noqa: BLE001
        _fail(db, run_id, pid_path, f"Template load error: {exc}")
        return

    # --- Build PipelineRunner ---
    mode = run["mode"]
    try:
        import os as _os  # noqa: PLC0415

        from ..pipeline_runner import PipelineRunner  # noqa: PLC0415

        if mode == "standalone":
            api_key = _os.environ.get("ANTHROPIC_API_KEY")
            # Read executor_type stored in input_json (set by pipeline_launch CLI).
            # Falls back to 'auto' for runs created before this feature was added.
            _raw_input = json.loads(run["input_json"]) if run.get("input_json") else {}
            executor_type = _raw_input.pop("_executor_type", "auto")
            runner = PipelineRunner.standalone(api_key=api_key, executor_type=executor_type)
        elif mode == "openclaw":
            gateway_url = run.get("gateway_url") or _os.environ.get("OPENCLAW_GATEWAY_URL")
            gateway_token = _os.environ.get("OPENCLAW_GATEWAY_TOKEN")
            runner = PipelineRunner.openclaw(
                gateway_url=gateway_url,
                gateway_token=gateway_token,
            )
        elif mode == "openrouter":
            _or_key = _os.environ.get("OPENROUTER_API_KEY", "")
            runner = PipelineRunner.openrouter(api_key=_or_key)
        else:  # dry-run
            runner = PipelineRunner.dry_run()
    except Exception as exc:  # noqa: BLE001
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
        initial_input: Dict[str, Any] = json.loads(run["input_json"])
    except Exception as exc:  # noqa: BLE001
        _fail(db, run_id, pid_path, f"Input JSON parse error: {exc}")
        return

    # --- Apply config_schema defaults (#835) ---
    apply_config_schema_defaults(initial_input, getattr(template, "config_schema", None))

    # --- Instantiate CostTracker (Issue #5.2.2) ---
    try:
        from ..cost_tracker import CostTracker  # noqa: PLC0415

        _cost_tracker = CostTracker(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CostTracker init failed (non-fatal): %s", exc)
        _cost_tracker = None

    # --- Preflight: Definition of Ready (Issue #476, #576) ---
    try:
        from ..preflight import PreflightChecker  # noqa: PLC0415

        # Extract required_fields and category from template (Issue #576).
        # config_schema.required overrides the hardcoded REQUIRED_INPUT_FIELDS
        # when it is a list (may be empty []).  Any non-list value (None,
        # string, int, …) triggers the existing default coding-field fallback.
        _config_schema = getattr(template, "config_schema", None) or {}
        _schema_required = (
            _config_schema.get("required") if isinstance(_config_schema, dict) else None
        )
        _required_fields: Optional[List[str]] = (
            _schema_required if isinstance(_schema_required, list) else None
        )
        _category: str = getattr(template, "category", "") or ""

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
                db,
                run_id,
                pid_path,
                f"Preflight FAILED (Definition of Ready not met):\n"
                f"{chr(10).join(_preflight_result.errors)}",
            )
            return
        if _preflight_result.warnings:
            for w in _preflight_result.warnings:
                logger.warning("Preflight warning: %s", w)
    except ImportError:
        logger.debug("Preflight module not available, skipping")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Preflight infra check failed (non-fatal): %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Preflight raised unexpected error — failing safe: %s", exc)
        _fail(db, run_id, pid_path, f"Preflight error (fail-safe): {exc}")
        return

    # --- Create gate file for git-enabled pipelines (Issue #495) ---
    # The daemon never instantiates a full GitContext, so it must explicitly
    # create the gate file here — before sequencer.execute() — so that the
    # auto-merge path (load_gate / update_gate_scoring) can find the run.
    _gate_branch: str = initial_input.get("branch_name") or initial_input.get("branch") or ""
    if _gate_branch:
        try:
            from ..git_integration import GitContext as _GitContext  # noqa: PLC0415

            _gate_repo_path: Optional[str] = (
                initial_input.get("repo_path") or initial_input.get("repo") or None
            )
            _gate_issue_raw = initial_input.get("issue_number")
            _gate_issue: Optional[int] = (
                int(_gate_issue_raw) if _gate_issue_raw is not None else None
            )
            _gate_pipeline_id: str = run.get("template_id", "")
            _GitContext.create_gate(
                run_id=run_id,
                branch_name=_gate_branch,
                repo_path=_gate_repo_path,
                pipeline_id=_gate_pipeline_id,
                output_dir=str(output_dir),
                issue_number=_gate_issue,
            )
            logger.info("Gate file created for run_id=%s branch=%s", run_id, _gate_branch)
        except Exception as exc:  # pragma: no cover  # noqa: BLE001
            logger.warning("Gate file creation failed (non-fatal): %s", exc)
    else:
        logger.debug("No branch_name in initial_input — skipping gate file creation")

    # --- Extract trust routing context (Issue #4.2.3) ---
    # template_id comes from the run record; repo and task_type from initial_input.
    # repo_url like "https://github.com/owner/repo" is converted to "owner/repo".
    _trust_template_id: str = run.get("template_id", "")
    _trust_repo: str = _extract_repo_slug(
        initial_input.get("repo_url", "") or initial_input.get("repo", "")
    )
    _trust_task_type: str = initial_input.get("task_type", "")

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
        import time as _time  # noqa: PLC0415

        _phase_start_times[phase_id] = _time.monotonic()
        logger.info("Phase start: %s  wave=%d", phase_id, wave_index)

        # Enrich with model_tier and phase_name (#747)
        start_metadata: Dict[str, Any] = {}
        if hasattr(phase, "model_tier"):
            tier = phase.model_tier
            start_metadata["model_tier"] = tier.value if hasattr(tier, "value") else str(tier)
        if hasattr(phase, "name"):
            start_metadata["phase_name"] = str(phase.name)
        if hasattr(phase, "thinking_level"):
            tl = phase.thinking_level
            start_metadata["thinking_level"] = (
                tl.value if hasattr(tl, "value") else str(tl) if tl else None
            )

        _write_phase_event(db, run_id, phase_id, "phase_started", extra_metadata=start_metadata)

        # Persist the running phase so `orch status` reflects it immediately
        # (#516). Mirrors the current_phase write in _on_phase_complete but
        # touches ONLY current_phase — completed_phases and phase_outputs are
        # unchanged at phase START. Extracted to _persist_phase_start so the
        # #516 write is execution-path testable (#954).
        _persist_phase_start(db, run_id, phase_id)

    def _on_phase_complete(phase_id: str, phase_result: dict) -> None:  # noqa: C901
        """Update DB after each phase completes."""
        if _shutdown_requested:
            return

        _st = phase_result.get("state", "unknown")
        state_val = _st.value if hasattr(_st, "value") else str(_st)
        logger.info("Phase complete: %s  state=%s", phase_id, state_val)

        completed_phases.append(phase_id)
        phase_outputs[phase_id] = phase_result

        # Write phase output to disk (iteration-aware — Issue #648a)
        try:
            safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
            phase_text = _extract_output_text(phase_result)
            if phase_text:
                out_path = output_dir / f"{safe_pid}.md"
                new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"

                # Determine iteration number from metadata (BC-1 through BC-7)
                iteration_num = phase_result.get("metadata", {}).get("iteration", 1)
                if not isinstance(iteration_num, int) or iteration_num < 1:
                    iteration_num = 1

                # BC-3 / BC-3b: copy primary → round1 before first overwrite
                if iteration_num == 2 and out_path.exists():
                    round1_path = output_dir / f"{safe_pid}_round1.md"
                    if not round1_path.exists():
                        try:
                            round1_path.write_text(out_path.read_text())
                        except Exception as _exc:  # noqa: BLE001
                            logger.warning(
                                "Failed to copy round1 file for phase '%s': %s",
                                phase_id,
                                _exc,
                            )

                # BC-1 / BC-2: write primary file (existing size-check behaviour preserved)
                _safe_write_phase_output(out_path, new_content, phase_id)

                # BC-2: write round-indexed snapshot for N > 1 (always fresh, bypasses size check)
                if iteration_num > 1:
                    try:
                        round_path = output_dir / f"{safe_pid}_round{iteration_num}.md"
                        round_path.write_text(new_content)
                    except Exception as _exc:  # noqa: BLE001
                        logger.warning(
                            "Failed to write round-indexed file for phase '%s' " "iteration %d: %s",
                            phase_id,
                            iteration_num,
                            _exc,
                        )

            # Write JSON
            (output_dir / f"{safe_pid}.json").write_text(
                json.dumps(phase_result, indent=2, default=str)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write phase output to disk: %s", exc)

        # Persist progress to DB
        _persist_phase_complete(db, run_id, phase_id, completed_phases, phase_outputs)

        # Emit phase_completed event for SSE streaming (Issue #258, enriched #747)
        tokens = phase_result.get("tokens_consumed")
        cost = phase_result.get("cost_usd")

        # Compute elapsed time (#747)
        import time as _time  # noqa: PLC0415

        complete_metadata: Dict[str, Any] = {}
        start_t = _phase_start_times.pop(phase_id, None)
        if start_t is not None:
            complete_metadata["elapsed_seconds"] = round(_time.monotonic() - start_t, 2)

        # Enrich with model info (#747)
        model_used = phase_result.get("model_used")
        if model_used:
            complete_metadata["model_used"] = str(model_used)
        model_tier = phase_result.get("model_tier")
        if model_tier:
            complete_metadata["model_tier"] = (
                model_tier.value if hasattr(model_tier, "value") else str(model_tier)
            )

        # Token breakdown (#747)
        tokens_in = phase_result.get("tokens_in") or phase_result.get("prompt_tokens")
        tokens_out = phase_result.get("tokens_out") or phase_result.get("completion_tokens")
        if tokens_in is not None:
            complete_metadata["tokens_in"] = int(tokens_in)
        if tokens_out is not None:
            complete_metadata["tokens_out"] = int(tokens_out)

        _write_phase_event(
            db,
            run_id,
            phase_id,
            "phase_completed",
            phase_result=phase_result,
            tokens_consumed=int(tokens) if tokens is not None else None,
            cost_usd=float(cost) if cost is not None else None,
            state=state_val,
            extra_metadata=complete_metadata,
        )

        # --- Record phase cost and enforce per-run budget (Issue #496) ---
        if _cost_tracker is not None:
            try:
                _model = phase_result.get("model_used") or "unknown"
                _in = phase_result.get("input_tokens")
                _out = phase_result.get("output_tokens")
                _cost_record = None
                if _in is not None and _out is not None and (_in or _out):
                    # Real input/output split available (Issue #908): bill each
                    # portion at its own rate so the ledger is accurate.
                    _cost_record = _cost_tracker.record_phase(
                        run_id=run_id,
                        phase_id=phase_id,
                        model=_model,
                        input_tokens=int(_in),
                        output_tokens=int(_out),
                    )
                else:
                    _total_tokens = phase_result.get("tokens_consumed") or 0
                    if _total_tokens > 0:
                        # No split available: bill the whole total at the OUTPUT
                        # rate. output_per_million >= input_per_million for every
                        # model in pricing.yaml, so this over-estimates rather
                        # than under-estimates spend (conservative-high), which
                        # is the safe direction for budget enforcement.
                        _cost_record = _cost_tracker.record_phase(
                            run_id=run_id,
                            phase_id=phase_id,
                            model=_model,
                            input_tokens=0,
                            output_tokens=_total_tokens,
                        )
                if _cost_record is not None:
                    logger.info(
                        "Cost recorded for phase '%s': $%.6f "
                        "(in=%s, out=%s, total=%s, model=%s)",
                        phase_id,
                        _cost_record["cost_usd"],
                        _cost_record["input_tokens"],
                        _cost_record["output_tokens"],
                        phase_result.get("tokens_consumed"),
                        _model,
                    )
            except Exception as _cost_exc:  # noqa: BLE001
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
                    from ..cost_tracker import BudgetExceededError  # noqa: PLC0415

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
                phase_id,
                state_val,
                _verdict,
                _confidence,
            )

    # --- Execute pipeline ---
    from ..sequencer import PhaseSequencer, StateMachineSequencer  # noqa: PLC0415

    # Auto-select sequencer: use StateMachineSequencer when the template
    # declares transitions on any phase or at the template level.
    # Mirrors the same detection logic used in cli.py (orch run / orch score).
    _has_transitions = any(p.transitions for p in template.phases) or bool(
        template.default_transitions
    )
    _SequencerClass = StateMachineSequencer if _has_transitions else PhaseSequencer  # noqa: N806

    logger.info("Starting %s.execute()", _SequencerClass.__name__)

    aborted = False
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None

    # ── Git handoff for spec loop (Issue #674) ──────────────────────────
    _git_handoff = None
    _repo_path = initial_input.get("repo_path") if initial_input else None
    if _repo_path and output_dir:
        try:
            from ..git_handoff import GitHandoff  # noqa: PLC0415

            _git_handoff = GitHandoff(repo_path=Path(_repo_path), run_id=run_id)
            logger.info("GitHandoff created for run_id=%s", run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitHandoff creation failed (non-fatal): %s", exc)
            _git_handoff = None

    # Only pass git_handoff to StateMachineSequencer (PhaseSequencer doesn't support it)
    _extra_kwargs = {}
    if _git_handoff is not None and _SequencerClass is StateMachineSequencer:
        _extra_kwargs["git_handoff"] = _git_handoff

    with runner:
        sequencer = _SequencerClass(
            template,
            runner,
            config=initial_input,
            on_phase_complete=_on_phase_complete,
            on_phase_start=_on_phase_start,
            output_dir=output_dir,
            run_id=run_id,  # Issue #4.1.6: enables _record_review_outcome hook
            db=db,  # Issue #4.1.6: required alongside run_id for DB writes
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
                    "Graceful shutdown: orphaned session key = %s " "(attempting cancellation)",
                    _orphaned,
                )
            try:
                _active_executor.cancel_active_session()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Graceful shutdown: cancel_active_session failed (non-fatal): %s",
                    exc,
                )

        db.update_pipeline_run(
            run_id,
            status="cancelled",
            completed_at=now_utc().isoformat(),
            error_message="Cancelled by SIGTERM",
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
            status="budget_exceeded",
            completed_at=now_utc().isoformat(),
            error_message=_budget_msg,
        )
        _remove_pid_file(pid_path)
        sys.exit(3)

    if aborted or (result and result.get("aborted")):
        failed_phase = (result or {}).get("failed_phase", "unknown") if result else "unknown"
        base_msg = error_message or f"Pipeline aborted at phase '{failed_phase}'"
        _finding_analysis = (result or {}).get("finding_analysis", "") if result else ""
        msg = f"{base_msg} {_finding_analysis}".strip() if _finding_analysis else base_msg
        logger.error("Pipeline FAILED: %s", msg)
        db.update_pipeline_run(
            run_id,
            status="failed",
            completed_at=now_utc().isoformat(),
            error_message=msg,
        )
        # --- Diagnose failure (non-fatal) ---
        _diagnosis = None
        try:
            from ..diagnosis import DiagnosisEngine  # noqa: PLC0415

            _diag_executor = runner.executors[0] if runner.executors else None
            if _diag_executor is not None:
                _diag_engine = DiagnosisEngine(executor=_diag_executor, db=db)
                _diagnosis = _diag_engine.diagnose(
                    run_id,
                    error_message=msg,
                    output_dir=str(output_dir),
                    template_id=run.get("template_id"),
                )
                logger.info("Diagnosis complete for run %s", run_id)
        except Exception as _diag_exc:  # noqa: BLE001
            logger.warning("Diagnosis failed (non-fatal): %s", _diag_exc)

        # --- Adaptive retry (#3.2.3) ---
        if _diagnosis is not None:
            try:
                from ..adaptive_retry import AdaptiveRetryEngine  # noqa: PLC0415

                _retry_engine = AdaptiveRetryEngine(db=db, db_path=db_path)
                _max_retries = _get_effective_max_retries(template)
                _retry_engine.plan_and_execute(_diagnosis, run, run_id, max_retries=_max_retries)
            except Exception as _retry_exc:  # noqa: BLE001
                logger.warning("Adaptive retry failed (non-fatal): %s", _retry_exc)

        # --- Post failure result to GitHub (Issue #5.1.4) ---
        _post_github_result_hook(
            run_id=run_id,
            db=db,
            initial_input=initial_input,
            phase_outputs=phase_outputs,
            final_status="failed",
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write summary: %s", exc)

    # Auto-scoring (optional)
    # When scoring runs and fails, the pipeline run is marked 'scoring_failed'
    # instead of 'success' so that `orch wait` can propagate the failure to
    # callers (e.g. CI/CD pipelines).  Issue #288.
    skip_scoring = bool(run.get("skip_scoring", 0))
    _final_status = "success"
    _scoring_passed: bool = False  # tracks scoring outcome for auto-merge check
    _scoring_score_val: Optional[float] = None
    if not skip_scoring and template.scenario:
        try:
            from rich.console import Console  # noqa: PLC0415

            from ..scoring import run_scoring as _run_scoring  # noqa: PLC0415

            console = Console(highlight=False, force_terminal=False, no_color=True)
            # Forward the pipeline executor so LLM judge criteria route
            # through the same auth path (e.g. OpenClaw subscription token).
            # Issue #272.
            _scoring_executor = runner.executors[0] if runner.executors else None
            scoring_passed, scoring_score = _run_scoring(
                template,
                output_dir=output_dir,
                console=console,
                template_file=template_path,
                exit_on_failure=False,
                executor=_scoring_executor,
            )
            _scoring_passed = bool(scoring_passed)
            _scoring_score_val = scoring_score
            _scoring_status = "passed" if scoring_passed else "failed"
            logger.info(
                "Auto-scoring complete: %s  score=%s",
                _scoring_status,
                f"{scoring_score:.4f}" if scoring_score is not None else "n/a",
            )
            db.update_pipeline_run(
                run_id,
                scoring_status=_scoring_status,
                scoring_score=scoring_score,
            )
            # Persist scoring results to the gate file so orch gate info/approve
            # can enforce the score gate (Issue #289)
            try:
                from ..git_integration import GitContext as _GitContext  # noqa: PLC0415

                _GitContext.update_gate_scoring(run_id, _scoring_status, scoring_score)
            except Exception as _ge:  # noqa: BLE001
                logger.warning("Could not update gate file with scoring: %s", _ge)
            # Gate final pipeline status on scoring outcome (Issue #288)
            if not scoring_passed:
                _final_status = "scoring_failed"
                logger.warning(
                    "Scoring FAILED (score=%.4f) — marking run as 'scoring_failed'",
                    scoring_score,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto-scoring raised an exception: %s", exc)
            db.update_pipeline_run(run_id, scoring_status="error")
            # Design decision: scoring infrastructure errors do NOT block the pipeline
            # (scoring_status='error' vs 'failed'). A scoring exception means the
            # judge/LLM infra failed, not that the pipeline output was low quality.
            # _final_status remains 'success'. Gate approve will warn but allow.
            # Mark gate with error status on scoring exception (Issue #289)
            try:
                from ..git_integration import GitContext as _GitContext  # noqa: PLC0415

                _GitContext.update_gate_scoring(run_id, "error", None)
            except Exception as _ge:  # noqa: BLE001
                logger.warning("Could not update gate file with scoring error: %s", _ge)

    # --- Postflight: Definition of Done (Issue #476) ---
    try:
        from ..postflight import PostflightChecker  # noqa: PLC0415

        _elapsed = None
        try:
            _started = run.get("started_at")
            if _started:
                _parsed = datetime.fromisoformat(_started)
                if _parsed.tzinfo is None:
                    _parsed = _parsed.replace(tzinfo=timezone.utc)
                _elapsed = (now_utc() - _parsed).total_seconds()
        except Exception:  # noqa: BLE001
            pass
        # Derive the happy-path oracle from the loaded template (Issue #915).
        # A walk failure degrades to None → the completeness check is skipped,
        # never crashing the advisory postflight block.
        try:
            _expected_phases = _happy_path_phase_ids(template) or None
        except Exception:  # noqa: BLE001
            _expected_phases = None
        _postflight = PostflightChecker(
            input_data=initial_input,
            run_id=run_id,
            output_dir=output_dir,
            scoring_passed=_scoring_passed,
            scoring_score=_scoring_score_val,
            completed_phases=completed_phases,
            elapsed_seconds=_elapsed,
            expected_phases=_expected_phases,
        )
        _postflight_result = _postflight.run_all()
        logger.info("Postflight checks:\n%s", _postflight_result.summary())
        if _postflight_result.warnings:
            for w in _postflight_result.warnings:
                logger.warning("Postflight warning: %s", w)
    except ImportError:
        logger.debug("Postflight module not available, skipping")
    except Exception as exc:  # noqa: BLE001
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
    _NON_CODE_CATEGORIES_ROUTING = frozenset({"content", "research", "docs"})  # noqa: N806
    _normalised_category_routing = (_category or "").lower().strip()

    if _normalised_category_routing in _NON_CODE_CATEGORIES_ROUTING:
        logger.info(
            "Routing skipped for run '%s': template category=%r is non-code "
            "(Issue #578); final_status set to 'pending_review'.",
            run_id,
            _category,
        )
        _final_status = "pending_review"
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
        completed_at=now_utc().isoformat(),
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
        except Exception as _merge_exc:  # noqa: BLE001
            logger.warning(
                "Deferred auto-merge failed for run '%s' (non-fatal): %s",
                run_id,
                _merge_exc,
            )

    # --- Chain execution (Issue #330.2) ---
    # After the run's terminal status is persisted, evaluate on_complete entries
    # and spawn any configured child pipelines.  Failures here are non-fatal:
    # the parent run has already been marked with its final status.
    try:
        from ..chains import evaluate_on_complete, spawn_chain_runs  # noqa: PLC0415

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
                len(spawned),
                spawned,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Chain execution failed (non-fatal): %s", exc)

    _remove_pid_file(pid_path)
    logger.info("Daemon exiting cleanly")


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 3:
        logger.error("Usage: python -m orchestration_engine.daemon <run_id> <db_path>")
        sys.exit(1)

    _run_id = sys.argv[1]
    _db_path = sys.argv[2]
    run_daemon(_run_id, _db_path)
