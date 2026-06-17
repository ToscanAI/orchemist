"""Task-queue command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1002, 950b). These command
functions previously lived inline in ``cli/__init__.py``; their bodies are moved
here VERBATIM. Each command self-registers on the shared ``main`` Click group
(imported from ``._root``) at import time via its ``@main.command`` decorator,
so the facade only needs to import this module for the registration side effect.
"""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import click

# The queue commands below resolve ``Database`` and ``get_queue`` through the
# ``orchestration_engine.cli`` facade so that the existing tests, which
# ``patch("orchestration_engine.cli.Database" | ".get_queue")`` and then invoke
# these (now-relocated) commands, keep mocking the same objects they did when this
# code lived inline in ``cli/__init__.py`` (EPIC #942 / 950b). The attributes are
# read at call time, so the partially-initialised facade module during package
# import is never a problem.
import orchestration_engine.cli as _cli  # noqa: E402  (call-time facade ref; see note)

from ..schemas import Priority, TaskFilters, TaskSpec, TaskState, TaskType
from ..timestamps import now_utc
from ._helpers import _fmt_elapsed, format_datetime, print_table
from ._root import main


@main.command()
@click.option(
    "--type",
    "task_type",
    type=click.Choice([t.value for t in TaskType]),
    required=True,
    help="Task type",
)
@click.option("--payload", required=True, help="Task payload as JSON string")
@click.option(
    "--priority",
    type=click.Choice([p.name.lower() for p in Priority]),
    default="normal",
    help="Task priority",
)
@click.option("--max-retries", type=int, help="Maximum retry attempts")
@click.option("--timeout", type=int, help="Timeout in seconds")
@click.option("--min-confidence", type=float, help="Minimum confidence score (0.0-1.0)")
@click.option("--cost-limit", type=float, help="Cost limit in USD")
@click.option("--orchestra-id", help="Orchestra workflow ID")
@click.option("--orchestra-phase", help="Phase within orchestra")
@click.option("--tag", multiple=True, help="Tags (can be used multiple times)")
@click.option("--created-by", help="Creator identifier")
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
    created_by: Optional[str],
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
            created_by=created_by,
        )

        # Submit task
        task_queue = _cli.get_queue()
        task_id = task_queue.submit_task(task_spec)

        click.echo("✓ Task submitted successfully")
        click.echo(f"Task ID: {task_id}")
        click.echo(f"Type: {task_type}")
        click.echo(f"Priority: {priority}")

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error submitting task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument("run_or_task_id", required=False, metavar="[RUN-ID|TASK-ID]")
def status(run_or_task_id: Optional[str]) -> None:  # noqa: C901
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
            _db = _cli.Database()
            run = _db.get_pipeline_run(run_or_task_id)
        except Exception:  # noqa: BLE001
            run = None

        if run is not None:
            # --- Pipeline run detail ---
            _print_run_detail(run)
            return

        # Fall through to legacy task queue status
        try:
            task_queue = _cli.get_queue()
            task_status_obj = task_queue.get_task_status(run_or_task_id)
            if not task_status_obj:
                click.echo(
                    f"ID '{run_or_task_id}' not found in pipeline runs or task queue.", err=True
                )
                sys.exit(1)
            click.echo(f"Task Status: {run_or_task_id}")
            click.echo(f"├─ Type: {task_status_obj.task_type.value}")
            click.echo(f"├─ State: {task_status_obj.state.value}")
            click.echo(f"├─ Priority: {task_status_obj.priority.name}")
            click.echo(f"├─ Created: {format_datetime(task_status_obj.created_at)}")
            click.echo(f"├─ Started: {format_datetime(task_status_obj.started_at)}")
            click.echo(f"├─ Completed: {format_datetime(task_status_obj.completed_at)}")
            if task_status_obj.retry_count > 0:
                click.echo(
                    f"├─ Retries: {task_status_obj.retry_count}/{task_status_obj.max_retries}"
                )
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
        except Exception as e:  # noqa: BLE001
            click.echo(f"Error getting status: {e}", err=True)
            sys.exit(1)

    else:
        # No ID given → list recent pipeline runs (last 10)
        try:
            _db = _cli.Database()
            runs = _db.list_pipeline_runs(limit=10)
        except Exception:  # noqa: BLE001
            runs = []

        if not runs:
            click.echo("No pipeline runs found.  Use 'orch launch <template>' to begin.")
            click.echo("\nTip: 'orch status' lists recent async runs from 'orch launch'.")
            # Fall back to queue stats as secondary info
            try:
                task_queue = _cli.get_queue()
                stats = task_queue.get_queue_stats()
                click.echo(
                    f"\nQueue: {stats.queued} queued  {stats.running} running  {stats.completed} done"  # noqa: E501
                )
            except Exception:  # noqa: BLE001
                pass
            return

        click.echo("Recent Pipeline Runs (last 10)")
        click.echo("─" * 72)
        for run in runs:
            _print_run_summary_line(run)


