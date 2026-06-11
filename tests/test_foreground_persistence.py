"""#979 + #980 — foreground ``orch run`` persistence (run record + per-phase costs).

The foreground ``run_template`` command is BOTH submitter and executor in one
process. These tests drive the CLI black-box via ``CliRunner`` (mirroring
``test_pipeline_runner.py``) against a tmp-isolated HOME and assert on the
``pipeline_runs`` / ``cost_tracking`` rows the new persistence seam writes.

Isolation (req 5 CONSTRAINT / #981): a module-level ``_isolate_home`` autouse
fixture redirects ``$HOME`` so ``default_db_path()`` resolves under ``tmp_path``
and never touches the developer's real ``~/.orchestration-engine/engine.db``.
"""

import logging
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main
from orchestration_engine.db import Database, parse_json_list
from orchestration_engine.executors.anthropic_executor import AnthropicExecutor
from orchestration_engine.sequencer import PhaseSequencer


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """#980/#981: foreground `orch run` now persists by default. Redirect HOME
    so default_db_path() resolves under tmp and never touches the real
    ~/.orchestration-engine."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORCH_DB_PATH", raising=False)  # #981: session env wins over HOME; clear it so HOME isolation steers default_db_path()


def _db_path(tmp_path):
    """default_db_path() under the tmp HOME set by _isolate_home."""
    return tmp_path / ".orchestration-engine" / "engine.db"


_TWO_PHASE_YAML = """\
id: fg-persist
name: Foreground Persistence Test
version: "1.0.0"
description: Two-phase template for foreground persistence tests.
author: Test Author
phases:
  - id: phase_one
    name: Phase One
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    prompt_template: |
      Write one short sentence about software testing.
  - id: phase_two
    name: Phase Two
    task_type: content
    model_tier: haiku
    thinking_level: "off"
    depends_on: [phase_one]
    prompt_template: |
      Write a second short sentence about software testing.
