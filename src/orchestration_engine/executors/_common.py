"""Shared scaffolding for the engine's concrete model executors (Issue #927).

This module hoists the small amount of genuinely-identical boilerplate that the
five concrete executors (Anthropic, OpenRouter, GeminiCli, ClaudeCode, OpenClaw)
duplicated: task-id resolution, start-time capture, and the single shared
``PricingTable`` instance.

Design constraints (see spec §1.1/§1.2 for #927):
- ``BaseExecutor`` subclasses the :class:`~..runner.TaskExecutor` ABC, so every
  concrete executor that subclasses ``BaseExecutor`` also satisfies
  ``isinstance(x, TaskExecutor)``.
- ``BaseExecutor`` deliberately defines **no** ``__init__`` and remains abstract
  (it implements none of ``execute`` / ``can_handle`` / ``estimate_cost``).
  Because no executor calls ``super().__init__()``, inserting ``BaseExecutor``
  into the MRO leaves every executor's init chain unchanged.
- Imports are kept light: only ``..runner`` (the ABC, which imports only
  ``.schemas``), ``..cost_tracker`` (the ``PricingTable``), ``..schemas`` (for
  the ``TaskSpec`` type hint), and stdlib. No cycle is introduced — nothing in
  ``runner.py`` / ``schemas.py`` imports any executor or this module.

What is intentionally NOT hoisted (load-bearing per-executor divergence):
prompt resolution, model/tier resolution, success ``TaskResult`` construction,
and failure ``TaskResult`` construction. See spec §1.4.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ..cost_tracker import PricingTable
from ..runner import TaskExecutor
from ..schemas import TaskSpec

__all__ = ["BaseExecutor", "_PRICING"]

# Single source of truth for token pricing (Issue #908/#916). Loaded once at
# import time from the bundled pricing.yaml; this single shared instance replaces
# the three former module-scope ``_PRICING = PricingTable()`` instances in
# anthropic_executor, openrouter_executor, and openclaw_executor. PricingTable is
# stateless after loading, so a single shared instance is functionally identical
# to three separate ones, while reading pricing.yaml only once per process.
_PRICING: PricingTable = PricingTable()


class BaseExecutor(TaskExecutor):
    """Shared scaffolding base class for all concrete executor implementations.

    Provides:
    - :meth:`_resolve_task_id` — ``task.id`` if truthy, else a fresh ``uuid4``.
    - :meth:`_capture_start_time` — ``datetime.now()``; call at ``execute()`` entry.

    Does NOT provide prompt resolution, model/tier resolution, success result
    construction, or failure result construction — these remain per-executor
    because they diverge in observable behaviour (see spec §1.4).

    Remains abstract: it implements none of ``execute`` / ``can_handle`` /
    ``estimate_cost``, and defines no ``__init__`` (so no executor needs to call
    ``super().__init__()`` and every init chain is unchanged).
    """

    @staticmethod
    def _resolve_task_id(task: TaskSpec) -> str:
        """Return ``task.id`` if set and non-empty, else a fresh UUID4 string.

        This preserves the ClaudeCode safety check (``task.id`` must be truthy),
        which is strictly more correct than the bare ``task.id`` some executors
        used: a falsy id falls back to a fresh UUID rather than propagating an
        invalid ``None``/empty value to ``TaskResult.task_id``.
        """
        tid = getattr(task, "id", None)
        return tid if tid else str(uuid4())

    @staticmethod
    def _capture_start_time() -> datetime:
        """Return the current wall-clock time. Call at ``execute()`` entry.

        Centralising this makes the ``started_at``-at-entry semantics testable
        (see the Gemini ``started_at`` fix, spec §1.6).
        """
        return datetime.now()
