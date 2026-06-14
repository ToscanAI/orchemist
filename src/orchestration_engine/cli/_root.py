"""Root Click group and global queue state for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #998, 950a). Defines the ``main``
Click group (the ``orch`` entry point), the module logger, and the process-global
``TaskQueue`` accessor. Command bodies live in ``cli/__init__.py`` and decorate
themselves onto ``main`` imported from here.
"""

import logging
from pathlib import Path  # noqa: F401  (used by main's --db-path option type)
from typing import Optional

import click

from ..queue import TaskQueue

logger = logging.getLogger(__name__)

# Global queue instance (initialized per command)
queue: Optional[TaskQueue] = None


def get_queue() -> TaskQueue:
    """Get or create the global TaskQueue instance."""
    global queue
    if queue is None:
        queue = TaskQueue()
    return queue


@click.group()
@click.option("--db-path", type=click.Path(path_type=Path), help="Database file path")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
@click.version_option()
def main(db_path: Optional[Path], verbose: bool) -> None:
    """Orchestration Engine CLI - AI Agent Task Coordination."""
    global queue

    if verbose:
        import logging  # noqa: PLC0415

        logging.basicConfig(level=logging.INFO)

    # Initialize queue with custom db path if provided
    if db_path:
        from ..db import Database  # noqa: PLC0415

        queue = TaskQueue(Database(db_path))
