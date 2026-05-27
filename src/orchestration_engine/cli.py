"""Command Line Interface for the Orchestration Engine.

Provides CLI commands for task queue management: submit, status, list, cancel, etc.
Uses Click for command structure and rich formatting for output.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

import yaml

import click
from decimal import Decimal

from .daemon import apply_config_schema_defaults
from .db import Database, default_db_path
from .output_utils import (
    extract_output_text as _extract_output_text,
    safe_write_phase_output as _safe_write_phase_output,
)
from .queue import TaskQueue
from .schemas import (
    TaskSpec, TaskType, Priority, TaskState, TaskFilters,
    generate_task_id
)


# Global queue instance (initialized per command)
queue: Optional[TaskQueue] = None


def get_queue() -> TaskQueue:
    """Get or create the global TaskQueue instance."""
    global queue
    if queue is None:
        queue = TaskQueue()
    return queue


def format_datetime(dt: Optional[datetime]) -> str:
    """Format datetime for display."""
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: Optional[float]) -> str:
    """Format duration in seconds to human readable format."""
    if seconds is None or seconds == 0:
        return "N/A"
    
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"


def print_table(headers: List[str], rows: List[List[str]]) -> None:
    """Print a simple table with headers and rows."""
    if not rows:
        click.echo("No data to display")
        return
    
    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))
    
    # Print header
    header_line = " | ".join(
        h.ljust(col_widths[i]) for i, h in enumerate(headers)
    )
    click.echo(header_line)
    click.echo("-" * len(header_line))
    
    # Print rows
    for row in rows:
        row_line = " | ".join(
            str(row[i] if i < len(row) else "").ljust(col_widths[i])
            for i in range(len(headers))
        )
        click.echo(row_line)


@click.group()
@click.option('--db-path', type=click.Path(path_type=Path), help='Database file path')
@click.option('--verbose', '-v', is_flag=True, help='Verbose output')
@click.version_option()
def main(db_path: Optional[Path], verbose: bool) -> None:
    """Orchestration Engine CLI - AI Agent Task Coordination."""
    global queue
    
    if verbose:
        import logging
        logging.basicConfig(level=logging.INFO)
    
    # Initialize queue with custom db path if provided
    if db_path:
        from .db import Database
        queue = TaskQueue(Database(db_path))


@main.command()
@click.option('--type', 'task_type', type=click.Choice([t.value for t in TaskType]),
              required=True, help='Task type')
@click.option('--payload', required=True, help='Task payload as JSON string')
@click.option('--priority', type=click.Choice([p.name.lower() for p in Priority]),
              default='normal', help='Task priority')
@click.option('--max-retries', type=int, help='Maximum retry attempts')
@click.option('--timeout', type=int, help='Timeout in seconds')
@click.option('--min-confidence', type=float, help='Minimum confidence score (0.0-1.0)')
@click.option('--cost-limit', type=float, help='Cost limit in USD')
@click.option('--orchestra-id', help='Orchestra workflow ID')
@click.option('--orchestra-phase', help='Phase within orchestra')
@click.option('--tag', multiple=True, help='Tags (can be used multiple times)')
@click.option('--created-by', help='Creator identifier')
def submit(
    task_type: str,
    payload: str,
    priority: str,
    max_retries: Optional[int],
    timeout: Optional[int],
    min_confidence: Optional[float],
    cost_limit: Optional[float],
    orchestra_id: Optional[str],
    orchestra_phase: Optional[str],
    tag: tuple,
    created_by: Optional[str]
) -> None:
    """Submit a new task to the queue."""
    try:
        # Parse JSON payload
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON payload: {e}", err=True)
            sys.exit(1)
        
        # Create task specification
        task_spec = TaskSpec(
            type=TaskType(task_type),
            payload=payload_dict,
            priority=Priority[priority.upper()],
            max_retries=max_retries or 3,
            timeout_seconds=timeout or 3600,
            min_confidence=min_confidence or 0.7,
            cost_limit_usd=Decimal(str(cost_limit)) if cost_limit else None,
            orchestra_id=orchestra_id,
            orchestra_phase=orchestra_phase,
            tags=list(tag),
            created_by=created_by
        )
        
        # Submit task
        task_queue = get_queue()
        task_id = task_queue.submit_task(task_spec)
        
        click.echo(f"✓ Task submitted successfully")
        click.echo(f"Task ID: {task_id}")
        click.echo(f"Type: {task_type}")
        click.echo(f"Priority: {priority}")
        
    except Exception as e:
        click.echo(f"Error submitting task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument('run_or_task_id', required=False, metavar='[RUN-ID|TASK-ID]')
def status(run_or_task_id: Optional[str]) -> None:
    """Show pipeline run status, task status, or recent runs.

    Without an argument: list the 10 most recent async pipeline runs.
    With a RUN-ID: show detailed pipeline run status (phase progress, elapsed time).
    With a TASK-ID: show task queue status (legacy mode).

    \b
    Examples:
      orch status                 # list recent runs
      orch status a3f8c2d1        # pipeline run detail
      orch status <task-uuid>     # task queue status
    """
    # Try pipeline_runs first if an ID was given
    if run_or_task_id:
        try:
            _db = Database()
            run = _db.get_pipeline_run(run_or_task_id)
        except Exception:
            run = None

        if run is not None:
            # --- Pipeline run detail ---
            _print_run_detail(run)
            return

        # Fall through to legacy task queue status
        try:
            task_queue = get_queue()
            task_status_obj = task_queue.get_task_status(run_or_task_id)
            if not task_status_obj:
                click.echo(f"ID '{run_or_task_id}' not found in pipeline runs or task queue.", err=True)
                sys.exit(1)
            click.echo(f"Task Status: {run_or_task_id}")
            click.echo(f"├─ Type: {task_status_obj.task_type.value}")
            click.echo(f"├─ State: {task_status_obj.state.value}")
            click.echo(f"├─ Priority: {task_status_obj.priority.name}")
            click.echo(f"├─ Created: {format_datetime(task_status_obj.created_at)}")
            click.echo(f"├─ Started: {format_datetime(task_status_obj.started_at)}")
            click.echo(f"├─ Completed: {format_datetime(task_status_obj.completed_at)}")
            if task_status_obj.retry_count > 0:
                click.echo(f"├─ Retries: {task_status_obj.retry_count}/{task_status_obj.max_retries}")
            if task_status_obj.next_retry_at:
                click.echo(f"├─ Next Retry: {format_datetime(task_status_obj.next_retry_at)}")
            if task_status_obj.orchestra_id:
                click.echo(f"├─ Orchestra: {task_status_obj.orchestra_id}")
                if task_status_obj.orchestra_phase:
                    click.echo(f"├─ Phase: {task_status_obj.orchestra_phase}")
            if task_status_obj.progress_percentage is not None:
                click.echo(f"└─ Progress: {task_status_obj.progress_percentage:.1f}%")
            else:
                click.echo("└─ Progress: N/A")
        except Exception as e:
            click.echo(f"Error getting status: {e}", err=True)
            sys.exit(1)

    else:
        # No ID given → list recent pipeline runs (last 10)
        try:
            _db = Database()
            runs = _db.list_pipeline_runs(limit=10)
        except Exception:
            runs = []

        if not runs:
            click.echo("No pipeline runs found.  Use 'orch launch <template>' to begin.")
            click.echo("\nTip: 'orch status' lists recent async runs from 'orch launch'.")
            # Fall back to queue stats as secondary info
            try:
                task_queue = get_queue()
                stats = task_queue.get_queue_stats()
                click.echo(f"\nQueue: {stats.queued} queued  {stats.running} running  {stats.completed} done")
            except Exception:
                pass
            return

        click.echo("Recent Pipeline Runs (last 10)")
        click.echo("─" * 72)
        for run in runs:
            _print_run_summary_line(run)


def _print_run_summary_line(run: Dict[str, Any]) -> None:
    """Print a single-line summary of a pipeline run."""
    import os as _os
    run_id = run['run_id']
    status = run['status']
    template_id = run.get('template_id', '?')[:20]
    mode = run.get('mode', '?')
    created = (run.get('created_at') or '')[:16]

    # Check PID liveness if running
    if status == 'running':
        pid = run.get('pid')
        if pid:
            try:
                from .daemon import is_process_alive
                if not is_process_alive(pid):
                    status = 'crashed'
            except Exception:
                pass

    status_icon = {
        'pending': '⏳',
        'running': '🔄',
        'success': '✅',
        'failed': '❌',
        'cancelled': '🚫',
        'crashed': '💀',
        'scoring_failed': '🔴',
    }.get(status, '❓')

    current_phase = run.get('current_phase') or '-'
    # Append scoring suffix when scoring_status is set (Issue #287)
    _scoring_status = run.get('scoring_status')
    _scoring_suffix = f"  [score={_scoring_status}]" if _scoring_status else ""
    click.echo(
        f"{run_id}  {status_icon} {status:<10}  {template_id:<22}  "
        f"phase={current_phase:<20}  {created}  [{mode}]{_scoring_suffix}"
    )


def _print_run_detail(run: Dict[str, Any]) -> None:
    """Print detailed status for a single pipeline run, checking PID liveness."""
    import json as _json
    import os as _os

    run_id = run['run_id']
    status = run['status']
    pid = run.get('pid')

    # Check PID liveness
    if status == 'running' and pid:
        try:
            from .daemon import is_process_alive
            if not is_process_alive(pid):
                status = 'crashed'
                # Update DB
                try:
                    Database().update_pipeline_run(run_id, status='crashed')
                except Exception:
                    pass
        except Exception:
            pass

    # Elapsed time
    started_at = run.get('started_at')
    completed_at = run.get('completed_at')
    elapsed_str = 'n/a'
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at)
            if completed_at:
                end_dt = datetime.fromisoformat(completed_at)
            else:
                end_dt = datetime.now()
            elapsed_s = (end_dt - start_dt).total_seconds()
            elapsed_str = _fmt_elapsed(elapsed_s)
        except Exception:
            pass

    # Completed phases
    try:
        completed_phases = _json.loads(run.get('completed_phases') or '[]')
    except Exception:
        completed_phases = []

    status_icon = {
        'pending': '⏳',
        'running': '🔄',
        'success': '✅',
        'failed': '❌',
        'cancelled': '🚫',
        'crashed': '💀',
        'scoring_failed': '🔴',
    }.get(status, '❓')

    click.echo(f"Pipeline Run: {run_id}")
    click.echo(f"├─ Status:     {status_icon} {status}")
    click.echo(f"├─ Template:   {run.get('template_id', '?')}")
    click.echo(f"├─ Mode:       {run.get('mode', '?')}")
    click.echo(f"├─ Elapsed:    {elapsed_str}")
    click.echo(f"├─ Current:    {run.get('current_phase') or '(none)'}")
    click.echo(f"├─ Completed:  {len(completed_phases)} phase(s): {completed_phases}")
    click.echo(f"├─ PID:        {pid or 'n/a'}")
    click.echo(f"├─ Output:     {run.get('output_dir', '?')}")
    if run.get('error_message'):
        click.echo(f"├─ Error:      {run['error_message']}")
    # Stall detection (#413) — check for recent stall events
    if status == 'running':
        try:
            _stall_events = _db.list_pipeline_run_events(run_id, after_id=0, limit=100)
            _recent_stalls = [
                e for e in _stall_events
                if e.get('event_type') == 'stall_detected'
            ]
            if _recent_stalls:
                _last_stall = _recent_stalls[-1]
                _stall_meta = _json.loads(_last_stall.get('metadata_json', '{}'))
                _stall_msg = _stall_meta.get('message', 'No token progress detected')
                click.echo(f"├─ Warning:    ⚠️  {_stall_msg}")
        except Exception:
            pass
    # Scoring outcome (Issue #287)
    _scoring_status = run.get('scoring_status')
    _scoring_score = run.get('scoring_score')
    if _scoring_status is not None:
        _score_icon = {'passed': '✅', 'failed': '❌', 'error': '⚠️'}.get(_scoring_status, '❓')
        _score_suffix = f"  (score={_scoring_score:.3f})" if _scoring_score is not None else ""
        click.echo(f"├─ Scoring:    {_score_icon} {_scoring_status}{_score_suffix}")
    click.echo(f"├─ Created:    {(run.get('created_at') or '')[:19]}")
    click.echo(f"└─ Logs:       orch logs {run_id}")


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as Xm Ys or Xs."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m {s}s"


@main.command()
@click.option('--state', multiple=True, type=click.Choice([s.value for s in TaskState]),
              help='Filter by task state (can be used multiple times)')
@click.option('--type', 'task_type', multiple=True, type=click.Choice([t.value for t in TaskType]),
              help='Filter by task type (can be used multiple times)')
@click.option('--priority', multiple=True, type=click.Choice([p.name.lower() for p in Priority]),
              help='Filter by priority (can be used multiple times)')
@click.option('--orchestra-id', help='Filter by orchestra ID')
@click.option('--tag', multiple=True, help='Filter by tags')
@click.option('--limit', type=int, default=20, help='Maximum number of tasks to show')
@click.option('--offset', type=int, default=0, help='Number of tasks to skip')
@click.option('--format', 'output_format', type=click.Choice(['table', 'json']),
              default='table', help='Output format')
def list(
    state: tuple,
    task_type: tuple,
    priority: tuple,
    orchestra_id: Optional[str],
    tag: tuple,
    limit: int,
    offset: int,
    output_format: str
) -> None:
    """List tasks with optional filtering."""
    try:
        # Create filters
        filters = TaskFilters(
            states=[TaskState(s) for s in state] if state else None,
            types=[TaskType(t) for t in task_type] if task_type else None,
            priorities=[Priority[p.upper()] for p in priority] if priority else None,
            orchestra_id=orchestra_id,
            tags=list(tag) if tag else None,
            limit=limit,
            offset=offset
        )
        
        # Get tasks
        task_queue = get_queue()
        tasks = task_queue.list_tasks(filters)
        
        if output_format == 'json':
            # JSON output
            tasks_data = []
            for task in tasks:
                tasks_data.append({
                    'task_id': task.task_id,
                    'type': task.task_type.value,
                    'state': task.state.value,
                    'priority': task.priority.name,
                    'created_at': task.created_at.isoformat(),
                    'retry_count': task.retry_count,
                    'orchestra_id': task.orchestra_id,
                    'title': task.title,
                    'description': task.description,
                    'tags': task.tags
                })
            click.echo(json.dumps(tasks_data, indent=2))
        
        else:
            # Table output
            if not tasks:
                click.echo("No tasks found matching the criteria")
                return
            
            headers = [
                "Task ID", "Type", "State", "Priority", 
                "Created", "Retries", "Orchestra", "Title"
            ]
            
            rows = []
            for task in tasks:
                rows.append([
                    task.task_id[:8] + "...",  # Truncate task ID
                    task.task_type.value,
                    task.state.value,
                    task.priority.name,
                    format_datetime(task.created_at),
                    f"{task.retry_count}" if task.retry_count > 0 else "-",
                    task.orchestra_id[:8] + "..." if task.orchestra_id else "-",
                    task.title[:30] + "..." if task.title and len(task.title) > 30 else task.title or "-"
                ])
            
            print_table(headers, rows)
            
            if len(tasks) == limit:
                click.echo(f"\nShowing {limit} tasks (use --offset to see more)")
    
    except Exception as e:
        click.echo(f"Error listing tasks: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument('task_id')
@click.option('--force', is_flag=True, help='Force cancellation without confirmation')
def cancel(task_id: str, force: bool) -> None:
    """Cancel a queued or running task."""
    try:
        task_queue = get_queue()
        
        # Check if task exists first
        task_status = task_queue.get_task_status(task_id)
        if not task_status:
            click.echo(f"Task {task_id} not found", err=True)
            sys.exit(1)
        
        # Confirm cancellation unless forced
        if not force:
            click.echo(f"Task: {task_id}")
            click.echo(f"Type: {task_status.task_type.value}")
            click.echo(f"State: {task_status.state.value}")
            
            if not click.confirm("Are you sure you want to cancel this task?"):
                click.echo("Cancellation aborted")
                return
        
        # Cancel task
        success = task_queue.cancel_task(task_id)
        
        if success:
            click.echo(f"✓ Task {task_id} cancelled successfully")
        else:
            click.echo(f"✗ Failed to cancel task {task_id} (may not be in cancellable state)", err=True)
            sys.exit(1)
    
    except Exception as e:
        click.echo(f"Error cancelling task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument('task_id')
def retry(task_id: str) -> None:
    """Manually retry a failed task."""
    try:
        task_queue = get_queue()
        
        # Check task status first
        task_status = task_queue.get_task_status(task_id)
        if not task_status:
            click.echo(f"Task {task_id} not found", err=True)
            sys.exit(1)
        
        if task_status.state not in [TaskState.FAILED, TaskState.PERMANENTLY_FAILED]:
            click.echo(f"Task {task_id} is not in failed state (current: {task_status.state.value})", err=True)
            sys.exit(1)
        
        # Retry task
        success = task_queue.retry_failed_task(task_id)
        
        if success:
            click.echo(f"✓ Task {task_id} queued for retry")
        else:
            click.echo(f"✗ Failed to retry task {task_id} (may have exceeded max retries)", err=True)
            sys.exit(1)
    
    except Exception as e:
        click.echo(f"Error retrying task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option('--limit', type=int, default=20, help='Maximum number of dead letter tasks to show')
def dead_letter(limit: int) -> None:
    """Show tasks in the dead letter queue."""
    try:
        task_queue = get_queue()
        dead_tasks = task_queue.get_dead_letter_tasks(limit)
        
        if not dead_tasks:
            click.echo("No tasks in dead letter queue")
            return
        
        headers = [
            "Original ID", "Type", "Failure Reason", "Attempts", "Failed At"
        ]
        
        rows = []
        for task in dead_tasks:
            rows.append([
                task.original_task_id[:8] + "...",
                task.task_type.value,
                task.failure_reason[:50] + "..." if len(task.failure_reason) > 50 else task.failure_reason,
                str(task.failure_count),
                format_datetime(task.created_at)
            ])
        
        print_table(headers, rows)
        
        if len(dead_tasks) == limit:
            click.echo(f"\nShowing {limit} dead letter tasks")
    
    except Exception as e:
        click.echo(f"Error getting dead letter tasks: {e}", err=True)
        sys.exit(1)


@main.command()
def health() -> None:
    """Check system health and configuration."""
    try:
        task_queue = get_queue()
        stats = task_queue.get_queue_stats()
        
        # Basic health checks
        health_issues = []
        
        # Check queue depth
        if stats.queued > 100:
            health_issues.append(f"High queue depth: {stats.queued} tasks")
        
        # Check dead letter growth
        if stats.dead_letter_count > 50:
            health_issues.append(f"High dead letter count: {stats.dead_letter_count}")
        
        # Check worker utilization
        if stats.active_workers == 0 and stats.queued > 0:
            health_issues.append("No active workers but tasks are queued")
        
        # Display health status
        if health_issues:
            click.echo("⚠️  Health Issues Detected:")
            for issue in health_issues:
                click.echo(f"   • {issue}")
        else:
            click.echo("✅ System appears healthy")
        
        # Display configuration
        click.echo("\nConfiguration:")
        click.echo(f"├─ Database: {task_queue.db.db_path}")
        click.echo(f"├─ Max Workers: {stats.max_workers}")
        click.echo(f"└─ Active Workers: {stats.active_workers}")
    
    except Exception as e:
        click.echo(f"Error checking health: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument('task_id', required=True)
@click.option('--force', is_flag=True, help='Force execution even if worker pool is full')
@click.option('--model', type=str, help='Override model tier (haiku-4-5, sonnet-4, opus-4-6)')
@click.option('--timeout', type=int, help='Override timeout in seconds')
def execute(task_id: str, force: bool, model: Optional[str], timeout: Optional[int]) -> None:
    """Execute a specific queued task immediately by task ID."""
    try:
        # Import here to avoid circular imports during CLI parsing
        from .runner import TaskRunner
        from .config import get_global_config
        
        config = get_global_config()
        runner = TaskRunner(config=config)
        
        # Check if runner is already running
        if not runner._running:
            click.echo("Starting task runner...")
            runner.start()
            time.sleep(2)  # Give it a moment to start
        
        # Execute the task
        success = runner.execute_task_immediately(task_id)
        
        if success:
            click.echo(f"✅ Task {task_id} started successfully")
            click.echo(f"Use 'orch watch {task_id}' to monitor progress")
        else:
            click.echo(f"❌ Failed to start task {task_id}")
            sys.exit(1)
    
    except Exception as e:
        click.echo(f"Error executing task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument('run_id', required=True)
@click.option('--json-output', '--json', 'json_mode', is_flag=True, help='Machine-readable JSON output')
@click.option('--refresh', default=3, help='Refresh interval in seconds')
def watch(run_id: str, json_mode: bool, refresh: int) -> None:
    """Watch pipeline run progress in real-time (#414).

    Streams phase transitions, scoring results, warnings, and errors
    as they happen.  Exits when the pipeline reaches a terminal state.

    \b
    Examples:
      orch watch a3f8c2d1           # live-follow a running pipeline
      orch watch a3f8c2d1 --json    # machine-readable event stream

    Also works with legacy task IDs (falls back to old task-queue watch).
    """
    import json as _json

    # ── Try pipeline run first ──────────────────────────────────────
    try:
        _db = Database()
        run = _db.get_pipeline_run(run_id)
    except Exception:
        run = None

    if run is not None:
        _watch_pipeline_run(run_id, _db, json_mode, refresh)
        return

    # ── Fallback: legacy task-queue watch ────────────────────────────
    try:
        from .progress import ProgressTracker

        db = Database()
        tracker = ProgressTracker(db)

        def display_progress():
            progress = tracker.get_task_progress(run_id, include_events=True)
            if not progress:
                click.echo(f"❌ Task {run_id} not found")
                return False

            click.clear()
            click.echo(f"📊 Task Progress: {run_id}")
            click.echo("=" * 60)

            status_emoji = {
                "queued": "⏳", "running": "🔄", "success": "✅",
                "failed": "❌", "retry": "🔁", "permanently_failed": "💀",
                "cancelled": "🚫",
            }
            emoji = status_emoji.get(progress.current_state.value, "❓")
            click.echo(f"Status: {emoji} {progress.current_state.value.upper()}")

            if progress.current_message:
                click.echo(f"Message: {progress.current_message}")
            click.echo(f"Progress: {progress.progress_percentage:.1f}%")
            if progress.execution_time_seconds:
                click.echo(f"Runtime: {progress.execution_time_seconds:.1f}s")
            if progress.events:
                click.echo(f"\n📋 Recent Events ({len(progress.events)}):")
                for event in progress.events[-5:]:
                    timestamp = event.timestamp.strftime("%H:%M:%S")
                    click.echo(f"  {timestamp} - {event.event_type}: {event.message or 'N/A'}")
            return progress.is_active

        is_active = display_progress()
        if is_active:
            click.echo(f"\n🔄 Following (refresh every {refresh}s, Ctrl+C to stop)...")
            try:
                while is_active:
                    time.sleep(refresh)
                    is_active = display_progress()
                click.echo("\n✅ Task completed")
            except KeyboardInterrupt:
                click.echo("\n👋 Stopped")
    except Exception as e:
        click.echo(f"Error: '{run_id}' not found in pipeline runs or task queue.", err=True)
        sys.exit(1)


def _watch_pipeline_run(
    run_id: str, db: "Database", json_mode: bool, refresh: int
) -> None:
    """Stream pipeline run events in real-time (#414).

    Polls the pipeline_run_events table and prints formatted output
    as phases start, complete, stall, or score.
    """
    import json as _json

    terminal_states = {
        'success', 'failed', 'cancelled', 'crashed',
        'scoring_failed', 'pending_review', 'rejected',
    }

    last_event_id = 0
    last_status = None
    header_printed = False

    try:
        while True:
            run = db.get_pipeline_run(run_id)
            if run is None:
                click.echo(f"✗ Run '{run_id}' not found.", err=True)
                sys.exit(1)

            current_status = run['status']

            # Check PID liveness
            if current_status == 'running' and run.get('pid'):
                try:
                    from .daemon import is_process_alive
                    if not is_process_alive(run['pid']):
                        current_status = 'crashed'
                        db.update_pipeline_run(run_id, status='crashed')
                except Exception:
                    pass

            # Print header once
            if not header_printed:
                template = run.get('template_id', '?')
                click.echo(f"🔄 Pipeline {run_id} — {template}")
                header_printed = True

            # Fetch new events
            events = db.list_pipeline_run_events(run_id, after_id=last_event_id, limit=100)
            for evt in events:
                last_event_id = evt['id']
                _print_watch_event(evt, json_mode)

            # Status change
            if current_status != last_status:
                if current_status in terminal_states:
                    _scoring = run.get('scoring_status')
                    _score = run.get('scoring_score')
                    score_str = f" (score={_score:.3f})" if _score is not None else ""
                    icon = {'success': '✅', 'pending_review': '📋', 'failed': '❌',
                            'crashed': '💀', 'scoring_failed': '🔴'}.get(current_status, '❓')
                    ts = datetime.now().strftime('%H:%M:%S')
                    if json_mode:
                        click.echo(_json.dumps({
                            "time": ts, "type": "run_complete",
                            "status": current_status,
                            "scoring_status": _scoring,
                            "scoring_score": _score,
                        }))
                    else:
                        click.echo(
                            f"  [{ts}] 🏁 Pipeline complete — "
                            f"{icon} {current_status}{score_str}"
                        )
                    return
                last_status = current_status

            time.sleep(refresh)

    except KeyboardInterrupt:
        click.echo("\n👋 Stopped watching")


def _print_watch_event(evt: dict, json_mode: bool) -> None:
    """Format and print a single pipeline run event (#414)."""
    import json as _json

    event_type = evt.get('event_type', '')
    phase = evt.get('phase_id') or ''
    tokens = evt.get('tokens_consumed')
    state = evt.get('state')
    ts = datetime.now().strftime('%H:%M:%S')

    try:
        meta = _json.loads(evt.get('metadata_json', '{}'))
    except (TypeError, _json.JSONDecodeError):
        meta = {}

    if json_mode:
        click.echo(_json.dumps({
            "time": ts, "type": event_type, "phase": phase,
            "tokens": tokens, "state": state, "metadata": meta,
        }))
        return

    # Human-friendly formatting
    if event_type == 'phase_started':
        click.echo(f"  [{ts}] ▶ {phase} started")
    elif event_type == 'phase_completed':
        tokens_str = f", {tokens:,} tokens" if tokens else ""
        state_str = f" — {state}" if state else ""
        click.echo(f"  [{ts}] ✓ {phase} completed{tokens_str}{state_str}")
    elif event_type == 'stall_detected':
        msg = meta.get('message', 'No token progress detected')
        click.echo(f"  [{ts}] ⚠️  {msg}")
    elif event_type == 'status_changed':
        new_status = meta.get('new_status') or state or '?'
        click.echo(f"  [{ts}] 🔄 status → {new_status}")
    else:
        click.echo(f"  [{ts}] {event_type}: {phase or '(run)'}")

@main.command()
@click.option('--detailed', '-d', is_flag=True, help='Show detailed worker information')
def workers(detailed: bool) -> None:
    """Show active worker status."""
    try:
        # Import here to avoid circular imports
        from .concurrency import WorkerPool
        from .config import get_global_config
        from .db import Database
        
        config = get_global_config()
        db = Database()
        worker_pool = WorkerPool(db, config)
        
        status = worker_pool.get_worker_status()
        
        if not status:
            click.echo("No worker status available")
            return
        
        # Summary
        click.echo(f"👥 Worker Pool Status")
        click.echo("=" * 40)
        click.echo(f"Total Workers: {status['total_workers']}")
        click.echo(f"Max Workers: {status['max_workers']}")
        
        # Resource status
        if 'resource_status' in status:
            resources = status['resource_status']
            click.echo(f"\n💻 Resource Usage:")
            click.echo(f"  Sessions: {resources['current_sessions']}/{resources['max_sessions']} "
                      f"({resources['session_utilization']:.1f}%)")
            
            if resources.get('daily_budget_usd'):
                click.echo(f"  Budget: ${resources['daily_cost_usd']:.2f}/"
                          f"${resources['daily_budget_usd']:.2f} "
                          f"({resources['budget_utilization']:.1f}%)")
            else:
                click.echo(f"  Daily Cost: ${resources['daily_cost_usd']:.2f}")
        
        # Workers by state
        if 'workers_by_state' in status and status['workers_by_state']:
            click.echo(f"\n📊 Workers by State:")
            
            for state, workers in status['workers_by_state'].items():
                count = len(workers)
                state_emoji = {
                    'idle': '😴',
                    'assigned': '📝',
                    'running': '🏃',
                    'stale': '💀',
                    'terminated': '🚫'
                }
                
                emoji = state_emoji.get(state, '❓')
                click.echo(f"  {emoji} {state.capitalize()}: {count}")
                
                if detailed and workers:
                    for worker in workers[:3]:  # Show first 3 workers
                        worker_id = worker['worker_id'][:12] + "..." if len(worker['worker_id']) > 15 else worker['worker_id']
                        age = worker.get('heartbeat_age_seconds', 0)
                        
                        task_info = ""
                        if worker.get('assigned_task_id'):
                            task_id = worker['assigned_task_id'][:8] + "..."
                            task_info = f" (task: {task_id})"
                        
                        click.echo(f"    • {worker_id} - {age:.0f}s ago{task_info}")
                    
                    if len(workers) > 3:
                        click.echo(f"    ... and {len(workers) - 3} more")
        else:
            click.echo("No active workers")
    
    except Exception as e:
        click.echo(f"Error getting worker status: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Template-based pipeline commands
# ---------------------------------------------------------------------------

@main.command("run")
@click.argument('template_name_or_file')
@click.option(
    '--mode',
    type=click.Choice(['standalone', 'openclaw', 'openrouter', 'dry-run']),
    default='standalone',
    show_default=True,
    help='Execution mode: standalone (direct API), openclaw (sub-agent), openrouter (multi-provider), dry-run (mock).',
)
@click.option(
    '--api-key',
    envvar='ANTHROPIC_API_KEY',
    default=None,
    help='API key for standalone (ANTHROPIC_API_KEY) or openrouter (OPENROUTER_API_KEY) mode.',
)
@click.option(
    '--input', 'input_json',
    default=None,
    help='Pipeline input as a JSON string, e.g. \'{"brief": "AI safety"}\'.',
)
@click.option(
    '--input-file',
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help='Path to a JSON file containing pipeline input.',
)
@click.option(
    '--output-dir',
    type=click.Path(path_type=Path),
    default=None,
    help='Directory to write phase outputs. Created if missing. '
         'Defaults to ./output/<template-id>-<YYYYMMDD-HHMMSS>/',
)
@click.option(
    '--gateway-url',
    default=None,
    help='OpenClaw gateway URL for openclaw mode (or set OPENCLAW_GATEWAY_URL).',
)
@click.option(
    '--gateway-token',
    default=None,
    help='OpenClaw gateway bearer token for openclaw mode (or set OPENCLAW_GATEWAY_TOKEN).',
)
@click.option(
    '--dry-run-delay',
    type=float,
    default=0.0,
    hidden=True,
    help='(dry-run mode only) Simulated per-phase delay in seconds.',
)
@click.option(
    '--dry-run-failure-rate',
    type=float,
    default=0.0,
    hidden=True,
    help='(dry-run mode only) Probability [0.0-1.0] of simulated phase failure.',
)
@click.option(
    '--skip-scoring',
    is_flag=True,
    default=False,
    help='Skip auto-scoring even if the template declares a scenario.',
)
@click.option(
    '--score-only',
    is_flag=True,
    default=False,
    help=(
        'Run scoring on an existing output directory without re-running the pipeline. '
        'Requires --output-dir pointing to a completed run.'
    ),
)
@click.option(
    '--issue',
    'issue_number',
    type=int,
    default=None,
    help='GitHub issue number to auto-fetch and merge into pipeline input.',
)
@click.option(
    '--repo',
    'repo',
    default=None,
    help='GitHub repository slug (e.g. owner/repo) for --issue lookup.',
)
@click.option(
    '--executor',
    type=click.Choice(['api', 'claudecode', 'auto']),
    default='auto',
    show_default=True,
    help='Executor backend for standalone mode: api (ANTHROPIC_API_KEY), claudecode, or auto.',
)
@click.option(
    '--model-map',
    'model_map_json',
    default=None,
    help='JSON model tier overrides for openrouter mode, e.g. \'{"sonnet": "openai/gpt-4o"}\'.',
)
def run_template(
    template_name_or_file: str,
    mode: str,
    api_key: Optional[str],
    input_json: Optional[str],
    input_file: Optional[Path],
    output_dir: Optional[Path],
    gateway_url: Optional[str],
    gateway_token: Optional[str],
    dry_run_delay: float,
    dry_run_failure_rate: float,
    skip_scoring: bool,
    score_only: bool,
    issue_number: Optional[int],
    repo: Optional[str],
    executor: str,
    model_map_json: Optional[str],
) -> None:
    """Execute a pipeline template end-to-end.

    TEMPLATE_NAME_OR_FILE is a template name (e.g. content-pipeline) or a
    path to a YAML file.  Template names are resolved using the search order:
    ORCH_TEMPLATES_PATH → ./templates/ → ~/.orch/templates/ → bundled.

    Examples:

      # By name (resolved automatically):
      orch run content-pipeline --mode dry-run

      # By path:
      orch run pipeline.yaml --mode standalone \\
        --api-key sk-ant-... \\
        --input '{"brief": "AI safety"}'

      # Standalone reading input from file:
      orch run pipeline.yaml --input-file brief.json \\
        --output-dir ./results/

      # OpenClaw sub-agent mode:
      orch run pipeline.yaml --mode openclaw

      # Dry-run (no API calls):
      orch run pipeline.yaml --mode dry-run
    """
    import json as _json

    # --executor is only valid with --mode standalone (dry-run ignores it; openclaw/openrouter are incompatible)
    if mode in ('openclaw', 'openrouter') and executor != 'auto':
        click.echo("Error: --executor is only valid with --mode standalone", err=True)
        sys.exit(1)

    # --model-map is only valid with --mode openrouter
    if model_map_json and mode != 'openrouter':
        click.echo("Error: --model-map is only valid with --mode openrouter", err=True)
        sys.exit(1)

    from rich.console import Console
    from rich.table import Table

    from .templates import TemplateEngine
    from .pipeline_runner import PipelineRunner
    from .sequencer import PhaseSequencer, StateMachineSequencer
    from .heartbeat import ProgressHeartbeat

    import sys as _sys
    # Force plain text output in non-TTY environments (background, nohup, pipes)
    console = Console(
        highlight=False,
        force_terminal=_sys.stdout.isatty(),
        no_color=not _sys.stdout.isatty(),
    )
    run_start = time.time()

    # --- 1. Resolve template path (name or path) ----------------------
    template_file = _resolve_template_arg(template_name_or_file)

    # --- 2. Load and validate template --------------------------------
    try:
        engine = TemplateEngine()
        template = engine.load_template(template_file)
    except FileNotFoundError as exc:
        click.echo(f"✗ Template file not found: {exc}", err=True)
        sys.exit(1)
    except (KeyError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"✗ Invalid template: {exc}", err=True)
        sys.exit(1)

    errors = engine.validate_template(template)
    # Issue #295: --skip-scoring opts out of the mandatory-scenario check
    if skip_scoring:
        errors = [e for e in errors if "require a scenario" not in e]
    if errors:
        click.echo(f"✗ Template has {len(errors)} structural error(s):", err=True)
        for err in errors:
            click.echo(f"  • {err}", err=True)
        sys.exit(1)

    # --- Score-only mode (Issue #172) ---------------------------------
    if score_only:
        if output_dir is None:
            click.echo(
                "✗ --score-only requires --output-dir pointing to a completed run.",
                err=True,
            )
            sys.exit(1)
        if not template.scenario:
            click.echo(
                "✗ Template has no 'scenario' field — nothing to score.",
                err=True,
            )
            sys.exit(1)
        from .scoring import run_scoring as _run_scoring
        # Build an executor for the LLM judge grader so that scoring uses the
        # same authentication path as the mode specified by the caller.
        # In openclaw mode this routes judge calls through the gateway token
        # instead of a raw ANTHROPIC_API_KEY.  Issue #272.
        import os as _os_so
        _so_executor = None
        if mode == "openclaw":
            try:
                from .openclaw_executor import OpenClawExecutor
                _so_url = gateway_url or _os_so.environ.get("OPENCLAW_GATEWAY_URL")
                _so_token = gateway_token or _os_so.environ.get("OPENCLAW_GATEWAY_TOKEN")
                _so_executor = OpenClawExecutor(
                    gateway_url=_so_url,
                    gateway_token=_so_token,
                )
            except Exception as _so_exc:
                click.echo(
                    f"⚠ Could not create OpenClawExecutor for grader: {_so_exc}\n"
                    "  LLM judge criteria will fall back to ANTHROPIC_API_KEY.",
                    err=True,
                )
        elif mode == "standalone" and api_key:
            try:
                from .executors.anthropic_executor import AnthropicExecutor
                _so_executor = AnthropicExecutor(api_key=api_key)
            except Exception:
                pass  # fall back to ANTHROPIC_API_KEY env var
        _run_scoring(
            template,
            output_dir=output_dir,
            console=console,
            template_file=template_file,
            exit_on_failure=True,
            executor=_so_executor,
        )
        sys.exit(0)

    # --- 1b. Default output directory (Feature #72) ------------------
    from uuid import uuid4
    run_id = str(uuid4())[:8]
    if output_dir is None:
        _safe_id = re.sub(r'[^\w\-]', '_', template.id)
        _ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        output_dir = Path(f"./output/{_safe_id}-{_ts}-{run_id}")

    # --- 2. Resolve pipeline input ------------------------------------
    if input_file and input_json:
        click.echo("⚠ Both --input and --input-file provided; using --input-file", err=True)

    initial_input: Dict[str, Any] = {}
    if input_file:
        try:
            initial_input = _json.loads(input_file.read_text())
        except (_json.JSONDecodeError, OSError) as exc:
            click.echo(f"✗ Could not read input file: {exc}", err=True)
            sys.exit(1)
    elif input_json:
        try:
            initial_input = _json.loads(input_json)
        except _json.JSONDecodeError as exc:
            click.echo(f"✗ Invalid JSON in --input: {exc}", err=True)
            sys.exit(1)

    # --- 2b. Auto-fetch GitHub issue data (#507) ---
    if issue_number is not None:
        if not repo:
            click.echo("⚠ --issue requires --repo (e.g. owner/repo). Skipping GitHub fetch.", err=True)
        else:
            try:
                from .github_fetcher import fetch_github_issue
                issue_data = fetch_github_issue(repo=repo, issue_number=issue_number)
                if issue_data:
                    initial_input = issue_data.merge_into(initial_input)
                    click.echo(
                        f"  ✓ GitHub issue #{issue_number} fetched: {issue_data.title!r}",
                        err=True,
                    )
                else:
                    click.echo(
                        f"  ⚠ Could not fetch GitHub issue #{issue_number} from {repo} — continuing with provided input.",
                        err=True,
                    )
            except Exception as _gh_exc:
                click.echo(
                    f"  ⚠ GitHub fetch error: {_gh_exc} — continuing with provided input.",
                    err=True,
                )

    # --- 2c. Validate required config fields (#411) ---
    missing = _validate_required_config(template, initial_input)
    # Apply schema defaults for optional fields (#835) — runs AFTER required-
    # field validation so missing-required errors are reported on the original
    # input, but BEFORE the sequencer reads `initial_input` for prompt
    # rendering. Without this, pre-v2.1 consumers running `orch run` against
    # the v2.1.0 standard pipeline would see <MISSING:ui_primitive_paths>
    # (and similar) literals rendered into Phase 0 prompts.
    apply_config_schema_defaults(initial_input, getattr(template, 'config_schema', None))
    if missing:
        if mode == 'dry-run':
            # In dry-run mode, missing required fields are non-fatal: phases run with
            # synthetic output anyway, so warn but continue (issue #659).
            click.echo(
                f"⚠ Dry-run: {len(missing)} required field(s) not provided "
                f"({', '.join(missing)}) — continuing with synthetic output.",
                err=True,
            )
        else:
            click.echo(f"✗ Missing {len(missing)} required config field(s):", err=True)
            for field in missing:
                click.echo(f"  • {field}", err=True)
            click.echo(
                "\nThese fields are required by the template's config_schema. "
                "Add them to your --input or --input-file JSON.",
                err=True,
            )
            sys.exit(1)

    # --- 3. Build PipelineRunner based on mode -----------------------
    try:
        if mode == 'standalone':
            runner = PipelineRunner.standalone(api_key=api_key, executor_type=executor)
        elif mode == 'openclaw':
            # Read env vars only when actually needed (avoid leaking in dry-run tracebacks)
            import os as _os_env
            effective_url = gateway_url or _os_env.environ.get("OPENCLAW_GATEWAY_URL")
            effective_token = gateway_token or _os_env.environ.get("OPENCLAW_GATEWAY_TOKEN")
            runner = PipelineRunner.openclaw(
                gateway_url=effective_url,
                gateway_token=effective_token,
            )
        elif mode == 'openrouter':
            import os as _os_env
            effective_key = api_key or _os_env.environ.get("OPENROUTER_API_KEY", "")
            model_map = None
            if model_map_json:
                try:
                    model_map = json.loads(model_map_json)
                except json.JSONDecodeError as e:
                    click.echo(f"Error: --model-map is not valid JSON: {e}", err=True)
                    sys.exit(1)
            runner = PipelineRunner.openrouter(api_key=effective_key, model_map=model_map)
        else:  # dry-run
            runner = PipelineRunner.dry_run(
                delay_seconds=dry_run_delay,
                failure_rate=dry_run_failure_rate,
            )
    except ValueError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)

    # --- 4. Execute pipeline -----------------------------------------
    n_phases = len(template.phases)
    console.print(
        f"[bold]Pipeline:[/bold] {template.name!r}  "
        f"({n_phases} phase{'s' if n_phases != 1 else ''})"
    )
    console.print(f"[bold]Mode:[/bold]     {mode}")
    console.print(f"[bold]Output:[/bold]   {output_dir}/")
    console.print()

    # --- 3b. Non-TTY heartbeat (Issue #186) --------------------------
    # In non-TTY environments (piped, nohup, CI) the CLI goes silent between
    # phase completions.  ProgressHeartbeat emits a status line every 30s so
    # operators can confirm the pipeline is alive.  In TTY mode (isatty==True)
    # the class is automatically inactive and the Rich progress bar is
    # unaffected.
    heartbeat = ProgressHeartbeat(
        total_phases=n_phases,
        start_time=run_start,
    )

    # --- 3c. Set up git integration if template has it enabled --------
    from .git_integration import GitContext, GitError

    git_ctx: Optional[GitContext] = None
    _gate_result = None  # MergeGateResult set by on_pipeline_complete hook

    if (
        template.git_config is not None
        and template.git_config.enabled
        and mode != 'dry-run'
    ):
        git_ctx = GitContext(
            config=template.git_config,
            pipeline_id=template.id,
            run_id=run_id,
            output_dir=output_dir,
            issue_number=initial_input.get("issue_number"),  # None if not in input
        )
        console.print(
            f"[bold]Git:[/bold]      enabled  "
            f"(branch_pattern={template.git_config.branch_pattern!r})"
        )
    elif (
        template.git_config is not None
        and template.git_config.enabled
        and mode == 'dry-run'
    ):
        console.print(
            "[yellow]Git:[/yellow]      skipped in dry-run mode"
        )

    # Build git lifecycle hooks (no-ops when git_ctx is None)
    def _on_pipeline_start_hook(pipeline_context: dict) -> None:
        """Create feature branch and populate pipeline_context."""
        if git_ctx is None:
            return
        try:
            branch_info = git_ctx.on_pipeline_start()
            pipeline_context["branch_name"] = branch_info.branch_name
            pipeline_context["base_branch"] = branch_info.base_branch
            console.print(
                f"  [cyan]git[/cyan] branch created: "
                f"[bold]{branch_info.branch_name}[/bold] "
                f"(from {branch_info.base_branch})"
            )
        except GitError as exc:
            console.print(f"  [red]✗ git setup failed:[/red] {exc}", highlight=False)
            raise

    def _on_phase_complete_git(phase_id: str, phase_result: dict) -> None:
        """Stage + commit after code phases, and refresh diff in context."""
        if git_ctx is None:
            return
        try:
            commit = git_ctx.on_phase_complete(phase_id, phase_result)
            if commit:
                console.print(
                    f"  [cyan]git[/cyan] committed phase '{phase_id}' → "
                    f"{commit.sha[:8]}  ({commit.files_changed} file(s))"
                )
            # Refresh diff in context so downstream review phases see it
            diff = git_ctx.get_branch_diff()
            if diff and hasattr(git_ctx, '_branch_info') and git_ctx._branch_info:
                # Update pipeline_context (accessed via sequencer reference)
                _pipeline_context_ref[0]["git_diff"] = diff
        except GitError as exc:
            console.print(f"  [yellow]⚠ git commit failed:[/yellow] {exc}", highlight=False)

    # We need a mutable reference so the nested closure can update pipeline_context.
    # The list is populated after the sequencer is created.
    _pipeline_context_ref: list = [{}]

    def _on_pipeline_complete_hook(pipeline_context: dict, result: Optional[dict]) -> None:
        """Push and enter merge gate (or cleanup on failure)."""
        nonlocal _gate_result
        if git_ctx is None:
            return
        success = result is not None and not result.get("aborted", False)
        try:
            gate_result = git_ctx.on_pipeline_complete(success=success)
            _gate_result = gate_result
        except GitError as exc:
            console.print(
                f"  [yellow]⚠ git pipeline-complete failed:[/yellow] {exc}",
                highlight=False,
            )
        finally:
            git_ctx.cleanup(success=success)

    # Heartbeat phase-start callback (Issue #186)
    def _on_phase_start_cb(phase_id: str, phase, wave_index: int) -> None:
        """Notify the heartbeat that a new phase has started.

        Updates the heartbeat's current-phase display so the next emitted
        line shows the correct phase name.  This is a no-op when the
        heartbeat is inactive (TTY mode).
        """
        heartbeat.set_current_phase(phase_id)

    # Live phase-completion callback (Feature #70)
    def _on_phase_complete(phase_id: str, phase_result: dict) -> None:
        _st = phase_result.get('state', 'unknown')
        state_val = _st.value if hasattr(_st, 'value') else str(_st)
        tokens = phase_result.get('tokens_consumed', 0)
        cost = phase_result.get('cost_usd', 0)
        cost_str = f"${float(cost):.4f}" if cost else "n/a"
        safe_pid = re.sub(r'[^\w\-]', '_', phase_id)
        if state_val in ('failed', 'permanently_failed'):
            console.print(
                f"  [red]✗[/red] {safe_pid:30s}  state={state_val}  "
                f"tokens={tokens}  cost={cost_str}"
            )
        else:
            console.print(
                f"  [green]✓[/green] {safe_pid:30s}  state={state_val}  "
                f"tokens={tokens}  cost={cost_str}"
            )
        # Advance the heartbeat completed-phase counter (Issue #186).
        # Counts both successful and failed phases (pipeline aborts on first failure).
        heartbeat.on_phase_complete()

        # Write phase output to disk immediately (#239 follow-up).
        # This allows the sequencer to read from disk for prompt building,
        # avoiding truncation from in-memory session history capture.
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            phase_text = _extract_output_text(phase_result)
            if phase_text:
                out_path = output_dir / f"{safe_pid}.md"
                new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
                _safe_write_phase_output(out_path, new_content, phase_id)
        except Exception as exc:
            logger.warning(f"Failed to write phase output to disk: {exc}")

        # Run git commit hook after progress display
        _on_phase_complete_git(phase_id, phase_result)

    with runner:
        _has_transitions = any(p.transitions for p in template.phases) or bool(
            template.default_transitions
        )
        _SequencerClass = StateMachineSequencer if _has_transitions else PhaseSequencer
        sequencer = _SequencerClass(
            template, runner, config=initial_input,
            on_phase_complete=_on_phase_complete,
            on_phase_start=_on_phase_start_cb,
            on_pipeline_start=_on_pipeline_start_hook,
            on_pipeline_complete=_on_pipeline_complete_hook,
            output_dir=output_dir,
        )
        # Give the git diff closure access to the sequencer's pipeline_context
        _pipeline_context_ref[0] = sequencer.pipeline_context

        try:
            # Start the non-TTY heartbeat for the duration of pipeline execution.
            # In TTY mode the heartbeat is automatically inactive, so this is a no-op.
            with heartbeat:
                result = sequencer.execute(initial_input)
        except GitError as exc:
            console.print(f"\n[red]✗ Git error:[/red] {exc}", highlight=False)
            if git_ctx:
                git_ctx.cleanup(success=False)
            sys.exit(1)
        except Exception as exc:
            click.echo(f"✗ Pipeline execution crashed: {exc}", err=True)
            if git_ctx:
                git_ctx.cleanup(success=False)
            sys.exit(1)

    # --- 5. Report result (Feature #70 — rich summary table) ---------
    if result.get('aborted'):
        failed_phase = result.get('failed_phase', 'unknown')
        click.echo(f"✗ Pipeline aborted at phase '{failed_phase}'", err=True)
        click.echo(f"  Completed phases: {[*result['phase_outputs'].keys()]}", err=True)
        sys.exit(2)

    completed_phases = [*result['phase_outputs'].keys()]
    elapsed = time.time() - run_start

    # Build rich summary table
    table = Table(
        title=f"Pipeline completed — {len(completed_phases)} phases in {elapsed:.1f}s",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Phase", style="cyan", no_wrap=True)
    table.add_column("State", justify="center")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")

    total_tokens = 0
    total_cost = 0.0
    for phase_id in completed_phases:
        safe_id = re.sub(r'[^\w\-]', '_', phase_id)
        out = result['phase_outputs'][phase_id]
        _state = out.get('state', 'unknown')
        state = _state.value if hasattr(_state, 'value') else str(_state)
        tokens = out.get('tokens_consumed', 0)
        cost = out.get('cost_usd', 0)
        cost_float = float(cost) if cost else 0.0
        cost_str = f"${cost_float:.4f}" if cost else "n/a"
        total_tokens += tokens
        total_cost += cost_float
        state_display = (
            f"[green]✓ {state}[/green]"
            if state == 'success'
            else f"[red]✗ {state}[/red]"
        )
        table.add_row(safe_id, state_display, str(tokens), cost_str)

    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "",
        f"[bold]{total_tokens}[/bold]",
        f"[bold]${total_cost:.4f}[/bold]",
    )
    console.print()
    console.print(table)

    # --- 5b. Git merge gate output ------------------------------------
    if _gate_result is not None:
        if _gate_result.status == "awaiting_approval":
            console.print()
            console.print("[bold cyan]═══ Merge Gate ═══════════════════════════[/bold cyan]")
            console.print(
                f"  Branch ready for review.  Run ID: [bold]{run_id}[/bold]"
            )
            if git_ctx and git_ctx._branch_info:
                console.print(
                    f"  Branch: [bold]{git_ctx._branch_info.branch_name}[/bold]"
                    f" → {git_ctx._branch_info.base_branch}"
                )
            console.print()
            console.print(f"  [green]Approve:[/green]  orch gate approve {run_id}")
            console.print(f"  [red]Reject:[/red]   orch gate reject {run_id}")
            console.print(f"  [cyan]Info:[/cyan]     orch gate info {run_id}")
            console.print("[bold cyan]══════════════════════════════════════════[/bold cyan]")
        elif _gate_result.status == "skipped" and _gate_result.message:
            console.print(f"\n[dim]Git: {_gate_result.message}[/dim]")

    # --- 6. Write outputs to disk (always — default dir if not specified) ---
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[yellow]⚠ Could not create output directory: {exc}[/yellow]", stderr=True)
        sys.exit(0)  # Pipeline succeeded, just can't write

    for phase_id, phase_out in result['phase_outputs'].items():
        safe_id = re.sub(r'[^\w\-]', '_', phase_id)

        # JSON (existing behaviour)
        (output_dir / f"{safe_id}.json").write_text(
            _json.dumps(phase_out, indent=2, default=str)
        )

        # Markdown per phase (Feature #71)
        # Issue #210: if the sub-agent already wrote a larger file, keep it.
        phase_text = _extract_output_text(phase_out)
        out_path = output_dir / f"{safe_id}.md"
        new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
        _safe_write_phase_output(out_path, new_content, phase_id)

    # _final_output.json
    (output_dir / "_final_output.json").write_text(
        _json.dumps(result.get('final_output', {}), indent=2, default=str)
    )

    # _final_output.md (Feature #71)
    final_text = _extract_output_text(result.get('final_output', {}))
    (output_dir / "_final_output.md").write_text(f"# Final Output\n\n{final_text}\n")

    # _summary.md (Feature #71)
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary_lines = [
        f"# Run Summary: {template.name}",
        "",
        f"**Date:** {run_date}",
        f"**Template ID:** {template.id}",
        f"**Mode:** {mode}",
        f"**Elapsed:** {elapsed:.1f}s",
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
        summary_lines.append(f"| {safe_id} | {state} | {tokens} | {cost_str} |")
    summary_lines += [
        "",
        f"**Total Tokens:** {total_tokens}",
        f"**Total Cost:** ${total_cost:.4f}",
        "",
    ]
    (output_dir / "_summary.md").write_text("\n".join(summary_lines))

    console.print(f"\n[bold]Outputs written to:[/bold] {output_dir}/")

    # --- Auto-scoring (Issue #172) ------------------------------------
    if not skip_scoring and template.scenario:
        from .scoring import run_scoring as _run_scoring_auto
        from .git_integration import GitContext as _GitContext
        # Forward the pipeline executor so that LLM judge criteria are routed
        # through the same authentication path as the pipeline itself (e.g. the
        # OpenClaw subscription token in openclaw mode).  Issue #272.
        _scoring_executor = runner.executors[0] if runner.executors else None
        _scoring_passed: Optional[bool] = None
        _scoring_score_val: Optional[float] = None
        try:
            # Use exit_on_failure=False so we can persist results to the gate
            # file *before* exiting.  We replicate the exit logic below after
            # updating the gate.
            _scoring_passed, _scoring_score_val = _run_scoring_auto(
                template,
                output_dir=output_dir,
                console=console,
                template_file=template_file,
                exit_on_failure=False,
                executor=_scoring_executor,
            )
        except SystemExit as _se:
            # Guard: if scoring unexpectedly exits, treat as error
            _scoring_passed = False
            _scoring_score_val = None

        # Persist scoring results to the gate file (Issue #289)
        if _gate_result is not None and _gate_result.status == "awaiting_approval":
            if _scoring_passed is None:
                _gate_scoring_status = "error"
            elif _scoring_passed:
                _gate_scoring_status = "passed"
            else:
                _gate_scoring_status = "failed"
            try:
                _GitContext.update_gate_scoring(
                    run_id, _gate_scoring_status, _scoring_score_val
                )
            except Exception as _ge:
                logger.warning(f"Could not update gate scoring: {_ge}")

        # Now enforce exit on scoring failure (replaces exit_on_failure=True)
        if _scoring_passed is False:
            sys.exit(1)


# ---------------------------------------------------------------------------
# Issue #267 — Async pipeline execution: start / status / logs / wait / resume
# ---------------------------------------------------------------------------

def _get_persistent_db_path() -> str:
    """Return the path to the persistent on-disk DB used by async runs.

    Thin string-returning wrapper around :func:`orchestration_engine.db.default_db_path`
    preserved for callsite signature compatibility (Issue #864 consolidation).
    """
    return str(default_db_path())


@main.command("launch")
@click.argument('template_name_or_file')
@click.option(
    '--mode',
    type=click.Choice(['standalone', 'openclaw', 'openrouter', 'dry-run']),
    default='standalone',
    show_default=True,
    help='Execution mode.',
)
@click.option(
    '--input', 'input_json',
    default=None,
    help='Pipeline input as a JSON string.',
)
@click.option(
    '--input-file',
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help='Path to a JSON file containing pipeline input.',
)
@click.option(
    '--issue',
    'issue_number',
    type=int,
    default=None,
    help=(
        'GitHub issue number to auto-fetch as pipeline input. '
        'Fetched fields (title, body, labels, assignees, milestone) are '
        'merged as the base input; explicit --input / --input-file keys take '
        'precedence over fetched values.'
    ),
)
@click.option(
    '--repo',
    default=None,
    envvar='GITHUB_REPOSITORY',
    help=(
        'Repository slug (owner/repo) used with --issue to fetch issue data. '
        'Defaults to the GITHUB_REPOSITORY environment variable when set.'
    ),
)
@click.option(
    '--branch',
    'branch_name_override',
    default=None,
    help='Override the auto-generated branch name (Issue #591).',
)
@click.option(
    '--test-command',
    'test_command_override',
    default=None,
    help='Override the default test command for the pipeline (Issue #591).',
)
@click.option(
    '--output-dir',
    type=click.Path(path_type=Path),
    default=None,
    help='Directory to write phase outputs (created if missing).',
)
@click.option(
    '--gateway-url',
    default=None,
    envvar='OPENCLAW_GATEWAY_URL',
    help='OpenClaw gateway URL for openclaw mode.',
)
@click.option(
    '--gateway-token',
    default=None,
    help='OpenClaw gateway bearer token for openclaw mode (or set OPENCLAW_GATEWAY_TOKEN).',
)
@click.option(
    '--skip-scoring',
    is_flag=True,
    default=False,
    help='Skip auto-scoring even if the template declares a scenario.',
)
@click.option(
    '--db-path',
    default=None,
    help='Override path to the persistent pipeline-runs DB.',
)
@click.option(
    '--executor',
    type=click.Choice(['api', 'claudecode', 'auto']),
    default='auto',
    show_default=True,
    help='Executor backend for standalone mode: api (ANTHROPIC_API_KEY), claudecode, or auto.',
)
def pipeline_launch(
    template_name_or_file: str,
    mode: str,
    input_json: Optional[str],
    input_file: Optional[Path],
    issue_number: Optional[int],
    repo: Optional[str],
    branch_name_override: Optional[str],
    test_command_override: Optional[str],
    output_dir: Optional[Path],
    gateway_url: Optional[str],
    gateway_token: Optional[str],
    skip_scoring: bool,
    db_path: Optional[str],
    executor: str,
) -> None:
    """Launch a pipeline in the background and return immediately.

    Spawns a daemon process that runs the pipeline, then exits.  Use
    'orch status <run-id>' to check progress.

    \b
    Examples:
      orch launch content-pipeline --mode openclaw --input '{"brief": "AI"}'
      orch launch coding-pipeline-v1 --issue 42 --repo owner/repo
      orch launch coding-pipeline-v1 --issue 42 --branch my-feature
      orch status <run-id>
      orch logs <run-id> --follow
      orch wait <run-id>
    """
    import uuid

    from .templates import TemplateEngine

    # --- Validate issue_number (Issue #591): must be positive integer ---
    if issue_number is not None and issue_number <= 0:
        click.echo("Error: Issue number must be a positive integer.", err=True)
        sys.exit(1)

    # --executor is only valid with --mode standalone (openclaw/openrouter are incompatible)
    if mode in ('openclaw', 'openrouter') and executor != 'auto':
        click.echo("Error: --executor is only valid with --mode standalone", err=True)
        sys.exit(1)

    # --- Resolve template (with launch_fmt error messages) ---
    template_file = _resolve_template_arg(template_name_or_file, launch_fmt=True)

    try:
        engine = TemplateEngine()
        template = engine.load_template(template_file)
    except (FileNotFoundError, KeyError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"✗ Template error: {exc}", err=True)
        sys.exit(1)

    errors = engine.validate_template(template)
    # Issue #295: --skip-scoring opts out of the mandatory-scenario check
    if skip_scoring:
        errors = [e for e in errors if "require a scenario" not in e]
    if errors:
        click.echo(f"✗ Template has {len(errors)} error(s):", err=True)
        for err in errors:
            click.echo(f"  • {err}", err=True)
        sys.exit(1)

    # --- Resolve input ---
    if input_file and input_json:
        click.echo("⚠ Both --input and --input-file provided; using --input-file", err=True)

    initial_input: Dict[str, Any] = {}
    if input_file:
        try:
            initial_input = json.loads(input_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(f"✗ Could not read input file: {exc}", err=True)
            sys.exit(1)
    elif input_json:
        try:
            initial_input = json.loads(input_json)
        except json.JSONDecodeError as exc:
            click.echo(f"✗ Invalid JSON in --input: {exc}", err=True)
            sys.exit(1)

    # --- Issue #591: Auto-infer git context + strict issue fetch ---
    if issue_number is not None:
        repo_path_inferred, repo_url_inferred = _infer_git_context()

        # Determine effective_repo for the GitHub API call
        effective_repo = repo  # from --repo flag if provided
        if not effective_repo:
            if repo_url_inferred:
                # Extract owner/repo from the normalized HTTPS URL
                m = re.match(r'https://[^/]+/(.+)', repo_url_inferred)
                effective_repo = m.group(1) if m else None

        if not effective_repo:
            # No --repo flag and no origin remote to infer from
            if repo_path_inferred is None:
                click.echo(
                    "Error: Not inside a git repository. Use --repo to specify the path.",
                    err=True,
                )
            else:
                click.echo(
                    "Error: Cannot determine GitHub repository from git remote. "
                    "Use --repo to specify it.",
                    err=True,
                )
            sys.exit(1)

        # Fetch issue with strict error handling
        issue_raw = _fetch_issue_strict(effective_repo, issue_number)

        # Inject canonical fields (always overwrite, never from --input-file)
        issue_fields = {
            'issue_number': issue_raw['number'],
            'issue_title': issue_raw['title'],
            'issue_body': issue_raw.get('body', ''),
        }
        initial_input.update(issue_fields)

        # Inject repo_path from git root if not already in input
        if repo_path_inferred is not None and 'repo_path' not in initial_input:
            initial_input['repo_path'] = repo_path_inferred
        # Note: if --repo provided and CWD is outside git, repo_path_inferred is None
        # repo_path is simply omitted (missing-fields validation catches it if required)

        # Determine repo_url for pipeline input
        if repo:
            # --repo explicitly given → construct HTTPS URL from slug
            initial_input.setdefault('repo_url', f"https://github.com/{repo}")
        elif repo_url_inferred and 'repo_url' not in initial_input:
            initial_input['repo_url'] = repo_url_inferred

        # Generate branch_name as default (--branch override applied later)
        if 'branch_name' not in initial_input:
            slug = _slugify_title(issue_raw['title'])
            initial_input['branch_name'] = f"fix/{issue_number}-{slug}"

        # --test-command injection
        if test_command_override:
            initial_input['test_command'] = test_command_override

    # --- --branch always wins (over --input-file, auto-generated, issue-inferred) ---
    # This runs unconditionally — not gated on issue_number — so --branch works
    # both with and without --issue (Issue #591).
    if branch_name_override:
        initial_input['branch_name'] = branch_name_override

    # If --test-command was provided without --issue, inject it now
    if test_command_override and issue_number is None:
        initial_input['test_command'] = test_command_override

    # --- Persist executor_type so the daemon can forward it to the runner factory ---
    # Only stored when non-default to avoid polluting input for the common case.
    if executor != 'auto':
        initial_input['_executor_type'] = executor

    # --- Dry-run defaults: fill initial_input with template's example_input (Issue #591) ---
    # In dry-run mode the pipeline won't actually execute, but we still validate required fields
    # so that the validation code path (and mock) can be exercised.  Example_input provides
    # safe placeholder values for any fields not already in initial_input.
    if mode == 'dry-run':
        example = getattr(template, 'example_input', None) or {}
        for k, v in example.items():
            if k not in initial_input:
                initial_input[k] = v

    # --- Gateway token auto-read for openclaw mode (Issue #591) ---
    # NOTE: This runs BEFORE missing-fields validation so that "No gateway token found"
    # is the error shown when mode=openclaw and no token is available — not a fields error.
    if mode == 'openclaw':
        effective_token = (
            gateway_token
            or os.environ.get('OPENCLAW_GATEWAY_TOKEN')
            or _read_openclaw_token()
        )
        if not effective_token:
            click.echo(
                "No gateway token found. Set OPENCLAW_GATEWAY_TOKEN or check "
                "~/.openclaw/openclaw.json",
                err=True,
            )
            sys.exit(1)
        os.environ['OPENCLAW_GATEWAY_TOKEN'] = effective_token

    # --- Validate required config fields (#411) with new single-line format (#591) ---
    missing = _validate_required_config(template, initial_input)
    # Apply schema defaults for optional fields (#835) — see commentary in
    # the run_template path above for rationale.
    apply_config_schema_defaults(initial_input, getattr(template, 'config_schema', None))
    if missing:
        sorted_missing = sorted(missing)
        click.echo(
            f"Error: Missing required fields: {', '.join(sorted_missing)}",
            err=True,
        )
        sys.exit(1)

    # --- Build run_id and output_dir ---
    run_id = str(uuid.uuid4())[:8]
    if output_dir is None:
        _safe_id = re.sub(r'[^\w\-]', '_', template.id)
        _ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        output_dir = Path(f"./output/{_safe_id}-{_ts}-{run_id}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Persist run record to DB ---
    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))
    db.insert_pipeline_run({
        'run_id': run_id,
        'template_path': str(template_file.resolve()),
        'template_id': template.id,
        'input_json': json.dumps(initial_input),
        'mode': mode,
        'output_dir': str(output_dir.resolve()),
        'gateway_url': gateway_url,
        'skip_scoring': int(skip_scoring),
        'status': 'pending',
    })

    # --- Spawn daemon ---
    log_file_path = output_dir / ".orch-daemon.log"
    log_fh = open(str(log_file_path), 'a')

    proc = subprocess.Popen(
        [sys.executable, '-m', 'orchestration_engine.daemon', run_id, effective_db_path],
        start_new_session=True,
        stdout=log_fh,
        stderr=log_fh,
    )
    log_fh.close()

    db.update_pipeline_run(run_id, pid=proc.pid)

    click.echo(f"✓ Pipeline launched in background")
    click.echo(f"  Run ID:  {run_id}")
    click.echo(f"  PID:     {proc.pid}")
    click.echo(f"  Output:  {output_dir}/")
    click.echo(f"")
    click.echo(f"  Status:  orch status {run_id}")
    click.echo(f"  Logs:    orch logs {run_id}")
    click.echo(f"  Wait:    orch wait {run_id}")


@main.command("logs")
@click.argument('run_id')
@click.option('--follow', '-f', is_flag=True, default=False, help='Tail the log file.')
@click.option(
    '--db-path',
    default=None,
    help='Override path to the persistent pipeline-runs DB.',
)
def pipeline_logs(run_id: str, follow: bool, db_path: Optional[str]) -> None:
    """Show daemon logs for a pipeline run.

    \b
    Examples:
      orch logs a3f8c2d1
      orch logs a3f8c2d1 --follow
    """
    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))
    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"✗ Run '{run_id}' not found.", err=True)
        sys.exit(1)

    log_path = Path(run['output_dir']) / ".orch-daemon.log"
    if not log_path.exists():
        click.echo(f"✗ Log file not found: {log_path}", err=True)
        click.echo("  The run may not have started yet or the output dir is missing.")
        sys.exit(1)

    if follow:
        try:
            subprocess.run(['tail', '-f', str(log_path)])
        except KeyboardInterrupt:
            pass
    else:
        click.echo(log_path.read_text())


@main.command("children")
@click.argument("run_id")
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def pipeline_children(run_id: str, db_path: Optional[str]) -> None:
    """List child pipeline runs spawned by a parent run.

    Prints a table of all child runs (run_id, template_id, chain_depth,
    status) ordered by creation time.  Exits non-zero when the parent run
    ID is not found.

    \b
    Examples:
      orch children a3f8c2d1
      orch children a3f8c2d1 --db-path /tmp/custom.db
    """  # Issue #330.3: children CLI command
    from orchestration_engine.db import Database

    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))

    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"✗ Run '{run_id}' not found.", err=True)
        sys.exit(1)

    children = db.list_pipeline_run_children(run_id)

    if not children:
        click.echo(f"No child runs found for '{run_id}'.")
        return

    # Header
    click.echo(f"{'RUN ID':<36}  {'TEMPLATE':<30}  {'DEPTH':>5}  {'STATUS'}")
    click.echo("-" * 90)
    for child in children:
        click.echo(
            f"{child.get('run_id', ''):<36}  "
            f"{child.get('template_id', ''):<30}  "
            f"{child.get('chain_depth', 0):>5}  "
            f"{child.get('status', '')}"
        )