"""


def _write_template(tmp_path):
    tpl = tmp_path / "fg-persist.yaml"
    tpl.write_text(_TWO_PHASE_YAML)
    return tpl


def test_foreground_run_persists_run_record(tmp_path):
    """A dry-run foreground run writes one pipeline_runs row with a terminal
    status, populated current_phase/completed_phases, and a pid."""
    tpl = _write_template(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "run",
            str(tpl),
            "--mode",
            "dry-run",
            "--input",
            "{}",
            "--output-dir",
            str(tmp_path / "out"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    db = Database(_db_path(tmp_path))
    runs = db.list_pipeline_runs()
    assert len(runs) == 1, f"expected exactly one run row, got {len(runs)}"

    run = runs[0]
    # Foreground writes a plain "success"; allow the daemon's broader routed set
    # defensively (per spec §1e / test_daemon.py:686).
    assert run["status"] in {"success", "pending_review", "rejected"}
    assert run["pid"] is not None and int(run["pid"]) > 0
    assert run["mode"] == "dry-run"

    completed = parse_json_list(run["completed_phases"])
    assert completed == ["phase_one", "phase_two"]
    assert run["current_phase"] == "phase_two"


def test_foreground_dry_run_cost_rows_follow_executor_tokens(tmp_path):
    """Cost recording mirrors the daemon's VERBATIM extraction (spec §3d): a row
    is written only when token metadata is present. The DryRunExecutor emits a
    non-zero ``tokens_consumed`` (no input/output split), so each phase records
    exactly one row via the total-tokens fallback branch — billed at the
    ``default`` pricing entry (cost_usd >= 0). This is identical to what a daemon
    dry-run would persist (one-seam parity, req 4: cost metadata IS present)."""
    tpl = _write_template(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "run",
            str(tpl),
            "--mode",
            "dry-run",
            "--input",
            "{}",
            "--output-dir",
            str(tmp_path / "out"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    db = Database(_db_path(tmp_path))
    run = db.list_pipeline_runs()[0]
    rows = (
        db.get_connection()
        .execute(
            "SELECT phase_id, input_tokens, output_tokens, cost_usd "
            "FROM cost_tracking WHERE run_id = ?",
            (run["run_id"],),
        )
        .fetchall()
    )
    # One row per phase; the dry-run executor supplies only a total
    # (tokens_consumed) so it lands in output_tokens via the fallback branch.
    assert len(rows) == 2
    for row in rows:
        assert row["input_tokens"] == 0
        assert row["output_tokens"] > 0
        assert row["cost_usd"] >= 0


def test_foreground_run_records_cost_rows(tmp_path):
    """A standalone run whose phases carry token metadata yields one
    cost_tracking row per phase with the recorded tokens and a positive cost."""
    tpl = _write_template(tmp_path)

    mock_response = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }
    with patch.object(AnthropicExecutor, "_call_api", return_value=mock_response):
        result = CliRunner().invoke(
            main,
            [
                "run",
                str(tpl),
                "--mode",
                "standalone",
                "--api-key",
                "sk-ant-test",
                "--input",
                "{}",
                "--output-dir",
                str(tmp_path / "out"),
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output

    db = Database(_db_path(tmp_path))
    runs = db.list_pipeline_runs()
    assert len(runs) == 1
    run_id = runs[0]["run_id"]

    rows = (
        db.get_connection()
        .execute(
            "SELECT phase_id, input_tokens, output_tokens, cost_usd "
            "FROM cost_tracking WHERE run_id = ?",
            (run_id,),
        )
        .fetchall()
    )
    assert len(rows) == 2, f"expected one cost row per phase, got {len(rows)}"
    for row in rows:
        assert row["input_tokens"] == 20
        assert row["output_tokens"] == 10
        assert row["cost_usd"] > 0


def test_foreground_run_degrades_when_db_unopenable(tmp_path, monkeypatch, caplog):
    """An unopenable DB path (HOME points at a FILE) disables persistence: the
    run still completes, exactly one warning fires, and nothing crashes."""
    tpl = _write_template(tmp_path)

    # Override the autouse fixture's HOME with a path that is a FILE, so
    # default_db_path()'s mkdir raises NotADirectoryError inside Database().
    bad_home = tmp_path / "home_is_a_file"
    bad_home.write_text("x")
    monkeypatch.setenv("HOME", str(bad_home))

    with caplog.at_level(logging.WARNING, logger="orchestration_engine.cli"):
        result = CliRunner().invoke(
            main,
            [
                "run",
                str(tpl),
                "--mode",
                "dry-run",
                "--input",
                "{}",
                "--output-dir",
                str(tmp_path / "out"),
            ],
            catch_exceptions=False,
        )

    # Run completes un-persisted.
    assert result.exit_code == 0, result.output

    # Exactly ONE acquisition warning — no per-phase spam (db is None
    # short-circuits every subsequent write).
    persist_warnings = [r for r in caplog.records if "persistence disabled" in r.getMessage()]
    assert len(persist_warnings) == 1, [r.getMessage() for r in persist_warnings]


def test_foreground_run_keyboardinterrupt_marks_cancelled(tmp_path):
    """A KeyboardInterrupt mid-run flips the (already-inserted) row to
    'cancelled' with the SIGINT error_message and a completed_at."""
    tpl = _write_template(tmp_path)

    # The two-phase template has no transitions → run_template selects
    # PhaseSequencer (cli.py ~1612), so patching its execute hits the
    # foreground path. The INSERT happens before execute (EDIT 3.3 < 3.4),
    # so the row exists as 'running' before the interrupt arm flips it.
    with patch.object(PhaseSequencer, "execute", side_effect=KeyboardInterrupt):
        result = CliRunner().invoke(
            main,
            [
                "run",
                str(tpl),
                "--mode",
                "dry-run",
                "--input",
                "{}",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )

    # Click converts the re-raised KeyboardInterrupt into a non-zero exit.
    assert result.exit_code != 0
    assert isinstance(result.exception, (KeyboardInterrupt, SystemExit))

    db = Database(_db_path(tmp_path))
    runs = db.list_pipeline_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "cancelled"
    assert run["error_message"] == "Cancelled by user (SIGINT)"
    assert run["completed_at"] is not None