def _print_run_summary_line(run: Dict[str, Any]) -> None:
    """Print a single-line summary of a pipeline run."""
    run_id = run["run_id"]
    status = run["status"]
    template_id = run.get("template_id", "?")[:20]
    mode = run.get("mode", "?")
    created = (run.get("created_at") or "")[:16]

    # Check PID liveness if running
    if status == "running":
        pid = run.get("pid")
        if pid:
            try:
                from ..daemon import is_process_alive  # noqa: PLC0415

                if not is_process_alive(pid):
                    status = "crashed"
            except Exception:  # noqa: BLE001
                pass

    status_icon = {
        "pending": "⏳",
        "running": "🔄",
        "success": "✅",
        "failed": "❌",
        "cancelled": "🚫",
        "crashed": "💀",
        "scoring_failed": "🔴",
    }.get(status, "❓")

    current_phase = run.get("current_phase") or "-"
    # Append scoring suffix when scoring_status is set (Issue #287)
    _scoring_status = run.get("scoring_status")
    _scoring_suffix = f"  [score={_scoring_status}]" if _scoring_status else ""
    click.echo(
        f"{run_id}  {status_icon} {status:<10}  {template_id:<22}  "
        f"phase={current_phase:<20}  {created}  [{mode}]{_scoring_suffix}"
    )


def _print_run_detail(run: Dict[str, Any]) -> None:  # noqa: C901
    """Print detailed status for a single pipeline run, checking PID liveness."""
    import json as _json  # noqa: PLC0415

    run_id = run["run_id"]
    status = run["status"]
    pid = run.get("pid")

    # Check PID liveness
    if status == "running" and pid:
        try:
            from ..daemon import is_process_alive  # noqa: PLC0415

            if not is_process_alive(pid):
                status = "crashed"
                # Update DB
                try:
                    _cli.Database().update_pipeline_run(run_id, status="crashed")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    # Elapsed time
    started_at = run.get("started_at")
    completed_at = run.get("completed_at")
    elapsed_str = "n/a"
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if completed_at:
                end_dt = datetime.fromisoformat(completed_at)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            else:
                end_dt = now_utc()
            elapsed_s = (end_dt - start_dt).total_seconds()
            elapsed_str = _fmt_elapsed(elapsed_s)
        except Exception:  # noqa: BLE001
            pass

    # Completed phases
    try:
        completed_phases = _json.loads(run.get("completed_phases") or "[]")
    except Exception:  # noqa: BLE001
        completed_phases = []

    status_icon = {
        "pending": "⏳",
        "running": "🔄",
        "success": "✅",
        "failed": "❌",
        "cancelled": "🚫",
        "crashed": "💀",
        "scoring_failed": "🔴",
    }.get(status, "❓")

    click.echo(f"Pipeline Run: {run_id}")
    click.echo(f"├─ Status:     {status_icon} {status}")
    click.echo(f"├─ Template:   {run.get('template_id', '?')}")
    click.echo(f"├─ Mode:       {run.get('mode', '?')}")
    click.echo(f"├─ Elapsed:    {elapsed_str}")
    click.echo(f"├─ Current:    {run.get('current_phase') or '(none)'}")
    click.echo(f"├─ Completed:  {len(completed_phases)} phase(s): {completed_phases}")
    click.echo(f"├─ PID:        {pid or 'n/a'}")
    click.echo(f"├─ Output:     {run.get('output_dir', '?')}")
    if run.get("error_message"):
        click.echo(f"├─ Error:      {run['error_message']}")
    # Stall detection (#413) — check for recent stall events
    if status == "running":
        try:
            _db = _cli.Database()
            _stall_events = _db.list_pipeline_run_events(run_id, after_id=0, limit=100)
            _recent_stalls = [e for e in _stall_events if e.get("event_type") == "stall_detected"]
            if _recent_stalls:
                _last_stall = _recent_stalls[-1]
                _stall_meta = _json.loads(_last_stall.get("metadata_json", "{}"))
                _stall_msg = _stall_meta.get("message", "No token progress detected")
                click.echo(f"├─ Warning:    ⚠️  {_stall_msg}")
        except Exception:  # noqa: BLE001
            pass
    # Scoring outcome (Issue #287)
    _scoring_status = run.get("scoring_status")
    _scoring_score = run.get("scoring_score")
    if _scoring_status is not None:
        _score_icon = {"passed": "✅", "failed": "❌", "error": "⚠️"}.get(_scoring_status, "❓")
        _score_suffix = f"  (score={_scoring_score:.3f})" if _scoring_score is not None else ""
        click.echo(f"├─ Scoring:    {_score_icon} {_scoring_status}{_score_suffix}")
    click.echo(f"├─ Created:    {(run.get('created_at') or '')[:19]}")
    click.echo(f"└─ Logs:       orch logs {run_id}")