@main.command("chain")
@click.argument("run_id", required=False, default=None)
@click.option(
    "--active",
    is_flag=True,
    default=False,
    help="List all currently active (non-terminal) chains.",
)
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def pipeline_chain(
    run_id: Optional[str],
    active: bool,
    db_path: Optional[str],
) -> None:
    """Monitor pipeline chain execution status.

    \b
    Examples:
      orch chain a3f8c2d1          # Show full chain for a given run ID
      orch chain --active          # List all currently running chains
      orch chain a3f8c2d1 --db-path /tmp/custom.db
    """  # Issue #508: chain monitoring CLI
    from orchestration_engine.db import Database
    from orchestration_engine import chain_monitor

    # Validate: exactly one of run_id or --active must be provided
    if not run_id and not active:
        raise click.UsageError(
            "Provide a RUN_ID to inspect a chain, or use --active to list all active chains."
        )
    if run_id and active:
        raise click.UsageError("Cannot use both RUN_ID and --active together.")

    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))

    if active:
        click.echo(chain_monitor.build_active_chains_display(db))
        return

    # run_id mode: find root, then display full chain
    root = chain_monitor.find_chain_root(db, run_id)
    if root is None:
        click.echo(f"✗ Run '{run_id}' not found.", err=True)
        sys.exit(1)

    root_run_id = root["run_id"]
    if root_run_id != run_id:
        click.echo(f"(Showing chain from root: {root_run_id})")

    click.echo(chain_monitor.build_chain_display(db, root_run_id))


