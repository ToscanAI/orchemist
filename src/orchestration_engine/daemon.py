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
    skip_scoring = bool(run.get('skip_scoring', 0))
    if not skip_scoring and template.scenario:
        try:
            from rich.console import Console
            from .scoring import run_scoring as _run_scoring
            console = Console(highlight=False, force_terminal=False, no_color=True)
            # Forward the pipeline executor so LLM judge criteria route
            # through the same auth path (e.g. OpenClaw subscription token).
            # Issue #272.
            _scoring_executor = runner.executors[0] if runner.executors else None
            _run_scoring(template, output_dir=output_dir, console=console,
                         template_file=template_path, exit_on_failure=False,
                         executor=_scoring_executor)
            logger.info("Auto-scoring complete")
        except Exception as exc:
            logger.warning("Auto-scoring failed: %s", exc)

    db.update_pipeline_run(
        run_id,
        status='success',
        completed_at=datetime.now().isoformat(),
        completed_phases=json.dumps(completed_phases),
        phase_outputs=json.dumps(phase_outputs, default=str),
    )

    _remove_pid_file(pid_path)
    logger.info("Daemon exiting cleanly")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
