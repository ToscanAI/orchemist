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
    help='Directory to write phase outputs as JSON files. Created if missing.',
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

    from .templates import TemplateEngine
    from .pipeline_runner import PipelineRunner
    from .sequencer import PhaseSequencer

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
    click.echo(f"Pipeline: {template.name!r}  ({n_phases} phase{'s' if n_phases != 1 else ''})")
    click.echo(f"Mode:     {mode}")
    click.echo()

    with runner:
        sequencer = PhaseSequencer(template, runner, config=initial_input)

        try:
            result = sequencer.execute(initial_input)
        except Exception as exc:
            click.echo(f"✗ Pipeline execution crashed: {exc}", err=True)
            sys.exit(1)

    # --- 5. Report result --------------------------------------------
    if result.get('aborted'):
        failed_phase = result.get('failed_phase', 'unknown')
        click.echo(f"✗ Pipeline aborted at phase '{failed_phase}'", err=True)
        click.echo(f"  Completed phases: {[*result['phase_outputs'].keys()]}", err=True)
        sys.exit(2)

    completed_phases = [*result['phase_outputs'].keys()]
    click.echo(f"✓ Pipeline completed  ({len(completed_phases)} phases)")
    for phase_id in completed_phases:
        safe_id = re.sub(r'[^\w\-]', '_', phase_id)
        out = result['phase_outputs'][phase_id]
        _state = out.get('state', 'unknown')
        state = _state.value if hasattr(_state, 'value') else str(_state)
        tokens = out.get('tokens_consumed', 0)
        cost = out.get('cost_usd', 0)
        cost_str = f"${float(cost):.4f}" if cost else "n/a"
        click.echo(f"  ✓ {safe_id:30s}  state={state}  tokens={tokens}  cost={cost_str}")

    # --- 6. Write outputs to disk (optional) -------------------------
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        for phase_id, phase_out in result['phase_outputs'].items():
            safe_id = re.sub(r'[^\w\-]', '_', phase_id)
            out_file = output_dir / f"{safe_id}.json"
            out_file.write_text(_json.dumps(phase_out, indent=2, default=str))
        final_file = output_dir / "_final_output.json"
        final_file.write_text(
            _json.dumps(result.get('final_output', {}), indent=2, default=str)
        )
        click.echo(f"\nOutputs written to: {output_dir}/")


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


if __name__ == '__main__':
    main()