@main.command()
@click.option(
    "--state",
    multiple=True,
    type=click.Choice([s.value for s in TaskState]),
    help="Filter by task state (can be used multiple times)",
)
@click.option(
    "--type",
    "task_type",
    multiple=True,
    type=click.Choice([t.value for t in TaskType]),
    help="Filter by task type (can be used multiple times)",
)
@click.option(
    "--priority",
    multiple=True,
    type=click.Choice([p.name.lower() for p in Priority]),
    help="Filter by priority (can be used multiple times)",
)
@click.option("--orchestra-id", help="Filter by orchestra ID")
@click.option("--tag", multiple=True, help="Filter by tags")
@click.option("--limit", type=int, default=20, help="Maximum number of tasks to show")
@click.option("--offset", type=int, default=0, help="Number of tasks to skip")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format",
)
def list(  # noqa: A001
    state: tuple,
    task_type: tuple,
    priority: tuple,
    orchestra_id: Optional[str],
    tag: tuple,
    limit: int,
    offset: int,
    output_format: str,
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
            offset=offset,
        )

        # Get tasks
        task_queue = _cli.get_queue()
        tasks = task_queue.list_tasks(filters)

        if output_format == "json":
            # JSON output
            tasks_data = []
            for task in tasks:
                tasks_data.append(  # noqa: PERF401
                    {
                        "task_id": task.task_id,
                        "type": task.task_type.value,
                        "state": task.state.value,
                        "priority": task.priority.name,
                        "created_at": task.created_at.isoformat(),
                        "retry_count": task.retry_count,
                        "orchestra_id": task.orchestra_id,
                        "title": task.title,
                        "description": task.description,
                        "tags": task.tags,
                    }
                )
            click.echo(json.dumps(tasks_data, indent=2))

        else:
            # Table output
            if not tasks:
                click.echo("No tasks found matching the criteria")
                return

            headers = [
                "Task ID",
                "Type",
                "State",
                "Priority",
                "Created",
                "Retries",
                "Orchestra",
                "Title",
            ]

            rows = []
            for task in tasks:
                rows.append(  # noqa: PERF401
                    [
                        task.task_id[:8] + "...",  # Truncate task ID
                        task.task_type.value,
                        task.state.value,
                        task.priority.name,
                        format_datetime(task.created_at),
                        f"{task.retry_count}" if task.retry_count > 0 else "-",
                        task.orchestra_id[:8] + "..." if task.orchestra_id else "-",
                        (
                            task.title[:30] + "..."
                            if task.title and len(task.title) > 30
                            else task.title or "-"
                        ),
                    ]
                )

            print_table(headers, rows)

            if len(tasks) == limit:
                click.echo(f"\nShowing {limit} tasks (use --offset to see more)")

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error listing tasks: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument("task_id")
@click.option("--force", is_flag=True, help="Force cancellation without confirmation")
def cancel(task_id: str, force: bool) -> None:
    """Cancel a queued or running task."""
    try:
        task_queue = _cli.get_queue()

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
            click.echo(
                f"✗ Failed to cancel task {task_id} (may not be in cancellable state)", err=True
            )
            sys.exit(1)

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error cancelling task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument("task_id")
def retry(task_id: str) -> None:
    """Manually retry a failed task."""
    try:
        task_queue = _cli.get_queue()

        # Check task status first
        task_status = task_queue.get_task_status(task_id)
        if not task_status:
            click.echo(f"Task {task_id} not found", err=True)
            sys.exit(1)

        if task_status.state not in [TaskState.FAILED, TaskState.PERMANENTLY_FAILED]:
            click.echo(
                f"Task {task_id} is not in failed state (current: {task_status.state.value})",
                err=True,
            )
            sys.exit(1)

        # Retry task
        success = task_queue.retry_failed_task(task_id)

        if success:
            click.echo(f"✓ Task {task_id} queued for retry")
        else:
            click.echo(
                f"✗ Failed to retry task {task_id} (may have exceeded max retries)", err=True
            )
            sys.exit(1)

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error retrying task: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--limit", type=int, default=20, help="Maximum number of dead letter tasks to show")
def dead_letter(limit: int) -> None:
    """Show tasks in the dead letter queue."""
    try:
        task_queue = _cli.get_queue()
        dead_tasks = task_queue.get_dead_letter_tasks(limit)

        if not dead_tasks:
            click.echo("No tasks in dead letter queue")
            return

        headers = ["Original ID", "Type", "Failure Reason", "Attempts", "Failed At"]

        rows = []
        for task in dead_tasks:
            rows.append(  # noqa: PERF401
                [
                    task.original_task_id[:8] + "...",
                    task.task_type.value,
                    (
                        task.failure_reason[:50] + "..."
                        if len(task.failure_reason) > 50
                        else task.failure_reason
                    ),
                    str(task.failure_count),
                    format_datetime(task.created_at),
                ]
            )

        print_table(headers, rows)

        if len(dead_tasks) == limit:
            click.echo(f"\nShowing {limit} dead letter tasks")

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error getting dead letter tasks: {e}", err=True)
        sys.exit(1)