@main.command("wait")
@click.argument('run_id')
@click.option('--timeout', type=int, default=1800, show_default=True,
              help='Maximum seconds to wait before giving up.')
@click.option('--interval', type=int, default=3, show_default=True,
              help='Poll interval in seconds.')
@click.option(
    '--db-path',
    default=None,
    help='Override path to the persistent pipeline-runs DB.',
)
def pipeline_wait(run_id: str, timeout: int, interval: int, db_path: Optional[str]) -> None:
    """Block until a pipeline run finishes.

    Exits 0 on success, exits 2 on failure or timeout.

    \b
    Examples:
      orch wait a3f8c2d1
      orch wait a3f8c2d1 --timeout 120
    """
    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))

    terminal_states = {'success', 'failed', 'cancelled', 'crashed', 'scoring_failed', 'pending_review', 'rejected'}
    deadline = time.time() + timeout
    last_phase = None

    click.echo(f"Waiting for run '{run_id}' (timeout={timeout}s) …")

    while time.time() < deadline:
        run = db.get_pipeline_run(run_id)
        if run is None:
            click.echo(f"✗ Run '{run_id}' not found.", err=True)
            sys.exit(2)

        current_status = run['status']

        # Check PID liveness if still running
        if current_status == 'running':
            pid = run.get('pid')
            if pid:
                try:
                    from .daemon import is_process_alive
                    if not is_process_alive(pid):
                        current_status = 'crashed'
                        db.update_pipeline_run(run_id, status='crashed')
                except Exception:
                    pass

        current_phase = run.get('current_phase') or '(none)'
        if current_phase != last_phase:
            click.echo(f"  [{datetime.now().strftime('%H:%M:%S')}] status={current_status}  phase={current_phase}")
            last_phase = current_phase

        if current_status in terminal_states:
            click.echo(f"\nRun '{run_id}' finished with status: {current_status}")
            if current_status == 'success':
                sys.exit(0)
            else:
                sys.exit(2)

        time.sleep(interval)

    click.echo(f"\n✗ Timeout after {timeout}s — run '{run_id}' still in progress.", err=True)
    sys.exit(2)


