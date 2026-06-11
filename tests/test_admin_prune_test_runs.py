"""#981: tests for ``orch admin prune-test-runs``.

Seeds a tmp DB with three pipeline_runs rows:

  1. a ``.wt`` hello row   — template_id='hello-pipeline', output_dir under '/.wt/'
     (MUST match the prune predicate '%/.wt/%' → the ONLY row that may be deleted),
  2. a real run            — template_id='some-other-template', output_dir without '.wt'
     (MUST be spared),
  3. a non-``.wt`` hello   — template_id='hello-pipeline', output_dir without '.wt'
     (the operator's real hello run → MUST be spared).

and asserts the dry-run default deletes nothing, ``--no-dry-run`` without
``--yes`` deletes nothing, and ``--no-dry-run --yes`` deletes ONLY row (1) and
audit-logs the deletion. The command is invoked with ``--db-path`` so it targets
the seeded tmp DB explicitly (self-contained; not relying on the session env).
"""

from click.testing import CliRunner

from orchestration_engine.cli import main
from orchestration_engine.db import Database
from tests._helpers import insert_pipeline_run

_WT_HELLO = (
    "wt-hello-0001",
    "hello-pipeline",
    "/home/op/ToscanWorkspace/.wt/orchemist-x/output/hello-pipeline-20260611-aaa",
)
_REAL_RUN = (
    "real-run-0002",
    "some-other-template",
    "/home/op/ToscanWorkspace/output/some-other-20260611-bbb",
)
_NONWT_HELLO = (
    "nonwt-hello-0003",
    "hello-pipeline",
    "/home/op/ToscanWorkspace/output/hello-pipeline-20260611-ccc",
)


def _seed(tmp_path):
    """Create a fresh tmp DB with the three rows; return (db_path, Database)."""
    db_path = tmp_path / "engine.db"
    db = Database(db_path)
    for run_id, template_id, output_dir in (_WT_HELLO, _REAL_RUN, _NONWT_HELLO):
        insert_pipeline_run(
            db,
            run_id=run_id,
            template_id=template_id,
            output_dir=output_dir,
        )
    return db_path, db


def _count(db, run_id):
    row = db.fetch_one("SELECT COUNT(*) AS c FROM pipeline_runs WHERE run_id = ?", (run_id,))
    return int(row["c"]) if row else 0


def test_prune_dry_run_default_reports_and_deletes_nothing(tmp_path):
    db_path, db = _seed(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["admin", "prune-test-runs", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert "1" in result.output  # exactly one .wt-hello row matches
    # Nothing deleted — all three rows still present.
    assert _count(db, _WT_HELLO[0]) == 1
    assert _count(db, _REAL_RUN[0]) == 1
    assert _count(db, _NONWT_HELLO[0]) == 1


def test_prune_no_dry_run_without_yes_deletes_nothing(tmp_path):
    db_path, db = _seed(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["admin", "prune-test-runs", "--no-dry-run", "--db-path", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    # --no-dry-run alone (no --yes) falls into the dry-run echo: deletes nothing.
    assert "dry-run" in result.output
    assert _count(db, _WT_HELLO[0]) == 1
    assert _count(db, _REAL_RUN[0]) == 1
    assert _count(db, _NONWT_HELLO[0]) == 1


def test_prune_no_dry_run_yes_deletes_only_wt_hello(tmp_path):
    db_path, db = _seed(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "admin",
            "prune-test-runs",
            "--no-dry-run",
            "--yes",
            "--db-path",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Deleted 1" in result.output
    # ONLY the .wt-hello row is gone; the real run and the non-.wt hello survive.
    assert _count(db, _WT_HELLO[0]) == 0
    assert _count(db, _REAL_RUN[0]) == 1
    assert _count(db, _NONWT_HELLO[0]) == 1


def test_prune_real_delete_appends_audit_row(tmp_path):
    db_path, db = _seed(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "admin",
            "prune-test-runs",
            "--no-dry-run",
            "--yes",
            "--db-path",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    audit = db.list_admin_audit()
    matching = [r for r in audit if r.get("action") == "prune_test_runs"]
    assert matching, f"no prune_test_runs audit row found in {audit}"
    row = matching[0]
    assert row["target"] == "pipeline_runs"
    assert row["after"] == {"deleted": 1}
