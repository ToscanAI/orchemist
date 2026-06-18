"""Operator-admin command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1004, 950d). The ``admin`` group
(``orch admin prune-test-runs``) previously lived inline in ``cli/__init__.py``;
its command body is moved here VERBATIM. The group and its command self-register
on the shared ``main`` Click group (imported from ``._root``) at import time via
their ``@main.group`` / ``@admin_group.command`` decorators, so the facade only
needs to import this module for the registration side effect.

The command resolves ``Database`` / ``default_db_path`` via *function-local lazy
imports* of ``..db`` (exactly as it did inline), so the 950b/950c ``_cli.<dep>``
call-time facade indirection is NOT needed: the test-suite constructs a real
``orchestration_engine.db.Database`` and passes ``--db-path``, or patches the
source ``Database`` class — never ``orchestration_engine.cli.Database`` for this
command.
"""

from typing import Optional

import click

from ._root import main

# ---------------------------------------------------------------------------
# admin command group (#981) — operator DB hygiene, audit-logged
# ---------------------------------------------------------------------------


@main.group("admin")
def admin_group() -> None:
    """Operator maintenance commands (DB hygiene, audit-logged)."""


@admin_group.command("prune-test-runs")
@click.option(
    "--dry-run/--no-dry-run",
    "dry_run",
    default=True,
    help="Report the count without deleting (default). Use --no-dry-run (with --yes) to delete.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Confirm deletion. Required (with --no-dry-run) to actually delete.",
)
@click.option(
    "--db-path",
    default=None,
    help="Override DB path (defaults to the engine DB).",
)
def prune_test_runs(dry_run: bool, yes: bool, db_path: Optional[str]) -> None:
    """Delete pytest-residue pipeline_runs (#981).

    Targets hello-pipeline rows written from a worktree gate run (output_dir
    contains '/.wt/'). Dry-run by default (prints the count, deletes nothing);
    pass --no-dry-run --yes to execute. NEVER auto-deletes; spares the
    operator's real non-.wt runs.
    """
    from pathlib import Path  # noqa: PLC0415

    # F811: intentionally re-imported lazily here; the module-level Database is a
    # facade re-export / patch target (see top-of-module note), not used in body.
    from ..db import Database, default_db_path  # noqa: PLC0415, F811

    where = "template_id = ? AND output_dir LIKE ?"
    params = ("hello-pipeline", "%/.wt/%")
    db = Database(Path(db_path) if db_path else default_db_path())
    row = db.fetch_one(f"SELECT COUNT(*) AS c FROM pipeline_runs WHERE {where}", params)
    n = int(row["c"]) if row else 0
    if dry_run or not yes:
        click.echo(
            f"[dry-run] {n} test-residue pipeline_runs match "
            f"(template_id='hello-pipeline' AND output_dir LIKE '%/.wt/%'). "
            f"Re-run with --no-dry-run --yes to delete."
        )
        return
    db.execute(f"DELETE FROM pipeline_runs WHERE {where}", params)
    db.append_admin_audit(
        action="prune_test_runs",
        target="pipeline_runs",
        before={"matched": n},
        after={"deleted": n},
    )
    click.echo(f"Deleted {n} test-residue pipeline_runs.")