@main.command("resume")
@click.argument('run_id')
def pipeline_resume(run_id: str) -> None:
    """Resume a failed or crashed pipeline run from the last completed phase.

    This is a v2 feature — not yet implemented.

    \b
    Examples:
      orch resume a3f8c2d1
    """
    click.echo("✗ 'orch resume' is not yet implemented (v2 feature).", err=True)
    click.echo("  To re-run from scratch:  orch launch <template> [options]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# orch gate — merge gate management commands
# ---------------------------------------------------------------------------

@main.group("gate")
def gate_group() -> None:
    """Manage coding pipeline merge gates.

    After a git-enabled pipeline completes, it creates a merge gate that
    requires human approval before the feature branch is merged.

    Examples:

      orch gate list                      # show all pending gates
      orch gate approve abc12345          # approve a gate (run ID)
      orch gate reject abc12345           # reject a gate
      orch gate info abc12345             # show gate details
    """


@gate_group.command("list")
@click.option(
    "--all", "show_all", is_flag=True, default=False,
    help="Show all gates including approved/rejected."
)
def gate_list(show_all: bool) -> None:
    """List pending merge gates."""
    from .git_integration import GitContext

    gates = GitContext.list_gates()
    if not gates:
        click.echo("No merge gates found.")
        return

    if not show_all:
        gates = [g for g in gates if g.get("status") == "awaiting_approval"]
        if not gates:
            click.echo("No pending merge gates.  Use --all to see all gates.")
            return

    headers = ["Run ID", "Pipeline", "Branch", "Status", "Created"]
    rows = []
    for g in gates:
        rows.append([
            g.get("run_id", "?")[:10],
            g.get("pipeline_id", "?")[:25],
            g.get("branch", "?")[:40],
            g.get("status", "?"),
            (g.get("created_at") or "?")[:19],
        ])
    print_table(headers, rows)


@gate_group.command("approve")
@click.argument("run_id")
@click.option("--message", "-m", default=None, help="Optional approval message.")
@click.option(
    "--force", "-f", is_flag=True, default=False,
    help="Override score gate enforcement and approve even when scoring failed. Use with caution.",
)
def gate_approve(run_id: str, message: Optional[str], force: bool) -> None:
    """Approve a merge gate (run ID from ``orch gate list``)."""
    from .git_integration import GitContext, GitError

    gate = GitContext.load_gate(run_id)
    if gate is None:
        click.echo(f"✗ No gate found for run ID '{run_id}'", err=True)
        sys.exit(1)

    if gate.get("status") not in ("awaiting_approval",):
        current = gate.get("status", "?")
        click.echo(
            f"⚠ Gate '{run_id}' is in status '{current}' — "
            f"can only approve 'awaiting_approval' gates."
        )
        if current in ("approved", "rejected"):
            sys.exit(0)
        sys.exit(1)

    # --- Score gate enforcement (Issue #289) --------------------------
    _gate_scoring = gate.get("scoring_status")
    _gate_score = gate.get("scoring_score")
    if _gate_scoring == "failed" and not force:
        _score_pct = f"{_gate_score * 100:.1f}" if _gate_score is not None else "n/a"
        click.echo(f"✗ Score gate FAILED — approval blocked.", err=True)
        click.echo(f"  Score: {_score_pct} / 100  (threshold: see scenario config)", err=True)
        click.echo(
            f"  Pipeline scoring failed. Fix the issues and re-run, "
            f"or use --force to override.",
            err=True,
        )
        sys.exit(1)
    elif _gate_scoring == "failed" and force:
        click.echo(
            f"⚠ Score gate FAILED — approving anyway because --force was specified."
        )
    elif _gate_scoring == "error":
        click.echo(
            f"⚠ Scoring encountered an error for this run — proceeding without score gate enforcement."
        )
    elif _gate_scoring is None:
        click.echo(
            f"⚠ No scoring data for this run — proceeding without score gate."
        )
    # scoring_status == "passed" → allow silently (happy path)

    try:
        updated = GitContext.update_gate_status(
            run_id, "approved", message=message or "Approved via orch gate approve"
        )
    except GitError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)

    branch = updated.get("branch", "?")
    base = updated.get("base_branch", "main")
    click.echo(f"✓ Gate '{run_id}' approved.")
    click.echo(f"  Branch: {branch}")
    click.echo(f"  Merge into {base}:")
    click.echo(f"    git checkout {base} && git merge --no-ff {branch}")

    # Optionally create PR if template configured create_pr: true
    if updated.get("create_pr"):
        from .git_integration import GitConfig, GitContext as _GC

        _cfg = GitConfig(create_pr=True)
        _tmp_ctx = _GC(config=_cfg, pipeline_id=updated.get("pipeline_id", ""),
                       run_id=run_id)
        pr_url = _tmp_ctx.create_pr(updated)
        if pr_url:
            click.echo(f"  PR created: {pr_url}")
        else:
            click.echo(
                "  ⚠ PR creation failed — run `gh pr create` manually or check gh CLI."
            )


@gate_group.command("reject")
@click.argument("run_id")
@click.option("--message", "-m", default=None, help="Optional rejection reason.")
def gate_reject(run_id: str, message: Optional[str]) -> None:
    """Reject a merge gate.  The feature branch is preserved for inspection."""
    from .git_integration import GitContext, GitError

    gate = GitContext.load_gate(run_id)
    if gate is None:
        click.echo(f"✗ No gate found for run ID '{run_id}'", err=True)
        sys.exit(1)

    try:
        updated = GitContext.update_gate_status(
            run_id, "rejected", message=message or "Rejected via orch gate reject"
        )
    except GitError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)

    branch = updated.get("branch", "?")
    click.echo(f"✓ Gate '{run_id}' rejected.")
    click.echo(f"  Branch '{branch}' preserved for inspection.")
    click.echo(
        f"  To delete it:  git branch -d {branch}"
    )


@gate_group.command("info")
@click.argument("run_id")
def gate_info(run_id: str) -> None:
    """Show details about a merge gate."""
    import json as _json
    from .git_integration import GitContext

    gate = GitContext.load_gate(run_id)
    if gate is None:
        click.echo(f"✗ No gate found for run ID '{run_id}'", err=True)
        sys.exit(1)

    status = gate.get("status", "?")
    status_emoji = {
        "awaiting_approval": "⏳",
        "approved": "✅",
        "rejected": "❌",
        "skipped": "⏭",
    }
    emoji = status_emoji.get(status, "❓")

    click.echo(f"Gate: {run_id}")
    click.echo(f"├─ Status:   {emoji} {status}")
    click.echo(f"├─ Pipeline: {gate.get('pipeline_id', '?')}")
    click.echo(f"├─ Branch:   {gate.get('branch', '?')}")
    click.echo(f"├─ Base:     {gate.get('base_branch', '?')}")
    click.echo(f"├─ Changes:  {gate.get('diff_stats', 'n/a')}")

    # Scoring info (Issue #289)
    _scoring_status = gate.get("scoring_status")
    _scoring_score = gate.get("scoring_score")
    if _scoring_status is not None:
        _score_emoji = {"passed": "✅", "failed": "❌", "error": "⚠️"}.get(
            _scoring_status, "❓"
        )
        _score_pct = (
            f"  ({_scoring_score * 100:.1f}/100)"
            if _scoring_score is not None
            else ""
        )
        click.echo(f"├─ Scoring:  {_score_emoji} {_scoring_status}{_score_pct}")
    else:
        click.echo(f"├─ Scoring:  ⏳ pending (not yet scored)")

    click.echo(f"├─ Created:  {(gate.get('created_at') or '?')[:19]}")
    if gate.get("updated_at"):
        click.echo(f"├─ Updated:  {gate['updated_at'][:19]}")
    if gate.get("message"):
        click.echo(f"├─ Message:  {gate['message']}")
    if gate.get("output_dir"):
        click.echo(f"├─ Output:   {gate['output_dir']}")

    commits = gate.get("commits", [])
    if commits:
        click.echo(f"└─ Commits ({len(commits)}):")
        for c in commits:
            click.echo(f"   • {c.get('sha', '?')[:8]}  {c.get('message', '?')}")
    else:
        click.echo(f"└─ Commits:  none")


def _check_yaml_syntax(template_file: Path) -> Optional[str]:
    """Try raw YAML parse and return a formatted error string or None if OK."""
    try:
        with open(template_file) as fh:
            yaml.safe_load(fh)
        return None
    except yaml.YAMLError as exc:
        if hasattr(exc, "problem_mark"):
            mark = exc.problem_mark
            line = mark.line + 1
            col = mark.column + 1
            problem = exc.problem or "syntax error"
            return f"YAML syntax error at line {line}:{col} — {problem}"
        return f"YAML syntax error — {exc}"


