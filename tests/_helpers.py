"""Shared test helpers consumed by the test suite.

These helpers exist because the same scaffolding (e.g. building a default
``pipeline_runs`` row dict) was duplicated across 45+ call sites. Centralising
them here means a future schema change only needs to update ONE place.

This module is intentionally NOT a fixture module — callers that need a
DB-bound version of these helpers should use the conftest fixtures
(``insert_pipeline_run``, ``db``, ``in_memory_db``, ``api_client``,
``admin_json_isolated``). Use the plain functions when you need full control
or when the conftest fixture doesn't fit (e.g. multi-DB tests).

Issues: #862 (insert_pipeline_run), #863 (db / in_memory_db), #874 (api_client
/ admin_json_isolated), #875 (pipeline_run_dict).
"""

from __future__ import annotations

from typing import Any, Dict


def pipeline_run_dict(run_id: str, **overrides: Any) -> Dict[str, Any]:
    """Return a minimum-viable ``pipeline_runs`` row dict.

    The returned dict satisfies ``Database.insert_pipeline_run()``'s required
    keys (``run_id``, ``template_path``, ``template_id``, ``input_json``,
    ``mode``, ``output_dir``) plus the commonly-stamped optional ones
    (``gateway_url``, ``status``). All defaults are explicit so any future
    column added to the table needs to be added here once.

    Args:
        run_id: Identifier for the run row.
        **overrides: Key/value pairs that supersede the defaults. Any keys
            unknown to ``insert_pipeline_run`` are silently dropped by the
            underlying method (it only consumes a known set).

    Example:
        >>> db.insert_pipeline_run(pipeline_run_dict("abc12345"))
        >>> db.insert_pipeline_run(pipeline_run_dict("xyz", status="running"))
    """
    base: Dict[str, Any] = {
        "run_id": run_id,
        "template_path": "/tmp/x.yaml",
        "template_id": "test-tpl",
        "input_json": "{}",
        "mode": "dry-run",
        "output_dir": f"/tmp/orch-{run_id}",
        "gateway_url": None,
        "status": "pending",
    }
    base.update(overrides)
    return base


def insert_pipeline_run(
    db: Any,
    *,
    run_id: str,
    status: str = "pending",
    pid: int | None = None,
    **overrides: Any,
) -> str:
    """Insert a minimal pipeline_runs row via the canonical default dict.

    Standalone counterpart to the ``insert_pipeline_run`` pytest fixture.
    Useful when a test uses a non-canonical DB fixture (e.g. a class-scoped
    DB) or needs to insert against multiple DBs in the same test.

    Args:
        db: An ``orchestration_engine.db.Database`` instance.
        run_id: Identifier for the new row.
        status: Status to seed (default ``"pending"``).
        pid: If provided, ``update_pipeline_run(pid=pid)`` is called after the
            insert. Useful for tests probing the zombie-sweep (#754) path.
        **overrides: Forwarded to :func:`pipeline_run_dict`. ``status`` is
            applied before overrides so callers may also pass ``status`` via
            overrides if they prefer.

    Returns:
        The inserted ``run_id``.
    """
    row = pipeline_run_dict(run_id, status=status, **overrides)
    db.insert_pipeline_run(row)
    if pid is not None:
        db.update_pipeline_run(run_id, pid=pid)
    return run_id
