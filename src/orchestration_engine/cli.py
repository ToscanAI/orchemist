"""Command Line Interface for the Orchestration Engine.

Provides CLI commands for task queue management: submit, status, list, cancel, etc.
Uses Click for command structure and rich formatting for output.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

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


if __name__ == '__main__':
    main()