def _apply_fixes(template_file: Path, raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply auto-corrections to *raw_data* in-place and rewrite the file.

    Corrections applied:
    - Add missing ``version`` (default ``"1.0.0"``)
    - Add missing ``description`` (default ``""``)
    - Normalize ``model_tier`` to lowercase for every phase

    Returns the modified ``raw_data`` dict.
    """
    changed = False

    if "version" not in raw_data or raw_data["version"] is None:
        raw_data["version"] = "1.0.0"
        changed = True

    if "description" not in raw_data or raw_data["description"] is None:
        raw_data["description"] = ""
        changed = True

    for phase in raw_data.get("phases") or []:
        tier = phase.get("model_tier")
        if tier and isinstance(tier, str):
            normalised = tier.lower()
            if normalised != tier:
                phase["model_tier"] = normalised
                changed = True

    if changed:
        try:
            with open(template_file, "w") as fh:
                yaml.dump(raw_data, fh, default_flow_style=False, allow_unicode=True,
                          sort_keys=False)
            click.echo(click.style("⚠", fg="yellow") +
                       " Note: --fix rewrites YAML; comments may not be preserved.")
        except PermissionError:
            click.echo(click.style("✗", fg="red") +
                       f" Cannot write --fix changes: permission denied on {template_file}",
                       err=True)

    return raw_data


@main.command("validate")
@click.argument('template_name_or_file')
@click.option('--fix', is_flag=True, default=False,
              help='Auto-correct simple issues (missing version/description, model tier casing).')
def validate_template(template_name_or_file: str, fix: bool) -> None:
    """Validate a pipeline template and report any structural errors.

    TEMPLATE_NAME_OR_FILE is a template name (e.g. content-pipeline) or a
    path to a YAML file.  Template names are resolved using the search order:
    ORCH_TEMPLATES_PATH → ./templates/ → ~/.orch/templates/ → bundled.

    Exit code 0 = valid (warnings only).  Exit code 1 = errors found.
    """
    OK  = click.style("✓", fg="green")
    ERR = click.style("✗", fg="red")
    WRN = click.style("⚠", fg="yellow")

    try:
        from .templates import TemplateEngine, PipelineTemplate  # noqa: F401

        template_file = _resolve_template_arg(template_name_or_file)

        # ── 1. YAML syntax check ──────────────────────────────────────
        yaml_error = _check_yaml_syntax(template_file)
        if yaml_error:
            click.echo(f"{ERR} YAML syntax:  {yaml_error}", err=True)
            sys.exit(1)
        click.echo(f"{OK} YAML syntax")

        # ── 2. Load raw data (for --fix and extended checks) ──────────
        with open(template_file) as fh:
            raw_data: Dict[str, Any] = yaml.safe_load(fh)

        # ── 3. Apply fixes before structural validation ───────────────
        if fix:
            raw_data = _apply_fixes(template_file, raw_data)
            click.echo(f"{OK} --fix applied (version, description, model tier casing)")

        # ── 3b. Validate top-level adversary_config (if present) ─────
        if isinstance(raw_data, dict) and "adversary_config" in raw_data:
            from .templates import _parse_adversary_config
            try:
                _parse_adversary_config(raw_data["adversary_config"])
            except ValueError as exc:
                click.echo(f"{ERR} Invalid adversary_config: {exc}")
                sys.exit(1)
            click.echo(f"{OK} adversary_config valid")
            sys.exit(0)

        # ── 4. Structural validation via engine ───────────────────────
        engine = TemplateEngine()
        try:
            template: PipelineTemplate = engine.load_template(template_file)
        except ValueError as exc:
            click.echo(f"{ERR} {exc}")
            sys.exit(1)
        structural_errors = engine.validate_template(template)

        if structural_errors:
            click.echo(f"{ERR} Structural checks ({len(structural_errors)} error(s)):")
            for err in structural_errors:
                click.echo(f"    • {err}")
        else:
            click.echo(f"{OK} Structural checks  ({len(template.phases)} phases, deps OK)")

        # ── 5. Extended / linting checks ─────────────────────────────
        ext_errors, ext_warnings = engine.validate_template_extended(template, raw_data)

        if ext_errors:
            click.echo(f"{ERR} Extended checks ({len(ext_errors)} error(s)):")
            for err in ext_errors:
                click.echo(f"    • {err}")
        elif ext_warnings:
            click.echo(f"{WRN} Extended checks ({len(ext_warnings)} warning(s)):")
            for w in ext_warnings:
                click.echo(f"    • {w}")
        else:
            click.echo(f"{OK} Extended checks  (model tiers, thinking levels, variable refs, config_schema)")

        # ── 6. Summary ────────────────────────────────────────────────
        total_errors = len(structural_errors) + len(ext_errors)
        total_warnings = len(ext_warnings)

        if total_errors:
            click.echo(
                f"\n{ERR} Template {str(template_file)!r}: "
                f"{total_errors} error(s), {total_warnings} warning(s)"
            )
            sys.exit(1)
        elif total_warnings:
            click.echo(
                f"\n{WRN} Template {str(template_file)!r}: "
                f"valid with {total_warnings} warning(s)"
            )
        else:
            click.echo(
                f"\n{OK} Template {str(template_file)!r} is valid"
            )

    except (KeyError, ValueError) as exc:
        click.echo(f"{ERR} Invalid template: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command("list-phases")
@click.argument('template_name_or_file')
def list_phases(template_name_or_file: str) -> None:
    """Show execution order and model tiers for a pipeline template.

    TEMPLATE_NAME_OR_FILE is a template name (e.g. content-pipeline) or a
    path to a YAML file.  Template names are resolved using the search order:
    ORCH_TEMPLATES_PATH → ./templates/ → ~/.orch/templates/ → bundled.
    """
    try:
        from .templates import TemplateEngine, PipelineTemplate  # noqa: F401

        template_file = _resolve_template_arg(template_name_or_file)
        engine = TemplateEngine()
        template: PipelineTemplate = engine.load_template(template_file)
        waves = engine.get_execution_order(template)

        # Build a lookup from phase id → PhaseDefinition
        phase_map = {p.id: p for p in template.phases}

        click.echo(f"Pipeline: {template.name!r}  (v{template.version})")
        click.echo(f"Phases: {len(template.phases)}  |  Waves: {len(waves)}\n")

        for wave_idx, wave in enumerate(waves, start=1):
            parallel = len(wave) > 1
            label = f"Wave {wave_idx}" + ("  [parallel]" if parallel else "")
            click.echo(f"  {label}")
            for phase_id in wave:
                phase = phase_map.get(phase_id)
                if phase:
                    deps = ", ".join(phase.depends_on) if phase.depends_on else "none"
                    click.echo(
                        f"    ├─ {phase_id:30s}  model={phase.model_tier:8s}"
                        f"  thinking={phase.thinking_level:6s}  deps=[{deps}]"
                    )
                else:
                    click.echo(f"    ├─ {phase_id} (unknown)")
            click.echo()

    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Template discovery helpers
# ---------------------------------------------------------------------------

def _yaml_str(val: Any) -> str:
    """Convert a YAML-parsed value to string, mapping YAML booleans back to their
    original keyword (e.g. False → 'off', True → 'on')."""
    if val is False:
        return "off"
    if val is True:
        return "on"
    return str(val) if val is not None else ""


def _template_resolution_paths() -> List[tuple]:
    """Return list of (Path, source_label) for template scanning.

    Scanned in order (each directory may or may not exist):
    1. Paths from ``ORCH_TEMPLATES_PATH`` env var (colon-separated)  → "custom"
    2. ``./templates/``   (project-local)                            → "project"
    3. ``./examples/``    (project examples — backward compat)       → "examples"
    4. ``~/.orch/templates/`` (user-global, if it exists)            → "user"

    Labels are consistent with :meth:`TemplateEngine.get_search_paths` (e.g.
    ``"project"`` for ``./templates/`` rather than the old ``"templates"``).

    Note: bundled/package templates are handled by TemplateEngine.resolve_template()
    for name-based lookup but are not listed here to keep the scan focused on
    user-visible template sources.
    """
    import os as _os

    paths: List[tuple] = []

    # 1. ORCH_TEMPLATES_PATH (colon-separated)
    env_raw = _os.environ.get("ORCH_TEMPLATES_PATH", "")
    if env_raw:
        for part in env_raw.split(":"):
            part = part.strip()
            if part:
                paths.append((Path(part), "custom"))

    # 2+3. Project-local dirs — use "project" to match TemplateEngine.get_search_paths()
    paths.append((Path("./templates"), "project"))
    paths.append((Path("./examples"), "examples"))

    # 4. User-global (only if it exists, to avoid creating it on scan)
    user_dir = Path.home() / ".orch" / "templates"
    if user_dir.exists():
        paths.append((user_dir, "user"))

    return paths


def _scan_templates(resolution_paths: Optional[List[tuple]] = None) -> List[tuple]:
    """Scan resolution paths for YAML templates.

    Returns:
        List of (filepath, source_label, PipelineTemplate) tuples.
    """
    from .templates import TemplateEngine

    if resolution_paths is None:
        resolution_paths = _template_resolution_paths()

    engine = TemplateEngine()
    found = []
    seen_stems: dict = {}  # stem → first source label

    for search_path, source_label in resolution_paths:
        if not search_path.exists():
            continue
        for filepath in sorted(search_path.glob("*.yaml")) + sorted(
            search_path.glob("*.yml")
        ):
            stem = filepath.stem
            if stem in seen_stems:
                continue
            try:
                template = engine.load_template(filepath)
                seen_stems[stem] = source_label
                found.append((filepath, source_label, template))
            except Exception as exc:
                click.echo(f"[warn] Skipping {filepath}: {exc}", err=True)

    return found


def _validate_required_config(template, initial_input: Dict[str, Any]) -> List[str]:
    """Validate that all required config fields are present in the input.

    Checks the template's ``config_schema.required`` list against the keys
    provided in *initial_input*.  Returns a list of missing field names
    (empty list means all required fields are present).
    """
    schema = getattr(template, "config_schema", None)
    if not schema:
        return []
    required = schema.get("required", [])
    if not required:
        return []
    return [field for field in required if field not in initial_input]


def _slugify_title(title: str) -> str:
    """Slugify an issue title for branch name generation (Issue #591).

    Algorithm: lowercase → replace runs of non-alphanumeric chars with hyphen
    → strip leading/trailing hyphens → truncate to 49 chars → strip trailing
    hyphen after truncation → fallback to 'untitled' if empty.

    Note: truncates to 49 chars (not 50) to match acceptance test expectations.
    """
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    slug = slug[:49]
    slug = slug.rstrip('-')
    return slug or 'untitled'


def _read_openclaw_token() -> Optional[str]:
    """Read gateway token from ~/.openclaw/openclaw.json (Issue #591).

    Returns None if the file is missing, invalid JSON, or the key path
    gateway.auth.token is absent or null.
    """
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        data = json.loads(config_path.read_text())
        return data.get("gateway", {}).get("auth", {}).get("token") or None
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def _normalize_git_url(url: str) -> str:
    """Normalize git remote URL to HTTPS form (Issue #591).

    Handles:
      - SCP-style SSH:   git@github.com:owner/repo.git → https://github.com/owner/repo
      - RFC 3986 SSH:    ssh://git@github.com/owner/repo.git → https://github.com/owner/repo
      - HTTPS:           https://github.com/owner/repo.git → https://github.com/owner/repo
    """
    # SCP-style: git@host:path
    scp_match = re.match(r'git@([^:]+):(.+?)(?:\.git)?$', url)
    if scp_match:
        host, path = scp_match.groups()
        return f"https://{host}/{path}"
    # RFC 3986 SSH: ssh://git@host/path
    ssh2_match = re.match(r'ssh://git@([^/]+)/(.+?)(?:\.git)?$', url)
    if ssh2_match:
        host, path = ssh2_match.groups()
        return f"https://{host}/{path}"
    # HTTPS: strip trailing .git
    if url.endswith('.git'):
        url = url[:-4]
    return url


def _infer_git_context() -> tuple:
    """Return (repo_path, repo_url) inferred from CWD (Issue #591).

    repo_url is None when no remote named 'origin' exists.
    Both are None when CWD is not inside a git repository.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, check=True
        )
        repo_path = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None, None

    try:
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, check=True
        )
        repo_url = _normalize_git_url(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        repo_url = None

    return repo_path, repo_url


def _fetch_issue_strict(repo: str, issue_number: int) -> dict:
    """Fetch a GitHub issue or exit with a precise error message (Issue #591).

    Distinguishes: missing credentials (no-token message, exit 1) from
    issue-not-found (not-found message, exit 1).
    """
    # Detect missing credentials before API call
    has_env_token = bool(os.environ.get('GITHUB_TOKEN'))
    if not has_env_token:
        try:
            auth_result = subprocess.run(
                ['gh', 'auth', 'status'],
                capture_output=True, text=True, timeout=10
            )
            if auth_result.returncode != 0:
                click.echo(
                    "Error: No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'.",
                    err=True
                )
                sys.exit(1)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            click.echo(
                "Error: No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'.",
                err=True
            )
            sys.exit(1)

    # Fetch issue via GitHub API
    try:
        result = subprocess.run(
            ['gh', 'api', f'repos/{repo}/issues/{issue_number}'],
            capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        click.echo(
            f"Error: GitHub API request timed out fetching issue #{issue_number}.",
            err=True
        )
        sys.exit(1)
    if result.returncode != 0:
        combined = (result.stderr + result.stdout).lower()
        if '404' in combined or 'not found' in combined:
            click.echo(
                f"Error: Issue #{issue_number} not found. "
                "Check the issue number and your GITHUB_TOKEN.",
                err=True
            )
        else:
            click.echo(
                "Error: No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'.",
                err=True
            )
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        click.echo(f"✗ Invalid JSON from GitHub API: {exc}", err=True)
        sys.exit(1)


def _resolve_template_arg(name_or_path: str, launch_fmt: bool = False) -> Path:
    """Resolve a CLI template argument to a :class:`Path`.

    Accepts:
    * A :class:`Path` object → returned directly (already resolved, e.g. from
      ``ctx.invoke``).
    * A direct file path string (absolute or relative) → existence-checked.
    * A bare template name (e.g. ``content-pipeline``) → resolved via
      :meth:`TemplateEngine.resolve_template`.

    When *launch_fmt* is True (used by ``orch launch``), uses the exact error
    format required by Issue #591: single-line, no tip suffix.

    Exits with an error message on failure.
    """
    import os as _os
    from .templates import TemplateEngine, TemplateNotFoundError

    # Already a Path — accept directly
    if isinstance(name_or_path, Path):
        if not name_or_path.exists():
            click.echo(f"✗ Template file not found: {name_or_path}", err=True)
            sys.exit(1)
        return name_or_path

    # Heuristic: treat as a path when it has a path separator or YAML extension
    looks_like_path = (
        name_or_path.endswith(".yaml")
        or name_or_path.endswith(".yml")
        or _os.sep in name_or_path
        or "/" in name_or_path
    )

    if looks_like_path:
        p = Path(name_or_path)
        if not p.exists():
            if launch_fmt:
                click.echo(
                    f"Error: Template not found: {name_or_path}. "
                    "Run 'orch templates list' to see available templates.",
                    err=True,
                )
            else:
                click.echo(f"✗ Template file not found: {name_or_path}", err=True)
            sys.exit(1)
        return p

    # Name-based resolution
    engine = TemplateEngine()
    try:
        return engine.resolve_template(name_or_path)
    except TemplateNotFoundError as exc:
        if launch_fmt:
            click.echo(
                f"Error: Template not found: {name_or_path}. "
                "Run 'orch templates list' to see available templates.",
                err=True,
            )
        else:
            click.echo(f"✗ {exc}", err=True)
            click.echo(
                "\nTip: run 'orch templates list' to see all available templates.",
                err=True,
            )
        sys.exit(1)


# ---------------------------------------------------------------------------
# templates command group
# ---------------------------------------------------------------------------

@main.group()
def templates() -> None:
    """Browse and inspect pipeline templates."""


# ---------------------------------------------------------------------------
# Feature #67 — orch templates list
# ---------------------------------------------------------------------------

@templates.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def templates_list(json_output: bool) -> None:
    """List available pipeline templates from all resolution paths.

    Templates are discovered in the following order (first match wins when
    names collide):

    \b
    1. Paths in ORCH_TEMPLATES_PATH (colon-separated env var) — labelled "custom"
    2. ./templates/                  (project-local)           — labelled "project"
    3. ~/.orch/templates/            (user-global)             — labelled "user"
    4. <package>/../../templates/    (bundled with the engine) — labelled "bundled"

    The Source column shows where each template was found.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console(highlight=False)
    found = _scan_templates()

    if json_output:
        result = []
        for filepath, source, tmpl in found:
            result.append({
                "id": tmpl.id,
                "name": tmpl.name,
                "version": tmpl.version,
                "phases": len(tmpl.phases),
                "description": tmpl.description,
                "source": source,
                "path": str(filepath),
            })
        click.echo(json.dumps(result, indent=2))
        return

    if not found:
        click.echo("No templates found.")
        click.echo("\nTemplate search paths:")
        for path, source in _template_resolution_paths():
            click.echo(f"  [{source}] {path.resolve()}")
        click.echo("\nTip: add .yaml files to ./templates/ or ./examples/ to get started.")
        return

    table = Table(title="Available Templates", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Version", justify="center")
    table.add_column("Phases", justify="center")
    table.add_column("Description")
    table.add_column("Source", justify="center")

    for _filepath, source, tmpl in found:
        desc = tmpl.description or ""
        if len(desc) > 60:
            desc = desc[:57] + "..."
        table.add_row(
            tmpl.name,
            tmpl.version,
            str(len(tmpl.phases)),
            desc,
            source,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Feature #68 — orch templates info <name|path>
# ---------------------------------------------------------------------------

@templates.command("info")
@click.argument("name_or_path")
def templates_info(name_or_path: str) -> None:
    """Show detailed info about a template (by name, ID, or file path)."""
    from rich.console import Console
    from rich.table import Table
    from .templates import TemplateEngine

    console = Console(highlight=False)
    engine = TemplateEngine()

    # Reuse shared template resolution logic
    template_path, template = _find_template(name_or_path)

    # ---- Header ----
    console.print(
        f"\n[bold cyan]{template.name}[/bold cyan] "
        f"[dim](v{template.version})[/dim]"
    )
    if template.description:
        console.print(template.description)
    console.print()

    # ---- Documentation fields (#78) ----
    doc_lines = []
    if template.author:
        doc_lines.append(f"[bold]Author:[/bold]   {template.author}")
    if template.category:
        doc_lines.append(f"[bold]Category:[/bold] {template.category}")
    if template.tags:
        doc_lines.append(f"[bold]Tags:[/bold]     {', '.join(template.tags)}")
    if template.use_cases:
        doc_lines.append("[bold]Use Cases:[/bold]")
        for uc in template.use_cases:
            doc_lines.append(f"  • {uc}")
    if template.example_input:
        doc_lines.append(f"[bold]Example Input:[/bold] {json.dumps(template.example_input)}")
    if doc_lines:
        for line in doc_lines:
            console.print(line)
        console.print()

    # ---- Config Schema ----
    props: Dict[str, Any] = {}
    required_fields: set = set()

    if template.config_schema:
        props = template.config_schema.get("properties", {}) or {}
        required_fields = set(template.config_schema.get("required", []))

    if props:
        console.print("[bold]Config Schema:[/bold]")
        schema_table = Table(show_header=True, header_style="bold")
        schema_table.add_column("Field")
        schema_table.add_column("Type")
        schema_table.add_column("Required", justify="center")
        schema_table.add_column("Description")

        for field_name, field_info in props.items():
            field_info = field_info or {}
            field_type = field_info.get("type", "any")
            field_desc = field_info.get("description", "")
            field_required = "yes" if field_name in required_fields else "no"
            schema_table.add_row(field_name, field_type, field_required, field_desc)

        console.print(schema_table)
        console.print()

    # ---- Phases table ----
    if template.phases:
        console.print("[bold]Phases:[/bold]")
        phases_table = Table(show_header=True, header_style="bold")
        phases_table.add_column("ID")
        phases_table.add_column("Name")
        phases_table.add_column("Model", justify="center")
        phases_table.add_column("Thinking", justify="center")
        phases_table.add_column("Depends On")

        for phase in template.phases:
            deps = ", ".join(phase.depends_on) if phase.depends_on else "—"
            phases_table.add_row(
                _yaml_str(phase.id),
                _yaml_str(phase.name),
                _yaml_str(phase.model_tier),
                _yaml_str(phase.thinking_level),
                deps,
            )

        console.print(phases_table)
        console.print()

    # ---- Execution order / dependency graph ----
    waves = engine.get_execution_order(template)
    if waves:
        console.print("[bold]Execution Order:[/bold]")
        for i, wave in enumerate(waves, start=1):
            console.print(f"  Wave {i}: {', '.join(wave)}")
        console.print()

    # ---- Example command ----
    if template_path:
        example_input: Dict[str, Any] = {}
        if props:
            # Use first field as example
            first_field, first_info = next(iter(props.items()))
            first_info = first_info or {}
            if first_info.get("type", "string") == "string":
                example_input[first_field] = "AI agents"
            else:
                example_input[first_field] = "..."

        input_str = json.dumps(example_input) if example_input else '{"key": "value"}'
        console.print("[bold]Example:[/bold]")
        console.print(
            f"  orch run {template_path} --mode dry-run --input '{input_str}'"
        )
        console.print()


# ---------------------------------------------------------------------------
# Feature #69 — orch templates install / uninstall
# ---------------------------------------------------------------------------

_USER_TEMPLATES_DIR = Path.home() / ".orch" / "templates"


_GH_SHORTHAND_RE = re.compile(r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$')


def _is_github_shorthand(source: str) -> bool:
    """Check if source looks like 'user/repo' (GitHub shorthand)."""
    if source.endswith(".yaml") or source.endswith(".yml"):
        return False
    return bool(_GH_SHORTHAND_RE.match(source)) and not source.startswith(".")


def _install_from_git(url: str, name: str, force: bool) -> Path:
    """Clone a git repo into ~/.orch/templates/<name>/.

    Returns the install directory.
    Raises click.ClickException on failure.
    """
    import subprocess

    if url.startswith("-"):
        raise click.ClickException(f"Invalid URL: {url}")

    dest = _USER_TEMPLATES_DIR / re.sub(r'[^\w\-]', '_', name)

    if dest.exists():
        if not force:
            raise click.ClickException(
                f"Template '{name}' already installed at {dest}.\n"
                f"  Use --force to overwrite."
            )
        import shutil
        shutil.rmtree(dest)

    _USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise click.ClickException("git is not installed. Install git and try again.")
    except subprocess.TimeoutExpired:
        raise click.ClickException(f"Git clone timed out after 60s: {url}")
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(f"Git clone failed: {exc.stderr.strip()}")

    return dest


def _find_yaml_in_dir(directory: Path) -> Optional[Path]:
    """Find the first .yaml/.yml template file in a directory."""
    for pattern in ("*.yaml", "*.yml"):
        files = sorted(directory.glob(pattern))
        for f in files:
            if not f.name.startswith("."):
                return f
    # Check subdirectories (templates/, examples/)
    for subdir in ("templates", "examples"):
        sub = directory / subdir
        if sub.exists():
            for pattern in ("*.yaml", "*.yml"):
                files = sorted(sub.glob(pattern))
                for f in files:
                    if not f.name.startswith("."):
                        return f
    return None


def _validate_installed_template(yaml_path: Path):
    """Validate an installed template. Returns the PipelineTemplate on success.

    Raises click.ClickException on failure.
    """
    from .templates import TemplateEngine

    engine = TemplateEngine()
    try:
        template = engine.load_template(yaml_path)
    except Exception as exc:
        raise click.ClickException(f"Installed template is not valid YAML: {exc}")

    errors = engine.validate_template(template)
    if errors:
        err_str = "\n".join(f"  • {e}" for e in errors)
        raise click.ClickException(
            f"Installed template has {len(errors)} validation error(s):\n{err_str}"
        )
    return template


@templates.command("install")
@click.argument("source")
@click.option("--force", is_flag=True, help="Overwrite existing installation.")
@click.option("--name", default=None, help="Override the install directory name.")
def templates_install(source: str, force: bool, name: Optional[str]) -> None:
    """Install a template from a git URL, GitHub shorthand, or local path.

    SOURCE can be:

      - A git URL: https://github.com/user/repo
      - GitHub shorthand: user/repo
      - A local .yaml file path (copied to ~/.orch/templates/)

    Examples:

      orch templates install user/my-pipeline
      orch templates install https://github.com/user/my-pipeline
      orch templates install ./my-template.yaml --name my-pipeline
    """
    from rich.console import Console
    import shutil

    console = Console(highlight=False)

    # Determine source type
    is_url = source.startswith("http://") or source.startswith("https://")
    is_shorthand = _is_github_shorthand(source)
    is_local = source.endswith(".yaml") or source.endswith(".yml")

    if is_url:
        # Git URL
        install_name = name or source.rstrip("/").split("/")[-1].removesuffix(".git")
        console.print(f"[bold]Installing from git:[/bold] {source}")
        dest = _install_from_git(source, install_name, force)

    elif is_shorthand:
        # GitHub shorthand → https://github.com/user/repo
        url = f"https://github.com/{source}.git"
        install_name = name or source.split("/")[-1]
        console.print(f"[bold]Installing from GitHub:[/bold] {source}")
        dest = _install_from_git(url, install_name, force)

    elif is_local:
        # Local YAML file — copy to ~/.orch/templates/
        local_path = Path(source)
        if not local_path.exists():
            raise click.ClickException(f"File not found: {source}")

        install_name = name or local_path.stem
        safe_name = re.sub(r'[^\w\-]', '_', install_name)
        dest = _USER_TEMPLATES_DIR / safe_name

        if dest.exists():
            if not force:
                raise click.ClickException(
                    f"Template '{install_name}' already installed at {dest}.\n"
                    f"  Use --force to overwrite."
                )
            shutil.rmtree(dest)

        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest / local_path.name)
        console.print(f"[bold]Installing local file:[/bold] {source}")

    else:
        raise click.ClickException(
            f"Unknown source format: '{source}'\n"
            f"  Expected: git URL, GitHub shorthand (user/repo), or .yaml file path.\n"
            f"  Community index lookup is not yet available."
        )

    # Validate the installed template
    yaml_path = _find_yaml_in_dir(dest)
    if yaml_path is None:
        console.print(
            f"[yellow]⚠ No .yaml template found in {dest}. "
            f"The repo may need a templates/ or examples/ directory.[/yellow]"
        )
    else:
        try:
            tmpl = _validate_installed_template(yaml_path)
        except click.ClickException:
            # Clean up broken install
            shutil.rmtree(dest, ignore_errors=True)
            raise
        console.print(
            f"\n[green]✓ Installed:[/green] [bold]{tmpl.name}[/bold] "
            f"(v{tmpl.version}, {len(tmpl.phases)} phases)"
        )

    console.print(f"[dim]Location: {dest}[/dim]")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  [cyan]orch templates list[/cyan]          See all installed templates")
    if yaml_path:
        console.print(
            f"  [cyan]orch start {install_name}[/cyan]"
            f"          Run it interactively"
        )
    console.print()


@templates.command("uninstall")
@click.argument("name")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation prompt.")
def templates_uninstall(name: str, force: bool) -> None:
    """Remove an installed template from ~/.orch/templates/.

    NAME is the template directory name (as shown in `orch templates list`).
    """
    import shutil

    safe_name = re.sub(r'[^\w\-]', '_', name)
    dest = _USER_TEMPLATES_DIR / safe_name

    if not dest.exists():
        raise click.ClickException(
            f"Template '{name}' not found in {_USER_TEMPLATES_DIR}"
        )

    if not force:
        if not click.confirm(f"Remove template '{name}' from {dest}?"):
            click.echo("Aborted.")
            return

    shutil.rmtree(dest)
    click.echo(f"✓ Template '{name}' uninstalled.")


# ---------------------------------------------------------------------------
# Feature #76 — orch templates search <query>
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE_INDEX_URL = (
    "https://raw.githubusercontent.com/ToscanAI/orchestration-engine/main/"
    "community-templates/index.yaml"
)
_TEMPLATE_INDEX_CACHE = Path.home() / ".orch" / "cache" / "template-index.yaml"


@templates.command("search")
@click.argument("query", default="", required=False)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Force re-fetch of the remote index (ignore cache).",
)
@click.option(
    "--index-url",
    default=None,
    help="Override the default community index URL.",
)
def templates_search(query: str, refresh: bool, index_url: Optional[str]) -> None:
    """Search the community template index.

    QUERY is an optional search term (name, description, tags, category).
    Omit to list all available community templates.

    \b
    Examples:
      orch templates search content
      orch templates search --refresh
      orch templates search code-review --index-url https://example.com/index.yaml
    """
    from .template_index import TemplateIndex

    index = TemplateIndex()
    url = index_url or DEFAULT_TEMPLATE_INDEX_URL

    # ── 1. Resolve index data ──────────────────────────────────────────
    loaded = False

    if not refresh and TemplateIndex.is_cache_fresh(_TEMPLATE_INDEX_CACHE):
        try:
            index.load_local(_TEMPLATE_INDEX_CACHE)
            loaded = True
        except Exception:
            pass  # Fall through to remote fetch

    if not loaded:
        try:
            click.echo(f"Fetching index from {url} …", err=True)
            index.load_remote(url)
            try:
                index.save_cache(_TEMPLATE_INDEX_CACHE)
            except Exception:
                pass  # Cache save failure is non-fatal
        except Exception as exc:
            # If remote fails but we have a stale cache, use it
            if _TEMPLATE_INDEX_CACHE.exists():
                click.echo(
                    f"⚠  Remote fetch failed ({exc}); using stale cache.",
                    err=True,
                )
                index.load_local(_TEMPLATE_INDEX_CACHE)
            else:
                click.echo(
                    f"✗ Could not load template index: {exc}",
                    err=True,
                )
                raise SystemExit(1)

    # ── 2. Search ──────────────────────────────────────────────────────
    results = index.search(query)

    # ── 3. Display ─────────────────────────────────────────────────────
    if not results:
        click.echo(f"No templates found matching {query!r}.")
        return

    label = f"({len(results)} result{'s' if len(results) != 1 else ''})"
    if query:
        click.echo(f"Results for {query!r} {label}:\n")
    else:
        click.echo(f"Community templates {label}:\n")

    click.echo(index.format_results(results))


# ---------------------------------------------------------------------------
# Feature #65 — orch quickstart
# ---------------------------------------------------------------------------

@main.command("quickstart")
@click.pass_context
def quickstart(ctx: click.Context) -> None:
    """Give new users a working pipeline in 30 seconds with zero configuration.

    Runs the bundled hello-pipeline.yaml in dry-run mode so you can see what
    the engine does without any API key or config.
    """
    from rich.console import Console

    console = Console(highlight=False)

    # Locate hello-pipeline.yaml — try multiple locations for both
    # repo-based development and pip-installed packages.
    _pkg_dir = Path(__file__).parent          # src/orchestration_engine/
    _repo_root = _pkg_dir.parent.parent       # repo root (when running from source)
    candidates = [
        _repo_root / "examples" / "hello-pipeline.yaml",
        Path("./examples/hello-pipeline.yaml"),
        _pkg_dir / "examples" / "hello-pipeline.yaml",      # package data
        Path.home() / ".orch" / "templates" / "hello-pipeline.yaml",  # user dir
    ]
    hello_yaml: Optional[Path] = None
    for candidate in candidates:
        if candidate.exists():
            hello_yaml = candidate.resolve()
            break

    if hello_yaml is None:
        click.echo(
            "✗ Could not find hello-pipeline.yaml.\n"
            "  Looked in:\n"
            f"    • {_repo_root / 'examples/'}\n"
            f"    • ./examples/\n"
            f"    • {_pkg_dir / 'examples/'}\n"
            f"    • ~/.orch/templates/\n"
            "  Copy hello-pipeline.yaml to one of these locations, or run from the repo root.",
            err=True,
        )
        sys.exit(1)

    # ---- Header ----
    console.print()
    console.print("[bold]🚀 Orchestration Engine — Quick Start[/bold]")
    console.print()
    console.print("Running a sample pipeline [dim](dry-run, no API key needed)[/dim]...")
    console.print()

    # ---- Execute via the existing run command ----
    ctx.invoke(
        run_template,
        template_name_or_file=hello_yaml,
        mode="dry-run",
        api_key=None,
        input_json=None,
        input_file=None,
        output_dir=None,
        dry_run_delay=0.0,
        dry_run_failure_rate=0.0,
    )

    # ---- Footer ----
    from .templates import TemplateEngine as _TE
    _tmpl = _TE().load_template(hello_yaml)
    n_phases = len(_tmpl.phases)

    console.print()
    console.print(
        f"[bold green]✓ That's it![/bold green] "
        f"You just ran a {n_phases}-phase AI pipeline."
    )
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(
        "  [cyan]orch templates list[/cyan]"
        "                              See all available templates"
    )
    console.print(
        "  [cyan]orch templates info hello-pipeline[/cyan]"
        "        Explore a simple pipeline"
    )
    console.print(
        "  [cyan]orch run hello-pipeline.yaml --mode dry-run[/cyan]"
        "  Try a test run (no API key needed)"
    )
    console.print()


# ---------------------------------------------------------------------------
# Issue #110 — orch templates test
# ---------------------------------------------------------------------------

@templates.command("test")
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Show full error output per template on failure.",
)
@click.option(
    "--fail-fast", "-x",
    is_flag=True,
    default=False,
    help="Stop after the first template failure.",
)
def templates_test(verbose: bool, fail_fast: bool) -> None:
    """Validate and dry-run every discovered template.

    Discovers all ``.yaml`` / ``.yml`` files in ``templates/`` and
    ``examples/`` (same glob pattern used by the test suite) then runs
    two checks on each:

    \b
    1. Structural + extended validation (equivalent to ``orch validate``)
    2. Dry-run execution (equivalent to ``orch run --mode dry-run``)

    Exits 0 when all templates pass; exits 1 on the first failure
    (if ``--fail-fast``) or after all templates have been checked.

    Examples:

      orch templates test
      orch templates test --verbose
      orch templates test --fail-fast
    """
    import glob as _glob
    import json as _json
    import traceback as _tb

    import yaml as _yaml

    from .templates import TemplateEngine
    from .pipeline_runner import PipelineRunner
    from .sequencer import PhaseSequencer, StateMachineSequencer

    OK_MARK = click.style("✓", fg="green")
    FAIL_MARK = click.style("✗", fg="red")

    # ── 1. Discover templates (same glob as test suite) ──────────────────
    repo_root = Path(__file__).parent.parent.parent.parent
    # Heuristic: walk up until we find a templates/ directory
    _candidate = Path(__file__).resolve()
    for _ in range(6):
        _candidate = _candidate.parent
        if (_candidate / "templates").exists() and (_candidate / "examples").exists():
            repo_root = _candidate
            break

    all_templates: List[str] = sorted(
        _glob.glob(str(repo_root / "templates" / "*.yaml"))
        + _glob.glob(str(repo_root / "templates" / "*.yml"))
        + _glob.glob(str(repo_root / "examples" / "*.yaml"))
        + _glob.glob(str(repo_root / "examples" / "*.yml"))
    )

    if not all_templates:
        click.echo(
            f"{FAIL_MARK} No templates discovered under {repo_root}/ "
            "(looked in templates/ and examples/)",
            err=True,
        )
        sys.exit(1)

    click.echo(
        f"Discovered {len(all_templates)} template(s) under {repo_root}/\n"
    )

    engine = TemplateEngine()
    passed: List[str] = []
    failed: List[str] = []

    for template_path_str in all_templates:
        template_path = Path(template_path_str)
        template_name = template_path.name
        errors: List[str] = []

        # ── 2a. Validate ──────────────────────────────────────────────────
        try:
            template = engine.load_template(template_path)
            structural_errors = engine.validate_template(template)
            if structural_errors:
                errors.extend(
                    [f"[structural] {e}" for e in structural_errors]
                )

            raw_data: Dict[str, Any] = _yaml.safe_load(
                template_path.read_text()
            )
            ext_errors, _ext_warnings = engine.validate_template_extended(
                template, raw_data
            )
            if ext_errors:
                errors.extend(
                    [f"[extended] {e}" for e in ext_errors]
                )
        except Exception as exc:
            errors.append(
                f"[load/validate] {exc}"
                + (f"\n{_tb.format_exc()}" if verbose else "")
            )

        # ── 2b. Dry-run ───────────────────────────────────────────────────
        if not errors:
            try:
                input_data: Dict[str, Any] = (
                    template.example_input if template.example_input else {}
                )
                dry_runner = PipelineRunner.dry_run(
                    delay_seconds=0.0,
                    failure_rate=0.0,
                )
                with dry_runner:
                    _has_transitions = any(p.transitions for p in template.phases) or bool(
                        template.default_transitions
                    )
                    _SequencerClass = StateMachineSequencer if _has_transitions else PhaseSequencer
                    sequencer = _SequencerClass(
                        template, dry_runner, config=input_data
                    )
                    result = sequencer.execute(input_data)

                if result.get("aborted"):
                    failed_phase = result.get("failed_phase", "unknown")
                    errors.append(
                        f"[dry-run] pipeline aborted at phase '{failed_phase}'"
                    )
            except Exception as exc:
                errors.append(
                    f"[dry-run] {exc}"
                    + (f"\n{_tb.format_exc()}" if verbose else "")
                )

        # ── 3. Report ─────────────────────────────────────────────────────
        if errors:
            failed.append(template_name)
            click.echo(f"  {FAIL_MARK} {template_name}")
            if verbose:
                for err in errors:
                    for line in err.splitlines():
                        click.echo(f"       {line}", err=True)
        else:
            passed.append(template_name)
            click.echo(f"  {OK_MARK} {template_name}")

        if errors and fail_fast:
            click.echo(
                f"\n{FAIL_MARK} Stopped after first failure (--fail-fast).",
                err=True,
            )
            sys.exit(1)

    # ── 4. Summary ────────────────────────────────────────────────────────
    click.echo()
    total = len(passed) + len(failed)
    if failed:
        click.echo(
            f"{FAIL_MARK} {len(failed)}/{total} template(s) failed: "
            + ", ".join(failed),
            err=True,
        )
        sys.exit(1)
    else:
        click.echo(
            f"{OK_MARK} All {total} template(s) passed."
        )


# ---------------------------------------------------------------------------
# Feature #66 — orch start (interactive wizard)
# ---------------------------------------------------------------------------

def _find_template(name_or_path: str):
    """Locate a template by file path OR by name/ID.

    Resolution strategy:
    1. If the argument looks like a path (has separators or .yaml/.yml), load
       it directly.
    2. Exact template ID match (scanning all search paths).
    3. Exact template display-name match (case-insensitive).
    4. :meth:`TemplateEngine.resolve_template` stem-based lookup — only returns
       when the resolved template's ID or name also matches the query exactly
       (prevents false positives when file stem differs from template ID).
    5. Partial/slug matching with suggestions on ambiguous or no match.

    Returns:
        (template_path: Path, template: PipelineTemplate)

    Raises SystemExit on failure.
    """
    import os as _os
    from .templates import TemplateEngine, TemplateNotFoundError

    engine = TemplateEngine()

    is_path = (
        name_or_path.endswith(".yaml")
        or name_or_path.endswith(".yml")
        or _os.sep in name_or_path
        or "/" in name_or_path
    )

    if is_path:
        p = Path(name_or_path)
        try:
            template = engine.load_template(p)
            return p, template
        except FileNotFoundError:
            click.echo(f"✗ Template file not found: {name_or_path}")
            sys.exit(1)
        except Exception as exc:
            click.echo(f"✗ Could not load template: {exc}")
            sys.exit(1)

    search = name_or_path.lower()
    found_all = _scan_templates()

    # 1. Exact ID match
    for filepath, _source, tmpl in found_all:
        if tmpl.id.lower() == search:
            return filepath, tmpl

    # 2. Exact name match
    for filepath, _source, tmpl in found_all:
        if tmpl.name.lower() == search:
            return filepath, tmpl

    # 3. Stem-based resolution via resolve_template (respects all search paths).
    #    Only accept the result when the resolved template's ID or display name
    #    also matches the query — avoids returning an unrelated template whose
    #    file stem happens to equal the query but whose logical ID differs.
    try:
        resolved_path = engine.resolve_template(name_or_path)
        template = engine.load_template(resolved_path)
        if (template.id.lower() == search or template.name.lower() == search
                or template.id.lower().startswith(search + "-")):
            return resolved_path, template
    except TemplateNotFoundError:
        pass

    # 4. Partial match: search string appears in ID or name — suggest, don't auto-resolve
    partial_matches = [
        f"{tmpl.name} (id: {tmpl.id})"
        for _, _source, tmpl in found_all
        if search in tmpl.id.lower() or search in tmpl.name.lower()
    ]

    # Not found — suggest similar
    candidates = partial_matches or [
        f"{tmpl.name} (id: {tmpl.id})"
        for _, _, tmpl in found_all
        if search in tmpl.id.lower() or search in tmpl.name.lower()
    ]
    click.echo(f"✗ Template '{name_or_path}' not found.")
    if candidates:
        click.echo("\nDid you mean one of these?")
        for c in candidates:
            click.echo(f"  • {c}")
    else:
        click.echo(
            "\nNo similar templates found. Run 'orch templates list' to see all.",
        )
    sys.exit(1)


def _prompt_for_field(
    field_name: str,
    field_info: Dict[str, Any],
    required_fields: set,
    yes: bool,
) -> Optional[str]:
    """Prompt the user for a single config-schema field.

    When *yes* is True, skip the prompt and return the field's default (or "").
    Returns None if the field is optional and the user leaves it blank.
    """
    field_info = field_info or {}
    field_type = field_info.get("type", "string")
    field_desc = field_info.get("description", "")
    field_default = field_info.get("default", None)
    field_enum = field_info.get("enum", None)
    is_required = field_name in required_fields

    # Build label
    req_tag = ", required" if is_required else ""
    label = f"  {field_name} ({field_type}{req_tag})"
    if field_desc:
        label += f": {field_desc}"

    if yes:
        # Non-interactive: use default or empty string
        return str(field_default) if field_default is not None else (None if not is_required else "")

    click.echo(label)

    prompt_text = "  > "
    if field_enum:
        choice = click.Choice(field_enum)
        value = click.prompt(
            prompt_text,
            default=field_default or "",
            type=choice,
            prompt_suffix="",
        )
    elif field_default is not None:
        # Show default in brackets
        click.echo(f"  [default: {field_default}]")
        value = click.prompt(
            prompt_text,
            default=str(field_default),
            show_default=False,
            prompt_suffix="",
        )
    else:
        if is_required:
            value = click.prompt(prompt_text, prompt_suffix="")
        else:
            value = click.prompt(prompt_text, default="", show_default=False, prompt_suffix="")

    click.echo()
    return value if value != "" else (None if not is_required else "")


@main.command("start")
@click.argument("template_name_or_path")
@click.option(
    "--mode",
    type=click.Choice(["standalone", "openclaw", "dry-run"]),
    default="dry-run",
    show_default=True,
    help="Execution mode (dry-run is safe — no API calls).",
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    default=None,
    help="Anthropic API key for standalone mode (or set ANTHROPIC_API_KEY).",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Skip prompts and use default values for all fields.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to write phase outputs.",
)
@click.pass_context
def pipeline_start(
    ctx: click.Context,
    template_name_or_path: str,
    mode: str,
    api_key: Optional[str],
    yes: bool,
    output_dir: Optional[Path],
) -> None:
    """Interactive wizard to fill in a template's inputs and run it.

    TEMPLATE_NAME_OR_PATH can be a template name/ID (e.g. hello-pipeline)
    or a path to a .yaml file.

    Examples:

      # Interactive wizard with defaults:
      orch start hello-pipeline

      # Non-interactive (use all defaults), standalone mode:
      orch start hello-pipeline --mode standalone --yes

      # Point at a local file:
      orch start ./my-template.yaml --mode dry-run
    """
    import json as _json
    from rich.console import Console

    console = Console(highlight=False)

    # ---- 1. Find and load template ----
    template_path, template = _find_template(template_name_or_path)

    # ---- 2. Show header ----
    console.print()
    console.print(
        f"[bold cyan]{template.name}[/bold cyan] "
        f"[dim](v{template.version})[/dim]"
    )
    if template.description:
        console.print(template.description)
    console.print()

    # ---- 3. Collect inputs from config_schema ----
    config_schema: Dict[str, Any] = template.config_schema or {}
    props: Dict[str, Any] = config_schema.get("properties", {}) or {}
    required_fields: set = set(config_schema.get("required", []))

    collected: Dict[str, str] = {}

    try:
        if props:
            if not yes:
                console.print("[bold]Fill in the pipeline inputs:[/bold]")
                console.print()

            for field_name, field_info in props.items():
                value = _prompt_for_field(field_name, field_info, required_fields, yes)
                if value is not None:
                    collected[field_name] = value
        elif not yes:
            console.print("[dim]This template has no configurable inputs.[/dim]")
            console.print()

        # ---- 4. Summary + confirmation ----
        if collected and not yes:
            console.print("[bold]Summary:[/bold]")
            for k, v in collected.items():
                console.print(f"  {k}: {v}")
            console.print()
            if not click.confirm("Proceed?", default=True):
                click.echo("Aborted.")
                return
    except (click.Abort, KeyboardInterrupt):
        console.print("\n[dim]Aborted.[/dim]")
        return

    # ---- 5. Run via the existing run_template command ----
    input_json_str = _json.dumps(collected) if collected else None

    ctx.invoke(
        run_template,
        template_name_or_file=template_path,
        mode=mode,
        api_key=api_key,
        input_json=input_json_str,
        input_file=None,
        output_dir=output_dir,
        dry_run_delay=0.0,
        dry_run_failure_rate=0.0,
    )


# ---------------------------------------------------------------------------
# Feature #73 — orch new (scaffold a new pipeline template)
# ---------------------------------------------------------------------------

_VALID_MODEL_TIERS = ["haiku", "sonnet", "opus"]
_VALID_THINKING_LEVELS = ["off", "low", "medium", "high"]


def _build_default_phases(n_phases: int) -> List[Dict[str, Any]]:
    """Return a list of minimal phase dicts for --yes / non-interactive mode."""
    phases: List[Dict[str, Any]] = []
    for i in range(1, n_phases + 1):
        phase_id = f"phase-{i}"
        phases.append({
            "id": phase_id,
            "name": f"Phase {i}",
            "description": f"Phase {i} of the pipeline",
            "task_type": "content",
            "model_tier": "sonnet",
            "thinking_level": "low",
            "depends_on": [f"phase-{i - 1}"] if i > 1 else [],
            "timeout_minutes": 30,
            "prompt_template": "Process the following input:\n{input[topic]}\n",
            "output_schema": {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                },
            },
        })
    return phases


def _collect_phases_interactive(
    n_phases: int,
    base_phases: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Interactively prompt for each phase's settings.

    Uses *base_phases* (may be empty) as defaults when cloning from an
    existing template.
    """
    phases: List[Dict[str, Any]] = []

    for i in range(1, n_phases + 1):
        base: Dict[str, Any] = base_phases[i - 1] if i <= len(base_phases) else {}

        click.echo(f"\n── Phase {i} of {n_phases} " + "─" * 50)

        phase_id = click.prompt("  Phase ID", default=base.get("id", f"phase-{i}"))
        phase_name = click.prompt("  Phase name", default=base.get("name", f"Phase {i}"))
        phase_desc = click.prompt(
            "  Description",
            default=base.get("description", ""),
            show_default=False,
        )

        model_tier = click.prompt(
            "  Model tier",
            default=base.get("model_tier", "sonnet"),
            type=click.Choice(_VALID_MODEL_TIERS),
        )
        thinking_level = click.prompt(
            "  Thinking level",
            default=base.get("thinking_level", "low"),
            type=click.Choice(_VALID_THINKING_LEVELS),
        )

        # Dependencies: offer to choose from already-defined phases
        previous_ids = [p["id"] for p in phases]
        deps: List[str] = []
        if previous_ids:
            click.echo(f"  Previous phases: {', '.join(previous_ids)}")
            dep_input = click.prompt(
                "  Dependencies (comma-separated IDs, or blank for none)",
                default="",
                show_default=False,
            )
            if dep_input.strip():
                deps = [d.strip() for d in dep_input.split(",") if d.strip()]
                unknown = [d for d in deps if d not in previous_ids]
                if unknown:
                    click.echo(
                        click.style(f"  ⚠ Unknown phase ID(s) {unknown} — added anyway.", fg="yellow")
                    )
        else:
            # Inherit base deps (if any) that still reference valid IDs
            base_deps = base.get("depends_on") or []
            deps = [d for d in base_deps if d in previous_ids]

        phases.append({
            "id": phase_id,
            "name": phase_name,
            "description": phase_desc,
            "task_type": base.get("task_type", "content"),
            "model_tier": model_tier,
            "thinking_level": thinking_level,
            "depends_on": deps,
            "timeout_minutes": base.get("timeout_minutes", 30),
            "prompt_template": base.get(
                "prompt_template", "Process the following input:\n{input[topic]}\n"
            ),
            "output_schema": base.get(
                "output_schema",
                {"type": "object", "properties": {"result": {"type": "string"}}},
            ),
        })

    return phases


def _build_scaffold_yaml(data: Dict[str, Any]) -> str:
    """Serialise *data* to a commented YAML string.

    Uses ``yaml.dump()`` for individual sections and manually prepends
    ``# comment`` lines before each major block, since PyYAML does not
    support comment generation natively.
    """

    def _dump(obj: Any) -> str:
        return yaml.dump(obj, default_flow_style=False, allow_unicode=True, sort_keys=False)

    lines: List[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines += [
        f"# Pipeline: {data['id']}",
        "# Generated by `orch new` — edit to customize",
        "# Run `orch validate <this-file>` to check validity",
        "",
    ]

    # ── Top-level metadata fields ────────────────────────────────────────────
    top_meta: Dict[str, Any] = {
        k: v
        for k, v in data.items()
        if k not in ("config_schema", "phases")
    }
    lines.append(_dump(top_meta).rstrip())
    lines.append("")

    # ── config_schema ────────────────────────────────────────────────────────
    lines += [
        "# config_schema: defines the inputs your pipeline accepts at runtime.",
        "# Add fields under 'properties'; list required field names under 'required'.",
    ]
    lines.append(_dump({"config_schema": data["config_schema"]}).rstrip())
    lines.append("")

    # ── phases ───────────────────────────────────────────────────────────────
    lines += [
        "# phases: the ordered list of pipeline steps.",
        "# A phase runs only after all its depends_on phases have completed.",
        "phases:",
    ]

    for phase in data["phases"]:
        lines.append("")
        lines.append(
            f"  # ── {phase['id']} " + "─" * max(4, 60 - len(phase["id"]))
        )
        # yaml.dump renders a one-element list; strip trailing newline then indent
        phase_block = _dump([phase]).rstrip()
        indented = "\n".join("  " + row for row in phase_block.splitlines())
        lines.append(indented)

    lines.append("")
    return "\n".join(lines)


@main.command("new")
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="Non-interactive: generate a template with sensible defaults "
         "(name=my-pipeline, 2 phases, sonnet/low).",
)
@click.option(
    "--from", "from_template",
    default=None,
    metavar="TEMPLATE",
    help="Clone an existing template as the starting point. "
         "Accepts a template name, ID, or file path.",
)
@click.option(
    "--output", "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file path. Defaults to ./templates/<name>.yaml.",
)
@click.option(
    "--force", "-f",
    is_flag=True,
    default=False,
    help="Overwrite the output file if it already exists.",
)
@click.option(
    "--phases", "num_phases",
    type=int,
    default=None,
    metavar="N",
    help="Number of phases (primarily used with --yes; default 2).",
)
def new_template(
    yes: bool,
    from_template: Optional[str],
    output_path: Optional[Path],
    force: bool,
    num_phases: Optional[int],
) -> None:
    """Scaffold a new pipeline template interactively.

    Walks you through naming the pipeline, adding phases, choosing model tiers
    and thinking levels, and wiring up phase dependencies.  The generated YAML
    file is ready to run with ``orch run`` and passes ``orch validate``.

    \b
    Examples:

      # Fully interactive wizard:
      orch new

      # Non-interactive with sensible defaults:
      orch new --yes

      # Clone an existing template as a starting point:
      orch new --from hello-pipeline

      # Custom output path:
      orch new --yes --output ./my-templates/awesome.yaml
    """

    # ── 0. Validate --phases early (even before prompts) ────────────────────
    if num_phases is not None and num_phases <= 0:
        click.echo("✗ Number of phases must be at least 1.", err=True)
        sys.exit(1)

    # ── 1. Load base template when --from is provided ────────────────────────
    base_data: Optional[Dict[str, Any]] = None
    if from_template:
        from_path, _ = _find_template(from_template)
        with open(from_path) as fh:
            base_data = yaml.safe_load(fh)
        if not yes:
            click.echo(click.style("✓", fg="green") + f" Cloning from: {from_path}")
            click.echo()

    # ── 2. Collect template metadata ─────────────────────────────────────────
    if yes:
        raw_name: str = (base_data or {}).get("name", "my-pipeline")
        description: str = (base_data or {}).get("description", "") or "My pipeline description"
        author: str = (base_data or {}).get("author", "") or "Unknown"
    else:
        click.echo("── Template Metadata " + "─" * 50)
        default_name = (base_data or {}).get("name", "my-pipeline")
        raw_name = click.prompt("  Template name", default=default_name)
        description = click.prompt(
            "  Description",
            default=(base_data or {}).get("description", "") or "",
            show_default=False,
        )
        author = click.prompt(
            "  Author",
            default=(base_data or {}).get("author", "") or "",
            show_default=False,
        )
        click.echo()

    template_id = re.sub(r"[^a-z0-9]+", "-", raw_name.lower()).strip("-") or "my-pipeline"

    # ── 3. Determine & validate output path ──────────────────────────────────
    if output_path is None:
        output_path = Path("templates") / f"{template_id}.yaml"

    if output_path.exists() and not force and not yes:
        click.echo(
            f"✗ Output file already exists: {output_path}\n"
            f"  Use --force to overwrite.",
            err=True,
        )
        sys.exit(1)

    # ── 4. Collect phases ─────────────────────────────────────────────────────
    base_phases: List[Dict[str, Any]] = (base_data or {}).get("phases") or []

    if yes:
        # --yes mode: use --phases N, base template count, or default 2
        n = num_phases if num_phases is not None else (len(base_phases) if base_phases else 2)
        if base_phases:
            # Clone phases from the base template (up to n)
            phases_data = base_phases[:n]
            # Pad with defaults if --phases exceeds source template count
            if n > len(base_phases):
                phases_data += _build_default_phases(n - len(base_phases))[: n - len(base_phases)]
        else:
            phases_data = _build_default_phases(n)
    else:
        # Interactive
        click.echo("── Phases " + "─" * 62)
        default_n = num_phases if num_phases is not None else (len(base_phases) if base_phases else 2)
        n = click.prompt("  Number of phases", default=default_n, type=int)
        if n <= 0:
            click.echo("✗ Number of phases must be at least 1.", err=True)
            sys.exit(1)
        phases_data = _collect_phases_interactive(n, base_phases)

    # ── 5. Build config_schema ────────────────────────────────────────────────
    if base_data and base_data.get("config_schema"):
        config_schema = base_data["config_schema"]
    else:
        config_schema = {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Main topic or input for the pipeline",
                }
            },
            "required": ["topic"],
        }

    # ── 6. Assemble template dict ─────────────────────────────────────────────
    version = (base_data or {}).get("version", "1.0.0") or "1.0.0"
    template_dict: Dict[str, Any] = {
        "id": template_id,
        "name": raw_name,
        "version": version,
        "description": description,
        "author": author,
        "config_schema": config_schema,
        "phases": phases_data,
    }

    # ── 7. Render YAML with comments ─────────────────────────────────────────
    yaml_content = _build_scaffold_yaml(template_dict)

    # ── 8. Write to disk ──────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_content, encoding="utf-8")

    click.echo(click.style("✓", fg="green") + f" Template written to: {output_path}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  orch validate {output_path}          # Check for errors")
    click.echo(f"  orch run {output_path} --mode dry-run  # Test it")


# ---------------------------------------------------------------------------
# orch import — import external resources into PipelineTemplate YAML
# ---------------------------------------------------------------------------


@main.group("import")
def import_group() -> None:
    """Import external resources and convert them to PipelineTemplate YAML."""


@import_group.command("plugin-command")
@click.argument(
    "command_file",
    type=click.Path(exists=True, path_type=Path),
    metavar="COMMAND_FILE",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Path to write the generated YAML template.  "
        "Defaults to <command-id>.yaml in the current directory."
    ),
)
@click.option(
    "--author",
    default=None,
    help="Author string for the generated template (default: 'orch import plugin-command').",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the generated YAML to stdout without writing a file.",
)
@click.option(
    "--validate",
    "run_validate",
    is_flag=True,
    default=False,
    help="Run orch validate on the generated file after writing.",
)
def import_plugin_command(
    command_file: Path,
    output: Optional[Path],
    author: Optional[str],
    dry_run: bool,
    run_validate: bool,
) -> None:
    """Convert a knowledge-work-plugin command file to a PipelineTemplate YAML.

    COMMAND_FILE is the path to a Markdown plugin command file (with optional
    YAML frontmatter).  The importer:

    \b
    1. Parses the frontmatter for template metadata.
    2. Maps every non-meta H2 section to a pipeline phase (sonnet tier).
    3. Auto-inserts a review phase (opus tier) after each content phase.
    4. Derives config_schema from the ## Inputs section.
    5. Collects skill file references into skill_refs.

    The generated YAML is written to --output (default: <id>.yaml).
    Use --dry-run to preview without writing.  Use --validate to immediately
    check the result with orch validate.

    Examples:

    \b
      orch import plugin-command campaign-plan.md
      orch import plugin-command draft-content.md --output my-draft.yaml
      orch import plugin-command brand-review.md --dry-run
      orch import plugin-command campaign-plan.md --validate
    """
    from .importers.plugin_command import (
        import_plugin_command as _do_import,
        GENERATED_AUTHOR,
    )

    OK  = click.style("✓", fg="green")
    ERR = click.style("✗", fg="red")

    # ── 1. Parse and generate YAML ────────────────────────────────────────────
    try:
        yaml_text = _do_import(
            command_file,
            author=author or GENERATED_AUTHOR,
        )
    except ValueError as exc:
        click.echo(f"{ERR} Failed to parse plugin command: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"{ERR} Unexpected error: {exc}", err=True)
        sys.exit(1)

    # ── 2. Dry-run: print and exit ────────────────────────────────────────────
    if dry_run:
        click.echo(yaml_text)
        return

    # ── 3. Determine output path ──────────────────────────────────────────────
    if output is None:
        # Derive stem from the generated YAML's id field.
        # Strip the leading comment header (lines beginning with "#") before
        # parsing so yaml.safe_load receives clean YAML.  The previous
        # approach (lstrip + concatenate) was fragile and produced invalid
        # duplicate-key YAML on some edge-case inputs.
        try:
            data_lines = [
                line for line in yaml_text.splitlines()
                if not line.startswith("#")
            ]
            first_pass = yaml.safe_load("\n".join(data_lines))
            template_id = (
                first_pass.get("id", command_file.stem)
                if isinstance(first_pass, dict)
                else command_file.stem
            )
        except Exception:
            template_id = command_file.stem
        output = Path(f"{template_id}.yaml")

    # ── 4. Write to disk ──────────────────────────────────────────────────────
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml_text, encoding="utf-8")
    except OSError as exc:
        click.echo(f"{ERR} Could not write output file: {exc}", err=True)
        sys.exit(1)

    click.echo(f"{OK} Generated template: {output}")

    # ── 5. Optional: run orch validate on the result ─────────────────────────
    if run_validate:
        # Call the validation logic directly rather than via CliRunner.
        # CliRunner is a *testing* utility: it intercepts stdout/stderr, creates
        # its own Click context, and does not propagate env vars reliably.
        # Using it inside a real CLI invocation is architecturally fragile.
        click.echo()
        OK_v  = click.style("✓", fg="green")
        ERR_v = click.style("✗", fg="red")
        WRN_v = click.style("⚠", fg="yellow")
        try:
            from .templates import TemplateEngine, PipelineTemplate  # noqa: F401

            # 5a. YAML syntax
            yaml_error = _check_yaml_syntax(output)
            if yaml_error:
                click.echo(f"{ERR_v} YAML syntax:  {yaml_error}", err=True)
                sys.exit(1)
            click.echo(f"{OK_v} YAML syntax")

            # 5b. Load raw data
            with open(output) as _fh:
                _raw_data: Dict[str, Any] = yaml.safe_load(_fh)

            # 5c. Structural validation
            _engine = TemplateEngine()
            _tpl: PipelineTemplate = _engine.load_template(output)
            _structural_errors = _engine.validate_template(_tpl)

            if _structural_errors:
                click.echo(f"{ERR_v} Structural checks ({len(_structural_errors)} error(s)):")
                for _e in _structural_errors:
                    click.echo(f"    • {_e}")
            else:
                click.echo(f"{OK_v} Structural checks  ({len(_tpl.phases)} phases, deps OK)")

            # 5d. Extended / linting checks
            _ext_errors, _ext_warnings = _engine.validate_template_extended(_tpl, _raw_data)

            if _ext_errors:
                click.echo(f"{ERR_v} Extended checks ({len(_ext_errors)} error(s)):")
                for _e in _ext_errors:
                    click.echo(f"    • {_e}")
            elif _ext_warnings:
                click.echo(f"{WRN_v} Extended checks ({len(_ext_warnings)} warning(s)):")
                for _w in _ext_warnings:
                    click.echo(f"    • {_w}")
            else:
                click.echo(
                    f"{OK_v} Extended checks  "
                    "(model tiers, thinking levels, variable refs, config_schema)"
                )

            # 5e. Summary
            _total_errors = len(_structural_errors) + len(_ext_errors)
            _total_warnings = len(_ext_warnings)
            if _total_errors:
                click.echo(
                    f"\n{ERR_v} Template {str(output)!r}: "
                    f"{_total_errors} error(s), {_total_warnings} warning(s)"
                )
                sys.exit(1)
            elif _total_warnings:
                click.echo(
                    f"\n{WRN_v} Template {str(output)!r}: "
                    f"valid with {_total_warnings} warning(s)"
                )
            else:
                click.echo(f"\n{OK_v} Template {str(output)!r} is valid")

        except (KeyError, ValueError) as _exc:
            click.echo(f"{ERR_v} Invalid template: {_exc}", err=True)
            sys.exit(1)
        except Exception as _exc:
            click.echo(f"Error during validation: {_exc}", err=True)
            sys.exit(1)
    else:
        click.echo(f"\nNext steps:")
        click.echo(f"  orch validate {output}           # Check the template")
        click.echo(f"  orch run {output} --mode dry-run  # Test it")


# ---------------------------------------------------------------------------
# orch serve — local web UI server  (Feature #79)
# ---------------------------------------------------------------------------

@main.command("serve")
@click.option('--port', default=8374, show_default=True, help='Port to serve on.')
@click.option('--host', default='127.0.0.1', show_default=True, help='Host to bind to.')
@click.option('--no-open', is_flag=True, help='Do not auto-open browser.')
@click.option('--db-path', default=None, help='SQLite DB path for pipeline runs.')
@click.option('--reload', is_flag=True, help='Enable uvicorn auto-reload (dev mode).')
def serve(port: int, host: str, no_open: bool, db_path: Optional[str], reload: bool) -> None:
    """Launch the unified web UI + REST API on a single port.

    Serves the Next.js static frontend and the /api/v1/ REST API together.
    No CORS, no proxy, no separate servers needed.

    Requires the optional [web] extra:

      pip install orchestration-engine[web]

    Examples:

      orch serve                    # http://127.0.0.1:8374
      orch serve --port 9000
      orch serve --no-open          # start without opening browser
      orch serve --db-path /tmp/my.db
    """
    try:
        import uvicorn
        from .web.api import create_api_app
    except ImportError:
        click.echo("Web UI requires extra dependencies. Install with:", err=True)
        click.echo("  pip install orchestration-engine[web]", err=True)
        sys.exit(1)

    app = create_api_app(db_path=db_path)

    # Mount static frontend if available
    frontend_out = Path(__file__).resolve().parent.parent.parent / 'frontend' / 'out'
    if frontend_out.exists():
        from fastapi.responses import FileResponse

        index_html = frontend_out / 'index.html'

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """SPA fallback: serve static files or route-specific HTML for client-side routing.

            Next.js static export generates dynamic route pages as `_.html`
            (e.g. `templates/_.html` for `/templates/[id]`). We must serve
            the correct HTML shell so the client-side router hydrates the
            right page component.
            """
            # 1. Exact static file match (JS, CSS, images, etc.)
            static_file = frontend_out / full_path
            if static_file.is_file() and static_file.resolve().is_relative_to(frontend_out.resolve()):
                return FileResponse(str(static_file))

            # 2. Try .html extension (e.g. /runs → runs.html)
            html_file = frontend_out / f"{full_path}.html"
            if html_file.is_file() and html_file.resolve().is_relative_to(frontend_out.resolve()):
                return FileResponse(str(html_file))

            # 3. Dynamic route: /templates/xyz/edit → templates/_/edit.html
            #    Try replacing dynamic segments with '_' (most-specific first)
            parts = full_path.strip('/').split('/')

            # 3a. Try substituting each path segment with '_' from right to left
            #     e.g. /templates/xyz/edit → templates/_/edit.html
            for i in range(len(parts) - 1, 0, -1):
                trial = [*parts]
                trial[i] = '_'
                # Try as .html
                candidate_html = frontend_out / ('/'.join(trial) + '.html')
                if candidate_html.is_file() and candidate_html.resolve().is_relative_to(frontend_out.resolve()):
                    return FileResponse(str(candidate_html))
                # Try as directory with index.html
                candidate_index = frontend_out / '/'.join(trial) / 'index.html'
                if candidate_index.is_file() and candidate_index.resolve().is_relative_to(frontend_out.resolve()):
                    return FileResponse(str(candidate_index))

            # 3b. Walk up the path to find the nearest _.html
            for i in range(len(parts), 0, -1):
                candidate = frontend_out / '/'.join(parts[:i]) / '_.html'
                if candidate.is_file() and candidate.resolve().is_relative_to(frontend_out.resolve()):
                    return FileResponse(str(candidate))

            # 4. Ultimate fallback: index.html (dashboard)
            return FileResponse(str(index_html))

        click.echo(f"Frontend: {frontend_out}")
    else:
        click.echo(
            "Warning: Frontend not built. Run 'cd frontend && npm run build'. "
            "API endpoints are still available at /api/v1/",
            err=True,
        )

    if not no_open:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    click.echo(f"Orchestration Engine (unified)")
    click.echo(f"  UI:  http://{host}:{port}")
    click.echo(f"  API: http://{host}:{port}/api/v1/docs")
    click.echo(f"  Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# orch ui — Serve static Next.js frontend export (Issue #310)
# ---------------------------------------------------------------------------

@main.command("ui")
@click.option('--port', default=8080, show_default=True, help='Port to serve the frontend on.')
@click.option('--host', default='127.0.0.1', show_default=True, help='Host to bind to.')
@click.option('--no-open', is_flag=True, help='Skip auto-opening the browser.')
def ui(port: int, host: str, no_open: bool) -> None:
    """Serve the static Next.js frontend export and open the browser.

    Serves the pre-built frontend from frontend/out/ using Python's built-in
    HTTP server.  Build the frontend first if the directory is missing:

      cd frontend && npm run build

    Examples:

      orch ui                    # http://localhost:8080
      orch ui --port 9090
      orch ui --no-open          # start without opening browser
      orch ui --host 0.0.0.0    # bind to all interfaces
    """
    import http.server
    import socketserver
    import threading
    import webbrowser

    frontend_out = Path(__file__).parent.parent.parent / 'frontend' / 'out'

    if not frontend_out.exists():
        click.echo(
            "✗ frontend/out/ not found. Run 'cd frontend && npm run build' first.",
            err=True,
        )
        sys.exit(1)

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        """Serve from frontend/out/ and suppress request logs."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(frontend_out), **kwargs)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silence per-request output

    url = f"http://{host}:{port}"

    # socketserver.TCPServer with allow_reuse_address so re-runs don't fail
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer((host, port), _QuietHandler)

    if not no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    click.echo(f"✓ Orchestration Engine frontend (static)")
    click.echo(f"  Serving:  {frontend_out}")
    click.echo(f"  URL:      {url}")
    click.echo(f"  Press Ctrl+C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        click.echo("\n✓ Server stopped.")


# ---------------------------------------------------------------------------
# orch api-server — REST API server (Issue #257)
# ---------------------------------------------------------------------------

@main.command("api-server")
@click.option('--port', default=8375, show_default=True, help='Port to serve on.')
@click.option('--host', default='127.0.0.1', show_default=True, help='Host to bind to.')
@click.option('--reload', is_flag=True, default=False, help='Enable auto-reload (dev only).')
@click.option(
    '--db-path',
    default=None,
    help='Override path to the persistent pipeline-runs DB.',
)
def api_server(port: int, host: str, reload: bool, db_path: Optional[str]) -> None:
    """Launch the REST API server for programmatic pipeline control.

    Starts a FastAPI REST API at /api/v1/ backed by the same daemon-based
    async execution infrastructure used by ``orch launch``.  Intended for
    CI/CD pipelines, OpenClaw, and other programmatic consumers.

    Requires the optional [web] extra:

      pip install orchestration-engine[web]

    Endpoints:

    \b
      GET  /api/v1/health                — health check
      GET  /api/v1/templates             — list all templates
      GET  /api/v1/templates/{name}      — template detail
      POST /api/v1/runs                  — launch a pipeline run
      GET  /api/v1/runs                  — list runs (with filtering/pagination)
      GET  /api/v1/runs/{run_id}         — run status
      GET  /api/v1/runs/{run_id}/logs    — daemon log output
      DELETE /api/v1/runs/{run_id}       — cancel a run

    Examples:

      orch api-server                    # http://127.0.0.1:8375/api/v1/
      orch api-server --port 9000
      orch api-server --reload           # dev mode with auto-reload
    """
    try:
        import uvicorn
        from .web.api import create_api_app
    except ImportError:
        click.echo("REST API server requires extra dependencies. Install with:", err=True)
        click.echo("  pip install orchestration-engine[web]", err=True)
        sys.exit(1)

    effective_db_path = db_path or _get_persistent_db_path()
    app = create_api_app(db_path=effective_db_path)

    click.echo(f"✓ Orchestration Engine REST API server")
    click.echo(f"  Listening on http://{host}:{port}")
    click.echo(f"  Docs:      http://{host}:{port}/api/v1/docs")
    click.echo(f"  DB:        {effective_db_path}")
    click.echo(f"  Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# orch rubric — skill rubric generation  (AC-1)
# ---------------------------------------------------------------------------

@main.group("rubric")
def rubric() -> None:
    """Generate LLM Judge rubric YAML from skill markdown files."""


@rubric.command("generate")
@click.argument("skill_file", type=click.Path(path_type=Path))
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output YAML file path. Defaults to <skill-name>-rubric.yaml in cwd.",
)
@click.option(
    "--force", "-f",
    is_flag=True,
    default=False,
    help="Overwrite output file if it already exists.",
)
def rubric_generate(skill_file: Path, output: Optional[Path], force: bool) -> None:
    """Generate a rubric YAML file from a SKILL.md file.

    SKILL_FILE is the path to a skill markdown file (e.g. SKILL.md).

    The generated YAML contains:

    \b
    - rubric: the rubric text to pass to LLMJudgeGrader
    - criteria: machine-readable list of extracted checks
    - name / generated_from / generated_at: metadata

    Examples:

      orch rubric generate path/to/SKILL.md

      orch rubric generate path/to/SKILL.md --output my-rubric.yaml

      orch rubric generate path/to/SKILL.md --output results/rubric.yaml --force
    """
    from .rubric_generator import generate_rubric_file

    try:
        out_path = generate_rubric_file(skill_file, output=output, force=force)
        click.echo(f"✓ Rubric written to: {out_path}")
    except ValueError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"✗ Unexpected error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# orch scenario — E2E autonomous scenario test runner
# ---------------------------------------------------------------------------


@main.group("scenario")
def scenario_group() -> None:
    """Run and inspect end-to-end autonomous scenario tests.

    Scenarios live in ``./scenarios/`` (by default) and combine a pipeline
    template with grading criteria.  The ``run`` sub-command executes the
    referenced template, grades the output, and prints a score report.

    Examples::

        # Dry-run (no API key needed):
        ORCH_DRY_RUN=1 orch scenario run e2e-autonomous --dry-run

        # Live run (requires ANTHROPIC_API_KEY):
        orch scenario run e2e-autonomous
    """


@scenario_group.command("run")
@click.argument("scenario_id")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Execute the pipeline in dry-run mode (no real API calls). "
        "Also sets ORCH_DRY_RUN=1 for downstream graders so LLMJudgeGrader "
        "returns its stub score instead of making API calls."
    ),
)
@click.option(
    "--scenario-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Directory to search for scenario YAML files.  "
        "Defaults to ./scenarios/ in the current working directory."
    ),
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    default=None,
    help="Anthropic API key for live (non-dry-run) mode.",
)
@click.option(
    "--mode",
    type=click.Choice(["standalone", "openclaw"]),
    default="standalone",
    show_default=True,
    help=(
        "Grader routing mode for LLM judge criteria: "
        "'standalone' uses a direct Anthropic API key (--api-key / ANTHROPIC_API_KEY), "
        "'openclaw' routes judge calls through the OpenClaw gateway subscription token "
        "(OPENCLAW_GATEWAY_URL / OPENCLAW_GATEWAY_TOKEN env vars or --gateway-url / "
        "--gateway-token options)."
    ),
)
@click.option(
    "--gateway-url",
    default=None,
    help="OpenClaw gateway URL for openclaw grader mode (or set OPENCLAW_GATEWAY_URL).",
)
@click.option(
    "--gateway-token",
    default=None,
    help="OpenClaw gateway bearer token for openclaw grader mode (or set OPENCLAW_GATEWAY_TOKEN).",
)
def scenario_run(
    scenario_id: str,
    dry_run: bool,
    scenario_dir: Optional[Path],
    api_key: Optional[str],
    mode: str,
    gateway_url: Optional[str],
    gateway_token: Optional[str],
) -> None:
    """Run an E2E scenario test and print a score report.

    SCENARIO_ID is the stem of a YAML file inside --scenario-dir, or a
    path to a YAML file directly.

    The command:

    \b
    1. Loads the scenario YAML (validates required keys).
    2. Resolves and executes the referenced pipeline template.
    3. Grades the pipeline output against all acceptance criteria.
    4. Prints a per-criterion breakdown and overall score report.

    Exit code: 0 if the scenario passes (score ≥ threshold), 1 otherwise.

    Examples::

        # Dry-run — safe for CI, no API key needed:
        ORCH_DRY_RUN=1 orch scenario run e2e-autonomous --dry-run

        # Override scenario directory:
        orch scenario run my-scenario --scenario-dir tests/scenarios/ --dry-run

        # Live run with explicit API key:
        orch scenario run e2e-autonomous --api-key sk-ant-...
    """
    import json as _json
    import os as _os

    from rich.console import Console
    from rich.table import Table

    from .templates import TemplateEngine
    from .pipeline_runner import PipelineRunner
    from .sequencer import PhaseSequencer, StateMachineSequencer

    # Import ScenarioRunner from the scenario_runner package.
    # Try both importable forms (installed package and source layout).
    try:
        from scenario_runner.runner import ScenarioRunner
    except ImportError:
        # Fallback: add the project root to sys.path
        import sys as _sys
        project_root = Path(__file__).resolve().parent.parent.parent
        _sys.path.insert(0, str(project_root))
        from scenario_runner.runner import ScenarioRunner

    console = Console(highlight=False)

    # ------------------------------------------------------------------
    # 1. Resolve scenario file path
    # ------------------------------------------------------------------
    cwd = Path.cwd()
    default_scenarios_dir = cwd / "scenarios"
    base_dir = Path(scenario_dir) if scenario_dir else default_scenarios_dir

    # Accept: bare ID ("e2e-autonomous"), stem with extension, or full path
    if scenario_id.endswith(".yaml") or scenario_id.endswith(".yml"):
        candidate = Path(scenario_id)
    else:
        candidate = base_dir / f"{scenario_id}.yaml"
        if not candidate.exists():
            candidate = base_dir / f"{scenario_id}.yml"

    if not candidate.exists():
        click.echo(
            f"✗ Scenario not found: '{scenario_id}'\n"
            f"  Searched: {candidate}",
            err=True,
        )
        sys.exit(1)

    scenario_file = candidate.resolve()

    # ------------------------------------------------------------------
    # 2. Build LLM judge executor and create ScenarioRunner
    #
    #    In 'openclaw' mode the LLM judge is routed through the OpenClaw
    #    gateway so that scoring can use the subscription token rather than
    #    a raw Anthropic API key.  In 'standalone' mode the grader falls
    #    back to the api_key / ANTHROPIC_API_KEY path as before.
    #    In dry-run mode no executor is needed (ORCH_DRY_RUN=1 handles it).
    # ------------------------------------------------------------------
    runner_dir = scenario_file.parent

    # Resolve gateway credentials once here so that both the grader executor
    # (section 2) and the pipeline runner (section 3) can reuse the values
    # without duplicating the env-var lookup logic.
    effective_gw_url = gateway_url or _os.environ.get("OPENCLAW_GATEWAY_URL")
    effective_gw_token = gateway_token or _os.environ.get("OPENCLAW_GATEWAY_TOKEN")

    grader_executor = None
    if not dry_run and mode == "openclaw":
        try:
            from .openclaw_executor import OpenClawExecutor
            grader_executor = OpenClawExecutor(
                gateway_url=effective_gw_url,
                gateway_token=effective_gw_token,
            )
        except Exception as exc:
            click.echo(
                f"⚠ Could not create OpenClawExecutor for grader: {exc}\n"
                f"  LLM judge criteria will fall back to ANTHROPIC_API_KEY.",
                err=True,
            )

    scenario_runner = ScenarioRunner(scenarios_dir=runner_dir, executor=grader_executor)

    try:
        scenario = scenario_runner.load_scenario(scenario_file)
    except (ValueError, yaml.YAMLError) as exc:
        click.echo(f"✗ Invalid scenario '{scenario_file.name}': {exc}", err=True)
        sys.exit(1)

    scenario_name = scenario.get("name", scenario["id"])
    console.print(
        f"\n[bold]Scenario:[/bold] {scenario_name} "
        f"[dim]({scenario_file.name})[/dim]"
    )
    display_mode = "dry-run" if dry_run else mode
    console.print(f"[bold]Mode:[/bold]     {display_mode}")
    console.print()

    # ------------------------------------------------------------------
    # 3. Execute the pipeline referenced by the scenario
    # ------------------------------------------------------------------
    pipeline_ref: Optional[str] = scenario.get("pipeline")
    if not pipeline_ref:
        click.echo(
            "✗ Scenario has no 'pipeline' key — cannot execute pipeline.\n"
            "  Proceeding with empty pipeline output (all criteria will grade against {}).",
            err=True,
        )
        pipeline_output: dict = {}
    else:
        # Resolve template path: relative to scenario file first, then cwd
        template_path_candidate = scenario_file.parent / pipeline_ref
        if not template_path_candidate.exists():
            template_path_candidate = cwd / pipeline_ref
        if not template_path_candidate.exists():
            # Try resolving as a template name
            template_path_candidate = _resolve_template_arg(pipeline_ref)

        # Load + validate template
        engine = TemplateEngine()
        try:
            template = engine.load_template(template_path_candidate)
        except (FileNotFoundError, KeyError, ValueError, yaml.YAMLError) as exc:
            click.echo(f"✗ Cannot load pipeline template '{pipeline_ref}': {exc}", err=True)
            sys.exit(1)

        template_errors = engine.validate_template(template)
        if template_errors:
            click.echo(
                f"✗ Template '{pipeline_ref}' has {len(template_errors)} error(s):",
                err=True,
            )
            for err in template_errors:
                click.echo(f"  • {err}", err=True)
            sys.exit(1)

        # Build initial input from scenario
        initial_input: Dict[str, Any] = scenario.get("input", {}) or {}

        # Build PipelineRunner
        try:
            if dry_run:
                pipe_runner = PipelineRunner.dry_run(delay_seconds=0.0)
            elif mode == "openclaw":
                pipe_runner = PipelineRunner.openclaw(
                    gateway_url=effective_gw_url,
                    gateway_token=effective_gw_token,
                )
            else:
                pipe_runner = PipelineRunner.standalone(api_key=api_key)
        except ValueError as exc:
            click.echo(f"✗ {exc}", err=True)
            sys.exit(1)

        # Execute
        console.print(
            f"[bold]Pipeline:[/bold] {template.name!r}  "
            f"({len(template.phases)} phase{'s' if len(template.phases) != 1 else ''})"
        )
        console.print()

        with pipe_runner:
            _has_transitions = any(p.transitions for p in template.phases) or bool(
                template.default_transitions
            )
            _SequencerClass = StateMachineSequencer if _has_transitions else PhaseSequencer
            # Apply schema defaults for optional fields (#835) — same rationale
            # as run_template / pipeline_launch above. Belt-and-suspenders so
            # scenario-driven runs benefit from the same backward-compat shim.
            apply_config_schema_defaults(initial_input, getattr(template, 'config_schema', None))
            sequencer = _SequencerClass(template, pipe_runner, config=initial_input)
            try:
                exec_result = sequencer.execute(initial_input)
            except Exception as exc:
                click.echo(f"✗ Pipeline execution failed: {exc}", err=True)
                sys.exit(1)

        if exec_result.get("aborted"):
            failed_phase = exec_result.get("failed_phase", "unknown")
            click.echo(f"✗ Pipeline aborted at phase '{failed_phase}'", err=True)
            sys.exit(2)

        # Build grading input: expose both the final phase output AND all
        # phase outputs so that criteria can inspect earlier phases.
        #
        # Schema seen by graders:
        #   {
        #     "final":  <last phase output dict>,   # most criteria use this
        #     "phases": <dict[phase_id → output]>,  # allows inspecting earlier phases
        #   }
        #
        # Backward-compatibility note: graders that call output.get("article")
        # will still work for any pipeline whose final phase emits an "article"
        # key (the "final" sub-dict is preserved verbatim).
        final_output = exec_result.get("final_output", {})
        phase_outputs = exec_result.get("phase_outputs", {})
        pipeline_output = {"final": final_output, "phases": phase_outputs}

        phase_count = len(phase_outputs)
        console.print(
            f"[green]✓[/green] Pipeline completed  "
            f"({phase_count} phase{'s' if phase_count != 1 else ''})"
        )
        console.print()

    # ------------------------------------------------------------------
    # 4. Grade the pipeline output against scenario criteria.
    #
    #    ORCH_DRY_RUN=1 is set here (not at function entry) so that it is
    #    only active during grading and is ALWAYS cleaned up afterwards —
    #    even when sys.exit() is called.  This prevents the env var from
    #    leaking into subsequent test invocations in Click's CliRunner
    #    (single-process) context.
    # ------------------------------------------------------------------
    _dry_run_env_owned = dry_run and _os.environ.get("ORCH_DRY_RUN") != "1"
    if dry_run:
        _os.environ["ORCH_DRY_RUN"] = "1"
    try:
        score_result = scenario_runner.run_scenario(scenario, pipeline_output)
    except Exception as exc:
        click.echo(f"✗ Scenario grading failed: {exc}", err=True)
        sys.exit(1)
    finally:
        # Only remove the var if WE set it (don't clobber a pre-existing value).
        if _dry_run_env_owned:
            _os.environ.pop("ORCH_DRY_RUN", None)

    # ------------------------------------------------------------------
    # 5. Print score report
    # ------------------------------------------------------------------
    _print_score_report(console, score_result, scenario)

    # ------------------------------------------------------------------
    # 6. Exit with appropriate code
    # ------------------------------------------------------------------
    sys.exit(0 if score_result.passed else 1)


def _print_score_report(console, score_result, scenario: dict) -> None:
    """Print a rich score report to stdout.

    Format (AC-5):
    - Scenario ID, overall weighted score (0–100), pass/fail verdict
    - Per-criterion rows: ID, type, weight/gate, score (0–100), pass/fail
    - Gate criteria are labelled [GATE]
    - Overall summary line at the bottom
    """
    from rich.table import Table

    # ── Per-criterion table ────────────────────────────────────────────
    crit_table = Table(
        title="Acceptance Criteria",
        show_header=True,
        header_style="bold cyan",
    )
    crit_table.add_column("Criterion", style="cyan", no_wrap=True)
    crit_table.add_column("Type", justify="center")
    crit_table.add_column("Weight", justify="center")
    crit_table.add_column("Score", justify="right")
    crit_table.add_column("Result", justify="center")

    for cr in score_result.criterion_results:
        weight_label = "[GATE]" if cr.is_gate else str(cr.weight)
        score_pct = f"{cr.grade.score * 100:.1f}"
        result_icon = (
            "[green]✓ PASS[/green]"
            if cr.grade.passed
            else "[red]✗ FAIL[/red]"
        )
        crit_table.add_row(
            cr.criterion_id,
            cr.grade.grader_type,
            weight_label,
            score_pct,
            result_icon,
        )

    console.print(crit_table)
    console.print()

    # ── Summary ────────────────────────────────────────────────────────
    overall_pct = score_result.weighted_score * 100
    threshold_pct = float(scenario.get("scoring", {}).get("pass_threshold", 0.70)) * 100
    verdict = (
        "[bold green]✓ PASS[/bold green]"
        if score_result.passed
        else "[bold red]✗ FAIL[/bold red]"
    )
    gate_status = (
        "[green]all passed[/green]"
        if score_result.gates_passed
        else "[red]one or more FAILED[/red]"
    )

    console.print(
        f"[bold]Scenario:[/bold]  {score_result.scenario_id}"
    )
    console.print(
        f"[bold]Score:[/bold]     {overall_pct:.1f} / 100  "
        f"(threshold {threshold_pct:.0f})"
    )
    console.print(f"[bold]Gates:[/bold]     {gate_status}")
    console.print(f"[bold]Verdict:[/bold]   {verdict}")
    console.print()


# ---------------------------------------------------------------------------
# Review Queue Commands (Issue #331.4)
# ---------------------------------------------------------------------------

@main.group()
def reviews() -> None:
    """Manage the human review queue for pipeline runs."""


@reviews.command(name="list")
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum number of items.")
@click.option("--offset", type=int, default=0, show_default=True, help="Number of items to skip.")
@click.option("--db-path", "reviews_db_path", type=click.Path(path_type=Path), default=None,
              help="Path to the orchestration engine database.")
def reviews_list(limit: int, offset: int, reviews_db_path: Optional[Path]) -> None:
    """List pipeline runs pending human review."""
    from .db import Database as _Database

    _db_path = reviews_db_path or default_db_path()
    db = _Database(_db_path)
    items = db.list_pending_reviews(limit=limit, offset=offset)
    total = db.count_pending_reviews()

    if not items:
        click.echo("No runs pending review.")
        return

    click.echo(f"Pending reviews: {total} total  (showing {len(items)}  offset={offset})\n")
    headers = ["RUN ID", "TEMPLATE", "CREATED AT", "SCORE", "TIER"]
    rows = []
    for r in items:
        rows.append([
            r.get("run_id", ""),
            r.get("template_id", ""),
            str(r.get("created_at", ""))[:19],
            f"{r.get('confidence_score', ''):.4f}" if r.get("confidence_score") is not None else "n/a",
            r.get("tier_name", "n/a"),
        ])
    print_table(headers, rows)


@reviews.command(name="approve")
@click.argument("run_id")
@click.option("--reviewed-by", default=None, help="Reviewer identifier.")
@click.option("--note", default=None, help="Review note.")
@click.option("--db-path", "reviews_db_path", type=click.Path(path_type=Path), default=None,
              help="Path to the orchestration engine database.")
def reviews_approve(run_id: str, reviewed_by: Optional[str], note: Optional[str],
                    reviews_db_path: Optional[Path]) -> None:
    """Approve a pipeline run that is pending human review."""
    from .db import Database as _Database

    _db_path = reviews_db_path or default_db_path()
    db = _Database(_db_path)

    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"Error: run '{run_id}' not found.", err=True)
        sys.exit(1)
    if run.get("status") != "pending_review":
        click.echo(
            f"Error: run '{run_id}' is in status '{run.get('status')}', "
            "not 'pending_review'.",
            err=True,
        )
        sys.exit(1)

    updated = db.approve_pipeline_run(run_id, reviewed_by=reviewed_by, note=note)
    if updated:
        click.echo(f"✓ Run '{run_id}' approved (status → success).")
    else:
        click.echo(f"✗ Could not approve run '{run_id}'.", err=True)
        sys.exit(1)


@reviews.command(name="reject")
@click.argument("run_id")
@click.argument("reason")
@click.option("--reviewed-by", default=None, help="Reviewer identifier.")
@click.option("--db-path", "reviews_db_path", type=click.Path(path_type=Path), default=None,
              help="Path to the orchestration engine database.")
def reviews_reject(run_id: str, reason: str, reviewed_by: Optional[str],
                   reviews_db_path: Optional[Path]) -> None:
    """Reject a pipeline run that is pending human review.

    REASON is a short description of why the run was rejected.
    """
    from .db import Database as _Database

    _db_path = reviews_db_path or default_db_path()
    db = _Database(_db_path)

    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"Error: run '{run_id}' not found.", err=True)
        sys.exit(1)
    if run.get("status") != "pending_review":
        click.echo(
            f"Error: run '{run_id}' is in status '{run.get('status')}', "
            "not 'pending_review'.",
            err=True,
        )
        sys.exit(1)

    updated = db.reject_pipeline_run(run_id, reason=reason, reviewed_by=reviewed_by)
    if updated:
        click.echo(f"✓ Run '{run_id}' rejected (status → rejected).")
    else:
        click.echo(f"✗ Could not reject run '{run_id}'.", err=True)
        sys.exit(1)


@main.command("mcp")
@click.option(
    "--transport",
    default="stdio",
    help="Transport protocol: stdio or sse",
)
@click.option(
    "--port",
    default=8000,
    type=int,
    show_default=True,
    help="Port for SSE transport (default: 8000)",
)
def mcp_server(transport: str, port: int) -> None:
    """Start the MCP server for IDE integration (Claude Code, Cursor)."""
    supported = ["stdio", "sse"]
    if transport not in supported:
        click.echo(
            f"Unsupported transport: {transport}. Supported: stdio, sse",
            err=True,
        )
        sys.exit(1)
    if not (1 <= port <= 65535):
        click.echo(
            f"Invalid port: {port}. Port must be between 1 and 65535",
            err=True,
        )
        sys.exit(1)
    from .mcp import run_mcp_server
    run_mcp_server(transport=transport, port=port)


if __name__ == '__main__':
    main()