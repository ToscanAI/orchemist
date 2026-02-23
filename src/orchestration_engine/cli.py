"""Command Line Interface for the Orchestration Engine.

Provides CLI commands for task queue management: submit, status, list, cancel, etc.
Uses Click for command structure and rich formatting for output.
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import yaml

import click
from decimal import Decimal

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


def _extract_output_text(phase_out: Dict[str, Any]) -> str:
    """Extract human-readable text from a serialised phase output dict.

    Tries common keys used by different executors:
      - ``result.output``  — explicit output key (future executors)
      - ``result.text``    — AnthropicExecutor plain-text response
      - ``result.content`` — alternative content key
      - ``result.message`` — DryRunExecutor mock message
    Falls back to a JSON representation of the ``result`` sub-dict.
    """
    import json as _json

    inner = phase_out.get('result', {})
    if not isinstance(inner, dict):
        return str(inner)
    for key in ('output', 'text', 'content', 'message'):
        if key in inner:
            return str(inner[key])
    if inner:
        return _json.dumps(inner, indent=2, default=str)
    return ""


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
@click.argument('task_id', required=False)
def status(task_id: Optional[str]) -> None:
    """Show task status or overall queue statistics."""
    try:
        task_queue = get_queue()
        
        if task_id:
            # Show specific task status
            task_status = task_queue.get_task_status(task_id)
            
            if not task_status:
                click.echo(f"Task {task_id} not found", err=True)
                sys.exit(1)
            
            click.echo(f"Task Status: {task_id}")
            click.echo(f"├─ Type: {task_status.task_type.value}")
            click.echo(f"├─ State: {task_status.state.value}")
            click.echo(f"├─ Priority: {task_status.priority.name}")
            click.echo(f"├─ Created: {format_datetime(task_status.created_at)}")
            click.echo(f"├─ Started: {format_datetime(task_status.started_at)}")
            click.echo(f"├─ Completed: {format_datetime(task_status.completed_at)}")
            
            if task_status.retry_count > 0:
                click.echo(f"├─ Retries: {task_status.retry_count}/{task_status.max_retries}")
            
            if task_status.next_retry_at:
                click.echo(f"├─ Next Retry: {format_datetime(task_status.next_retry_at)}")
            
            if task_status.orchestra_id:
                click.echo(f"├─ Orchestra: {task_status.orchestra_id}")
                if task_status.orchestra_phase:
                    click.echo(f"├─ Phase: {task_status.orchestra_phase}")
            
            if task_status.progress_percentage is not None:
                click.echo(f"└─ Progress: {task_status.progress_percentage:.1f}%")
            else:
                click.echo("└─ Progress: N/A")
            
        else:
            # Show queue statistics
            stats = task_queue.get_queue_stats()
            
            click.echo("Queue Statistics")
            click.echo(f"├─ Timestamp: {format_datetime(stats.timestamp)}")
            click.echo(f"├─ Total Tasks: {stats.total_tasks}")
            click.echo(f"├─ Queued: {stats.queued}")
            click.echo(f"├─ Running: {stats.running}")
            click.echo(f"├─ Completed: {stats.completed}")
            click.echo(f"├─ Failed: {stats.failed}")
            click.echo(f"├─ Retrying: {stats.retrying}")
            click.echo(f"├─ Cancelled: {stats.cancelled}")
            click.echo(f"├─ Workers: {stats.active_workers}/{stats.max_workers} ({stats.worker_utilization:.1f}%)")
            click.echo(f"├─ Dead Letter: {stats.dead_letter_count}")
            click.echo(f"├─ Avg Execution: {format_duration(stats.avg_execution_time_seconds)}")
            
            if stats.queue_depth_warning:
                click.echo(f"⚠️  Warning: High queue depth ({stats.queued} tasks)")
            
            if stats.stale_tasks_warning:
                click.echo(f"⚠️  Warning: Stale tasks detected")
            
            # Priority breakdown
            if stats.priority_breakdown:
                click.echo("\nPriority Breakdown:")
                for priority, count in stats.priority_breakdown.items():
                    click.echo(f"  {priority}: {count}")
            
            # Type breakdown
            if stats.type_breakdown:
                click.echo("\nType Breakdown:")
                for task_type, count in stats.type_breakdown.items():
                    click.echo(f"  {task_type}: {count}")
    
    except Exception as e:
        click.echo(f"Error getting status: {e}", err=True)
        sys.exit(1)


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
@click.argument('task_id', required=True)
@click.option('--follow', '-f', is_flag=True, help='Follow progress in real-time')
@click.option('--refresh', default=2, help='Refresh interval in seconds (with --follow)')
def watch(task_id: str, follow: bool, refresh: int) -> None:
    """Watch task progress in real-time."""
    try:
        # Import here to avoid circular imports
        from .progress import ProgressTracker
        from .db import Database
        
        db = Database()
        tracker = ProgressTracker(db)
        
        def display_progress():
            progress = tracker.get_task_progress(task_id, include_events=True)
            if not progress:
                click.echo(f"❌ Task {task_id} not found")
                return False
            
            # Clear screen for follow mode
            if follow:
                click.clear()
            
            # Display task header
            click.echo(f"📊 Task Progress: {task_id}")
            click.echo("=" * 60)
            
            # Status overview
            status_emoji = {
                "queued": "⏳",
                "running": "🔄", 
                "success": "✅",
                "failed": "❌",
                "retry": "🔁",
                "permanently_failed": "💀",
                "cancelled": "🚫"
            }
            
            emoji = status_emoji.get(progress.current_state.value, "❓")
            click.echo(f"Status: {emoji} {progress.current_state.value.upper()}")
            
            if progress.current_message:
                click.echo(f"Message: {progress.current_message}")
            
            click.echo(f"Progress: {progress.progress_percentage:.1f}%")
            
            if progress.execution_time_seconds:
                click.echo(f"Runtime: {progress.execution_time_seconds:.1f}s")
            
            if progress.current_model:
                click.echo(f"Model: {progress.current_model}")
            
            click.echo(f"Attempt: {progress.attempt_number}")
            
            if progress.retry_count > 0:
                click.echo(f"Retries: {progress.retry_count}")
            
            # Resource usage
            if progress.total_tokens > 0 or float(progress.total_cost_usd) > 0:
                click.echo("\n💰 Resource Usage:")
                if progress.total_tokens > 0:
                    click.echo(f"  Tokens: {progress.total_tokens:,}")
                if float(progress.total_cost_usd) > 0:
                    click.echo(f"  Cost: ${progress.total_cost_usd}")
            
            # Recent events
            click.echo(f"\n📋 Recent Events ({len(progress.events)}):")
            for event in progress.events[-5:]:  # Show last 5 events
                timestamp = event.timestamp.strftime("%H:%M:%S")
                click.echo(f"  {timestamp} - {event.event_type}: {event.message or 'N/A'}")
            
            # Return whether task is still active
            return progress.is_active
        
        # Display progress
        is_active = display_progress()
        
        # Follow mode
        if follow and is_active:
            click.echo(f"\n🔄 Following progress (refresh every {refresh}s, Ctrl+C to stop)...")
            
            try:
                while is_active:
                    time.sleep(refresh)
                    is_active = display_progress()
                
                click.echo("\n✅ Task completed - stopped following")
            
            except KeyboardInterrupt:
                click.echo("\n👋 Stopped following progress")
    
    except Exception as e:
        click.echo(f"Error watching task: {e}", err=True)
        sys.exit(1)


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
@click.argument('template_file', type=click.Path(exists=True, path_type=Path))
@click.option(
    '--mode',
    type=click.Choice(['standalone', 'openclaw', 'dry-run']),
    default='standalone',
    show_default=True,
    help='Execution mode: standalone (direct API), openclaw (sub-agent), dry-run (mock).',
)
@click.option(
    '--api-key',
    envvar='ANTHROPIC_API_KEY',
    default=None,
    help='Anthropic API key for standalone mode (or set ANTHROPIC_API_KEY).',
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
def run_template(
    template_file: Path,
    mode: str,
    api_key: Optional[str],
    input_json: Optional[str],
    input_file: Optional[Path],
    output_dir: Optional[Path],
    dry_run_delay: float,
    dry_run_failure_rate: float,
) -> None:
    """Execute a pipeline template end-to-end.

    TEMPLATE_FILE is the path to a YAML pipeline template.

    Examples:

      # Standalone with inline input:
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

    from rich.console import Console
    from rich.table import Table

    from .templates import TemplateEngine
    from .pipeline_runner import PipelineRunner
    from .sequencer import PhaseSequencer

    console = Console(highlight=False)
    run_start = time.time()

    # --- 1. Load and validate template --------------------------------
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
    if errors:
        click.echo(f"✗ Template has {len(errors)} structural error(s):", err=True)
        for err in errors:
            click.echo(f"  • {err}", err=True)
        sys.exit(1)

    # --- 1b. Default output directory (Feature #72) ------------------
    if output_dir is None:
        output_dir = Path(
            f"./output/{re.sub(r'[^\w\-]', '_', template.id)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )

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

    # --- 3. Build PipelineRunner based on mode -----------------------
    try:
        if mode == 'standalone':
            runner = PipelineRunner.standalone(api_key=api_key)
        elif mode == 'openclaw':
            runner = PipelineRunner.openclaw()
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

    with runner:
        sequencer = PhaseSequencer(
            template, runner, config=initial_input,
            on_phase_complete=_on_phase_complete,
        )

        try:
            result = sequencer.execute(initial_input)
        except Exception as exc:
            click.echo(f"✗ Pipeline execution crashed: {exc}", err=True)
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
        phase_text = _extract_output_text(phase_out)
        (output_dir / f"{safe_id}.md").write_text(
            f"# Phase: {phase_id}\n\n{phase_text}\n"
        )

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


@main.command("validate")
@click.argument('template_file', type=click.Path(exists=True, path_type=Path))
def validate_template(template_file: Path) -> None:
    """Validate a pipeline template and report any structural errors.

    TEMPLATE_FILE is the path to a YAML pipeline template.
    """
    try:
        from .templates import TemplateEngine, PipelineTemplate  # noqa: F401

        engine = TemplateEngine()
        template: PipelineTemplate = engine.load_template(template_file)
        errors = engine.validate_template(template)

        if errors:
            click.echo(f"✗ Template {template_file!r} has {len(errors)} error(s):", err=True)
            for err in errors:
                click.echo(f"  • {err}", err=True)
            sys.exit(1)
        else:
            click.echo(f"✓ Template {template_file!r} is valid ({len(template.phases)} phases)")
    except (KeyError, ValueError) as exc:
        click.echo(f"✗ Invalid template: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command("list-phases")
@click.argument('template_file', type=click.Path(exists=True, path_type=Path))
def list_phases(template_file: Path) -> None:
    """Show execution order and model tiers for a pipeline template.

    TEMPLATE_FILE is the path to a YAML pipeline template.
    """
    try:
        from .templates import TemplateEngine, PipelineTemplate  # noqa: F401

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

    Scanned in order:
    1. ./templates/  → label "templates"
    2. ./examples/   → label "examples"
    3. ~/.orch/templates/ → label "user"
    """
    user_dir = Path.home() / ".orch" / "templates"
    # Don't create ~/.orch/templates/ just for scanning — only include if it exists
    paths = [
        (Path("./templates"), "templates"),
        (Path("./examples"), "examples"),
    ]
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
    seen_paths: set = set()

    for search_path, source_label in resolution_paths:
        if not search_path.exists():
            continue
        for filepath in sorted(search_path.glob("*.yaml")) + sorted(search_path.glob("*.yml")):
            resolved = filepath.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                template = engine.load_template(filepath)
                found.append((filepath, source_label, template))
            except Exception as exc:
                click.echo(f"[warn] Skipping {filepath}: {exc}", err=True)

    return found


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
    """List available pipeline templates from all resolution paths."""
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
        template_file=hello_yaml,
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
        "                        See all available templates"
    )
    console.print(
        "  [cyan]orch templates info content-pipeline-mvp[/cyan]"
        "   Explore a real pipeline"
    )
    console.print(
        "  [cyan]orch start content-pipeline-mvp[/cyan]"
        "            Run interactively (needs API key)"
    )
    console.print()


# ---------------------------------------------------------------------------
# Feature #66 — orch start (interactive wizard)
# ---------------------------------------------------------------------------

def _find_template(name_or_path: str):
    """Locate a template by file path OR by name/ID.

    Returns:
        (template_path: Path, template: PipelineTemplate)

    Raises SystemExit on failure.
    """
    import os as _os
    from .templates import TemplateEngine

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
            click.echo(f"✗ Template file not found: {name_or_path}", err=True)
            sys.exit(1)
        except Exception as exc:
            click.echo(f"✗ Could not load template: {exc}", err=True)
            sys.exit(1)

    # Name / ID lookup — exact match first, then partial/slug match
    found_all = _scan_templates()
    search = name_or_path.lower()
    for filepath, _source, tmpl in found_all:
        if tmpl.id.lower() == search or tmpl.name.lower() == search:
            return filepath, tmpl

    # Partial match: search string appears in ID or name
    partial_matches = [
        (filepath, tmpl)
        for filepath, _source, tmpl in found_all
        if search in tmpl.id.lower() or search in tmpl.name.lower()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]

    # Not found — suggest similar
    candidates = [
        f"{tmpl.name} (id: {tmpl.id})"
        for _, _, tmpl in found_all
        if search in tmpl.id.lower() or search in tmpl.name.lower()
    ]
    click.echo(f"✗ Template '{name_or_path}' not found.", err=True)
    if candidates:
        click.echo("\nDid you mean one of these?", err=True)
        for c in candidates:
            click.echo(f"  • {c}", err=True)
    else:
        click.echo(
            "\nNo similar templates found. Run 'orch templates list' to see all.",
            err=True,
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
def start_wizard(
    ctx: click.Context,
    template_name_or_path: str,
    mode: str,
    api_key: Optional[str],
    yes: bool,
    output_dir: Optional[Path],
) -> None:
    """Interactive wizard to fill in a template's inputs and run it.

    TEMPLATE_NAME_OR_PATH can be a template name/ID (e.g. content-pipeline-mvp)
    or a path to a .yaml file.

    Examples:

      # Interactive wizard with defaults:
      orch start content-pipeline-mvp

      # Non-interactive (use all defaults), standalone mode:
      orch start content-pipeline-mvp --mode standalone --yes

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
        template_file=template_path,
        mode=mode,
        api_key=api_key,
        input_json=input_json_str,
        input_file=None,
        output_dir=output_dir,
        dry_run_delay=0.0,
        dry_run_failure_rate=0.0,
    )


if __name__ == '__main__':
    main()