@main.command()
def health() -> None:
    """Check system health and configuration."""
    try:
        task_queue = _cli.get_queue()
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

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error checking health: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--detailed", "-d", is_flag=True, help="Show detailed worker information")
def workers(detailed: bool) -> None:  # noqa: C901
    """Show active worker status."""
    try:
        # Import here to avoid circular imports
        from ..concurrency import WorkerPool  # noqa: PLC0415
        from ..config import get_global_config  # noqa: PLC0415
        from ..db import Database  # noqa: PLC0415

        config = get_global_config()
        db = Database()
        worker_pool = WorkerPool(db, config)

        status = worker_pool.get_worker_status()

        if not status:
            click.echo("No worker status available")
            return

        # Summary
        click.echo("👥 Worker Pool Status")
        click.echo("=" * 40)
        click.echo(f"Total Workers: {status['total_workers']}")
        click.echo(f"Max Workers: {status['max_workers']}")

        # Resource status
        if "resource_status" in status:
            resources = status["resource_status"]
            click.echo("\n💻 Resource Usage:")
            click.echo(
                f"  Sessions: {resources['current_sessions']}/{resources['max_sessions']} "
                f"({resources['session_utilization']:.1f}%)"
            )

            if resources.get("daily_budget_usd"):
                click.echo(
                    f"  Budget: ${resources['daily_cost_usd']:.2f}/"
                    f"${resources['daily_budget_usd']:.2f} "
                    f"({resources['budget_utilization']:.1f}%)"
                )
            else:
                click.echo(f"  Daily Cost: ${resources['daily_cost_usd']:.2f}")

        # Workers by state
        if "workers_by_state" in status and status["workers_by_state"]:
            click.echo("\n📊 Workers by State:")

            for state, workers in status["workers_by_state"].items():
                count = len(workers)
                state_emoji = {
                    "idle": "😴",
                    "assigned": "📝",
                    "running": "🏃",
                    "stale": "💀",
                    "terminated": "🚫",
                }

                emoji = state_emoji.get(state, "❓")
                click.echo(f"  {emoji} {state.capitalize()}: {count}")

                if detailed and workers:
                    for worker in workers[:3]:  # Show first 3 workers
                        worker_id = (
                            worker["worker_id"][:12] + "..."
                            if len(worker["worker_id"]) > 15
                            else worker["worker_id"]
                        )
                        age = worker.get("heartbeat_age_seconds", 0)

                        task_info = ""
                        if worker.get("assigned_task_id"):
                            task_id = worker["assigned_task_id"][:8] + "..."
                            task_info = f" (task: {task_id})"

                        click.echo(f"    • {worker_id} - {age:.0f}s ago{task_info}")

                    if len(workers) > 3:
                        click.echo(f"    ... and {len(workers) - 3} more")
        else:
            click.echo("No active workers")

    except Exception as e:  # noqa: BLE001
        click.echo(f"Error getting worker status: {e}", err=True)
        sys.exit(1)
