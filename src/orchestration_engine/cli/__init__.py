"""Command Line Interface for the Orchestration Engine.

Provides CLI commands for task queue management: submit, status, list, cancel, etc.
Uses Click for command structure and rich formatting for output.
"""

import json
import os
import re

# ``subprocess`` / ``time`` / ``datetime`` / ``timezone`` / ``Decimal`` /
# ``now_utc`` and the schema enums below are no longer referenced by the commands
# that remain inline in this module (they moved to queue_cmds / pipeline_cmds with
# their commands). They are kept here (unused-import suppressed) to preserve the
# ``orchestration_engine.cli`` public surface byte-identically AND, for
# ``subprocess`` / ``Database``, because the relocated commands resolve them as
# facade attributes (``_cli.subprocess`` / ``_cli.Database``) and existing tests
# patch them on the ``orchestration_engine.cli`` module (EPIC #942 / 950b).
import subprocess  # noqa: F401
import sys
import time  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from decimal import Decimal  # noqa: F401
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import yaml

from ..daemon import apply_config_schema_defaults
from ..db import Database, default_db_path  # noqa: F401
from ..output_utils import (  # noqa: F401
    extract_output_text as _extract_output_text,
)
from ..output_utils import (
    safe_write_phase_output as _safe_write_phase_output,
)
from ..schemas import (  # noqa: F401
    Priority,
    TaskFilters,
    TaskSpec,
    TaskState,
    TaskType,
)
from ..timestamps import now_utc  # noqa: F401

# Importing the command-group modules below registers their @main.command /
# @main.group decorators on the shared `main` Click group purely as an import
# side effect (EPIC #942 / 950b: registration-by-import). Every command remains
# reachable through ``main`` (``orch <cmd>``) and as an attribute of its
# ``queue_cmds`` / ``pipeline_cmds`` submodule; no caller imports the command
# functions by name from this facade.
#
# The explicit re-exports keep the names that ARE referenced through the facade:
#  * the test-imported privates ``_print_run_detail`` / ``_watch_pipeline_run`` /
#    ``_print_watch_event`` (see tests/test_cli_watch.py, tests/test_daemon.py +
#    the derived facade gate tests/test_facade_surface_942.py), and
#  * ``run_template`` / ``_build_default_phases`` / ``_collect_phases_interactive``,
#    still invoked by the ``quickstart`` / ``new_template`` commands that remain
#    inline in this module.
from . import pipeline_cmds, queue_cmds  # noqa: E402,F401
from ._helpers import (  # noqa: F401
    _fetch_issue_strict,
    _find_template,
    _fmt_elapsed,
    _get_persistent_db_path,
    _infer_git_context,
    _normalize_git_url,
    _read_openclaw_token,
    _resolve_template_arg,
    _scan_templates,
    _slugify_title,
    _template_resolution_paths,
    _validate_required_config,
    _yaml_str,
    format_datetime,
    format_duration,
    print_table,
)
from ._root import get_queue, logger, main, queue  # noqa: F401
from .pipeline_cmds import (  # noqa: E402,F401
    _build_default_phases,
    _collect_phases_interactive,
    _print_watch_event,
    _watch_pipeline_run,
    run_template,
)
from .queue_cmds import _print_run_detail  # noqa: E402,F401

# ---------------------------------------------------------------------------
# orch providers — read-only provider discoverability (#970, #101 epic-closer)
# ---------------------------------------------------------------------------


@main.group("providers")
def providers_group() -> None:
    """Inspect configured model providers (read-only).

    Lists each provider, the credential env var it needs, whether that var is
    currently set, default tier->model mappings, and a maturity label. Makes no
    network calls, constructs no executors, and touches no database.

    Note: .env files are NOT auto-loaded — export vars in your shell first
    (see docs/openrouter-setup.md for the manual `set -a; source .env` recipe).

    Examples:

      orch providers list            # human-readable table
      orch providers list --json     # machine-readable JSON
    """


def _tier_defaults_for(name: str) -> Dict[str, str]:
    """Return the tier->model default map for *name* (empty for non-tiered providers).

    Derived from the LIVE registries so the displayed defaults can never drift
    from what the executors actually emit: anthropic uses the canonical bare ids
    (``model_registry.bare_id``); openrouter uses ``DEFAULT_MODEL_MAP``
    (anthropic/-prefixed ids). All other providers carry no tier map.
    """
    if name == "anthropic":
        from ..model_registry import bare_id  # noqa: PLC0415

        return {tier: bare_id(tier) for tier in ("haiku", "sonnet", "opus")}
    if name == "openrouter":
        from ..executors.openrouter_executor import DEFAULT_MODEL_MAP  # noqa: PLC0415

        return dict(DEFAULT_MODEL_MAP)
    return {}


@providers_group.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def providers_list(json_output: bool) -> None:
    """List model providers, their credential env vars, status, and maturity.

    Read-only: presence of a credential is reported as a boolean only — the raw
    env-var VALUE is never printed, masked, or partially echoed.
    """
    from ..providers_info import PROVIDERS_INFO  # noqa: PLC0415

    # Presence is computed HERE, at call time, from os.environ — never stored on
    # the registry (which is import-pure). bool("") and bool(None) are both False
    # so an unset OR empty var reads as "missing" (pipeline_runner.py:255,286).
    if json_output:
        result = [
            {
                "name": p.name,
                "mode": p.mode,
                "per_phase": p.per_phase,
                "credential_env": p.credential_env,
                "configured": bool(p.credential_env and os.environ.get(p.credential_env, "")),
                "default_models": _tier_defaults_for(p.name),
                "maturity": p.maturity,
                "notes": p.notes,
            }
            for p in PROVIDERS_INFO
        ]
        click.echo(json.dumps(result, indent=2))
        return

    headers = [
        "Provider",
        "Mode",
        "Per-phase",
        "Credential env",
        "Status",
        "Default models",
        "Maturity",
        "Notes",
    ]
    rows = []
    for p in PROVIDERS_INFO:
        if p.credential_env is None:
            cred_cell = "-"
            status_cell = "n/a"
        else:
            cred_cell = p.credential_env
            configured = bool(os.environ.get(p.credential_env, ""))
            status_cell = "set" if configured else "missing"
        defaults = _tier_defaults_for(p.name)
        models_cell = (
            ", ".join(f"{tier}={mid}" for tier, mid in defaults.items()) if defaults else "-"
        )
        rows.append(  # noqa: PERF401
            [
                p.name,
                p.mode,
                "yes" if p.per_phase else "no",
                cred_cell,
                status_cell,
                models_cell,
                p.maturity,
                p.notes,
            ]
        )
    print_table(headers, rows)


# ---------------------------------------------------------------------------
# orch gate — merge gate management commands
# ---------------------------------------------------------------------------


@main.group("gate")
def gate_group() -> None:
    """Manage coding pipeline merge gates.

    After a git-enabled pipeline completes, it creates a merge gate that
    requires human approval before the feature branch is merged.

    Examples:

      orch gate list                      # show all pending gates
      orch gate approve abc12345          # approve a gate (run ID)
      orch gate reject abc12345           # reject a gate
      orch gate info abc12345             # show gate details
    """


@gate_group.command("list")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all gates including approved/rejected.",
)
def gate_list(show_all: bool) -> None:
    """List pending merge gates."""
    from ..git_integration import GitContext  # noqa: PLC0415

    gates = GitContext.list_gates()
    if not gates:
        click.echo("No merge gates found.")
        return

    if not show_all:
        gates = [g for g in gates if g.get("status") == "awaiting_approval"]
        if not gates:
            click.echo("No pending merge gates.  Use --all to see all gates.")
            return

    headers = ["Run ID", "Pipeline", "Branch", "Status", "Created"]
    rows = []
    for g in gates:
        rows.append(  # noqa: PERF401
            [
                g.get("run_id", "?")[:10],
                g.get("pipeline_id", "?")[:25],
                g.get("branch", "?")[:40],
                g.get("status", "?"),
                (g.get("created_at") or "?")[:19],
            ]
        )
    print_table(headers, rows)


@gate_group.command("approve")
@click.argument("run_id")
@click.option("--message", "-m", default=None, help="Optional approval message.")
@click.option(
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Override score gate enforcement and approve even when scoring failed. Use with caution.",
)
def gate_approve(run_id: str, message: Optional[str], force: bool) -> None:  # noqa: C901
    """Approve a merge gate (run ID from ``orch gate list``)."""
    from ..git_integration import GitContext, GitError  # noqa: PLC0415

    gate = GitContext.load_gate(run_id)
    if gate is None:
        click.echo(f"✗ No gate found for run ID '{run_id}'", err=True)
        sys.exit(1)

    if gate.get("status") not in ("awaiting_approval",):
        current = gate.get("status", "?")
        click.echo(
            f"⚠ Gate '{run_id}' is in status '{current}' — "
            f"can only approve 'awaiting_approval' gates."
        )
        if current in ("approved", "rejected"):
            sys.exit(0)
        sys.exit(1)

    # --- Score gate enforcement (Issue #289) --------------------------
    _gate_scoring = gate.get("scoring_status")
    _gate_score = gate.get("scoring_score")
    if _gate_scoring == "failed" and not force:
        _score_pct = f"{_gate_score * 100:.1f}" if _gate_score is not None else "n/a"
        click.echo("✗ Score gate FAILED — approval blocked.", err=True)
        click.echo(f"  Score: {_score_pct} / 100  (threshold: see scenario config)", err=True)
        click.echo(
            "  Pipeline scoring failed. Fix the issues and re-run, " "or use --force to override.",
            err=True,
        )
        sys.exit(1)
    elif _gate_scoring == "failed" and force:
        click.echo("⚠ Score gate FAILED — approving anyway because --force was specified.")
    elif _gate_scoring == "error":
        click.echo(
            "⚠ Scoring encountered an error for this run — proceeding without score gate enforcement."  # noqa: E501
        )
    elif _gate_scoring is None:
        click.echo("⚠ No scoring data for this run — proceeding without score gate.")
    # scoring_status == "passed" → allow silently (happy path)

    try:
        updated = GitContext.update_gate_status(
            run_id, "approved", message=message or "Approved via orch gate approve"
        )
    except GitError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)

    branch = updated.get("branch", "?")
    base = updated.get("base_branch", "main")
    click.echo(f"✓ Gate '{run_id}' approved.")
    click.echo(f"  Branch: {branch}")
    click.echo(f"  Merge into {base}:")
    click.echo(f"    git checkout {base} && git merge --no-ff {branch}")

    # Optionally create PR if template configured create_pr: true
    if updated.get("create_pr"):
        from ..git_integration import GitConfig  # noqa: PLC0415
        from ..git_integration import GitContext as _GC  # noqa: N814, PLC0415

        _cfg = GitConfig(create_pr=True)
        _tmp_ctx = _GC(config=_cfg, pipeline_id=updated.get("pipeline_id", ""), run_id=run_id)
        pr_url = _tmp_ctx.create_pr(updated)
        if pr_url:
            click.echo(f"  PR created: {pr_url}")
        else:
            click.echo("  ⚠ PR creation failed — run `gh pr create` manually or check gh CLI.")


@gate_group.command("reject")
@click.argument("run_id")
@click.option("--message", "-m", default=None, help="Optional rejection reason.")
def gate_reject(run_id: str, message: Optional[str]) -> None:
    """Reject a merge gate.  The feature branch is preserved for inspection."""
    from ..git_integration import GitContext, GitError  # noqa: PLC0415

    gate = GitContext.load_gate(run_id)
    if gate is None:
        click.echo(f"✗ No gate found for run ID '{run_id}'", err=True)
        sys.exit(1)

    try:
        updated = GitContext.update_gate_status(
            run_id, "rejected", message=message or "Rejected via orch gate reject"
        )
    except GitError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)

    branch = updated.get("branch", "?")
    click.echo(f"✓ Gate '{run_id}' rejected.")
    click.echo(f"  Branch '{branch}' preserved for inspection.")
    click.echo(f"  To delete it:  git branch -d {branch}")


@gate_group.command("info")
@click.argument("run_id")
def gate_info(run_id: str) -> None:
    """Show details about a merge gate."""
    from ..git_integration import GitContext  # noqa: PLC0415

    gate = GitContext.load_gate(run_id)
    if gate is None:
        click.echo(f"✗ No gate found for run ID '{run_id}'", err=True)
        sys.exit(1)

    status = gate.get("status", "?")
    status_emoji = {
        "awaiting_approval": "⏳",
        "approved": "✅",
        "rejected": "❌",
        "skipped": "⏭",
    }
    emoji = status_emoji.get(status, "❓")

    click.echo(f"Gate: {run_id}")
    click.echo(f"├─ Status:   {emoji} {status}")
    click.echo(f"├─ Pipeline: {gate.get('pipeline_id', '?')}")
    click.echo(f"├─ Branch:   {gate.get('branch', '?')}")
    click.echo(f"├─ Base:     {gate.get('base_branch', '?')}")
    click.echo(f"├─ Changes:  {gate.get('diff_stats', 'n/a')}")

    # Scoring info (Issue #289)
    _scoring_status = gate.get("scoring_status")
    _scoring_score = gate.get("scoring_score")
    if _scoring_status is not None:
        _score_emoji = {"passed": "✅", "failed": "❌", "error": "⚠️"}.get(_scoring_status, "❓")
        _score_pct = f"  ({_scoring_score * 100:.1f}/100)" if _scoring_score is not None else ""
        click.echo(f"├─ Scoring:  {_score_emoji} {_scoring_status}{_score_pct}")
    else:
        click.echo("├─ Scoring:  ⏳ pending (not yet scored)")

    click.echo(f"├─ Created:  {(gate.get('created_at') or '?')[:19]}")
    if gate.get("updated_at"):
        click.echo(f"├─ Updated:  {gate['updated_at'][:19]}")
    if gate.get("message"):
        click.echo(f"├─ Message:  {gate['message']}")
    if gate.get("output_dir"):
        click.echo(f"├─ Output:   {gate['output_dir']}")

    commits = gate.get("commits", [])
    if commits:
        click.echo(f"└─ Commits ({len(commits)}):")
        for c in commits:
            click.echo(f"   • {c.get('sha', '?')[:8]}  {c.get('message', '?')}")
    else:
        click.echo("└─ Commits:  none")


def _check_yaml_syntax(template_file: Path) -> Optional[str]:
    """Try raw YAML parse and return a formatted error string or None if OK."""
    try:
        with open(template_file) as fh:
            yaml.safe_load(fh)
        return None
    except yaml.YAMLError as exc:
        if hasattr(exc, "problem_mark"):
            mark = exc.problem_mark
            line = mark.line + 1
            col = mark.column + 1
            problem = exc.problem or "syntax error"
            return f"YAML syntax error at line {line}:{col} — {problem}"
        return f"YAML syntax error — {exc}"


def _apply_fixes(template_file: Path, raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply auto-corrections to *raw_data* in-place and rewrite the file.

    Corrections applied:
    - Add missing ``version`` (default ``"1.0.0"``)
    - Add missing ``description`` (default ``""``)
    - Normalize ``model_tier`` to lowercase for every phase

    Returns the modified ``raw_data`` dict.
    """
    changed = False

    if "version" not in raw_data or raw_data["version"] is None:
        raw_data["version"] = "1.0.0"
        changed = True

    if "description" not in raw_data or raw_data["description"] is None:
        raw_data["description"] = ""
        changed = True

    for phase in raw_data.get("phases") or []:
        tier = phase.get("model_tier")
        if tier and isinstance(tier, str):
            normalised = tier.lower()
            if normalised != tier:
                phase["model_tier"] = normalised
                changed = True

    if changed:
        try:
            with open(template_file, "w") as fh:
                yaml.dump(
                    raw_data, fh, default_flow_style=False, allow_unicode=True, sort_keys=False
                )
            click.echo(
                click.style("⚠", fg="yellow")
                + " Note: --fix rewrites YAML; comments may not be preserved."
            )
        except PermissionError:
            click.echo(
                click.style("✗", fg="red")
                + f" Cannot write --fix changes: permission denied on {template_file}",
                err=True,
            )

    return raw_data


@main.command("validate")
@click.argument("template_name_or_file")
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Auto-correct simple issues (missing version/description, model tier casing).",
)
def validate_template(template_name_or_file: str, fix: bool) -> None:  # noqa: C901
    """Validate a pipeline template and report any structural errors.

    TEMPLATE_NAME_OR_FILE is a template name (e.g. content-pipeline) or a
    path to a YAML file.  Template names are resolved using the search order:
    ORCH_TEMPLATES_PATH → ./templates/ → ~/.orch/templates/ → bundled.

    Exit code 0 = valid (warnings only).  Exit code 1 = errors found.
    """
    OK = click.style("✓", fg="green")  # noqa: N806
    ERR = click.style("✗", fg="red")  # noqa: N806
    WRN = click.style("⚠", fg="yellow")  # noqa: N806

    try:
        from ..templates import PipelineTemplate, TemplateEngine  # noqa: F401, PLC0415

        template_file = _resolve_template_arg(template_name_or_file)

        # ── 1. YAML syntax check ──────────────────────────────────────
        yaml_error = _check_yaml_syntax(template_file)
        if yaml_error:
            click.echo(f"{ERR} YAML syntax:  {yaml_error}", err=True)
            sys.exit(1)
        click.echo(f"{OK} YAML syntax")

        # ── 2. Load raw data (for --fix and extended checks) ──────────
        with open(template_file) as fh:
            raw_data: Dict[str, Any] = yaml.safe_load(fh)

        # ── 3. Apply fixes before structural validation ───────────────
        if fix:
            raw_data = _apply_fixes(template_file, raw_data)
            click.echo(f"{OK} --fix applied (version, description, model tier casing)")

        # ── 3b. Validate top-level adversary_config (if present) ─────
        if isinstance(raw_data, dict) and "adversary_config" in raw_data:
            from ..templates import _parse_adversary_config  # noqa: PLC0415

            try:
                _parse_adversary_config(raw_data["adversary_config"])
            except ValueError as exc:
                click.echo(f"{ERR} Invalid adversary_config: {exc}")
                sys.exit(1)
            click.echo(f"{OK} adversary_config valid")
            sys.exit(0)

        # ── 4. Structural validation via engine ───────────────────────
        engine = TemplateEngine()
        try:
            template: PipelineTemplate = engine.load_template(template_file)
        except ValueError as exc:
            click.echo(f"{ERR} {exc}")
            sys.exit(1)
        structural_errors = engine.validate_template(template)

        if structural_errors:
            click.echo(f"{ERR} Structural checks ({len(structural_errors)} error(s)):")
            for err in structural_errors:
                click.echo(f"    • {err}")
        else:
            click.echo(f"{OK} Structural checks  ({len(template.phases)} phases, deps OK)")

        # ── 5. Extended / linting checks ─────────────────────────────
        ext_errors, ext_warnings = engine.validate_template_extended(template, raw_data)

        if ext_errors:
            click.echo(f"{ERR} Extended checks ({len(ext_errors)} error(s)):")
            for err in ext_errors:
                click.echo(f"    • {err}")
        elif ext_warnings:
            click.echo(f"{WRN} Extended checks ({len(ext_warnings)} warning(s)):")
            for w in ext_warnings:
                click.echo(f"    • {w}")
        else:
            click.echo(
                f"{OK} Extended checks  (model tiers, thinking levels, variable refs, config_schema)"  # noqa: E501
            )

        # ── 6. Summary ────────────────────────────────────────────────
        total_errors = len(structural_errors) + len(ext_errors)
        total_warnings = len(ext_warnings)

        if total_errors:
            click.echo(
                f"\n{ERR} Template {str(template_file)!r}: "
                f"{total_errors} error(s), {total_warnings} warning(s)"
            )
            sys.exit(1)
        elif total_warnings:
            click.echo(
                f"\n{WRN} Template {str(template_file)!r}: "
                f"valid with {total_warnings} warning(s)"
            )
        else:
            click.echo(f"\n{OK} Template {str(template_file)!r} is valid")

    except (KeyError, ValueError) as exc:
        click.echo(f"{ERR} Invalid template: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command("list-phases")
@click.argument("template_name_or_file")
def list_phases(template_name_or_file: str) -> None:
    """Show execution order and model tiers for a pipeline template.

    TEMPLATE_NAME_OR_FILE is a template name (e.g. content-pipeline) or a
    path to a YAML file.  Template names are resolved using the search order:
    ORCH_TEMPLATES_PATH → ./templates/ → ~/.orch/templates/ → bundled.
    """
    try:
        from ..templates import PipelineTemplate, TemplateEngine  # noqa: F401, PLC0415

        template_file = _resolve_template_arg(template_name_or_file)
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

    except Exception as exc:  # noqa: BLE001
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Template discovery helpers
# ---------------------------------------------------------------------------


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
    """List available pipeline templates from all resolution paths.

    Templates are discovered in the following order (first match wins when
    names collide):

    \b
    1. Paths in ORCH_TEMPLATES_PATH (colon-separated env var) — labelled "custom"
    2. ./templates/                  (project-local)           — labelled "project"
    3. ~/.orch/templates/            (user-global)             — labelled "user"
    4. <package>/../../templates/    (bundled with the engine) — labelled "bundled"

    The Source column shows where each template was found.
    """
    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    console = Console(highlight=False)
    found = _scan_templates()

    if json_output:
        result = []
        for filepath, source, tmpl in found:
            result.append(
                {
                    "id": tmpl.id,
                    "name": tmpl.name,
                    "version": tmpl.version,
                    "phases": len(tmpl.phases),
                    "description": tmpl.description,
                    "source": source,
                    "path": str(filepath),
                }
            )
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
def templates_info(name_or_path: str) -> None:  # noqa: C901
    """Show detailed info about a template (by name, ID, or file path)."""
    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    from ..templates import TemplateEngine  # noqa: PLC0415

    console = Console(highlight=False)
    engine = TemplateEngine()

    # Reuse shared template resolution logic
    template_path, template = _find_template(name_or_path)

    # ---- Header ----
    console.print(f"\n[bold cyan]{template.name}[/bold cyan] " f"[dim](v{template.version})[/dim]")
    if template.description:
        console.print(template.description)
    console.print()

    # ---- Documentation fields (#78) ----
    doc_lines = []
    if template.author:
        doc_lines.append(f"[bold]Author:[/bold]   {template.author}")
    if template.category:
        doc_lines.append(f"[bold]Category:[/bold] {template.category}")
    if template.tags:
        doc_lines.append(f"[bold]Tags:[/bold]     {', '.join(template.tags)}")
    if template.use_cases:
        doc_lines.append("[bold]Use Cases:[/bold]")
        for uc in template.use_cases:
            doc_lines.append(f"  • {uc}")  # noqa: PERF401
    if template.example_input:
        doc_lines.append(f"[bold]Example Input:[/bold] {json.dumps(template.example_input)}")
    if doc_lines:
        for line in doc_lines:
            console.print(line)
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
        console.print(f"  orch run {template_path} --mode dry-run --input '{input_str}'")
        console.print()


# ---------------------------------------------------------------------------
# Feature #69 — orch templates install / uninstall
# ---------------------------------------------------------------------------

_USER_TEMPLATES_DIR = Path.home() / ".orch" / "templates"


_GH_SHORTHAND_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


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
    # F811: intentionally re-imported lazily; the module-level subprocess is a
    # facade re-export / patch target (see top-of-module note), not used in body.
    import subprocess  # noqa: PLC0415, F811

    if url.startswith("-"):
        raise click.ClickException(f"Invalid URL: {url}")

    dest = _USER_TEMPLATES_DIR / re.sub(r"[^\w\-]", "_", name)

    if dest.exists():
        if not force:
            raise click.ClickException(
                f"Template '{name}' already installed at {dest}.\n" f"  Use --force to overwrite."
            )
        import shutil  # noqa: PLC0415

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
    from ..templates import TemplateEngine  # noqa: PLC0415

    engine = TemplateEngine()
    try:
        template = engine.load_template(yaml_path)
    except Exception as exc:  # noqa: BLE001
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
    import shutil  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

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
        install_name = name or source.rsplit("/", maxsplit=1)[-1]
        console.print(f"[bold]Installing from GitHub:[/bold] {source}")
        dest = _install_from_git(url, install_name, force)

    elif is_local:
        # Local YAML file — copy to ~/.orch/templates/
        local_path = Path(source)
        if not local_path.exists():
            raise click.ClickException(f"File not found: {source}")

        install_name = name or local_path.stem
        safe_name = re.sub(r"[^\w\-]", "_", install_name)
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
        console.print(f"  [cyan]orch start {install_name}[/cyan]" f"          Run it interactively")
    console.print()


@templates.command("uninstall")
@click.argument("name")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation prompt.")
def templates_uninstall(name: str, force: bool) -> None:
    """Remove an installed template from ~/.orch/templates/.

    NAME is the template directory name (as shown in `orch templates list`).
    """
    import shutil  # noqa: PLC0415

    safe_name = re.sub(r"[^\w\-]", "_", name)
    dest = _USER_TEMPLATES_DIR / safe_name

    if not dest.exists():
        raise click.ClickException(f"Template '{name}' not found in {_USER_TEMPLATES_DIR}")

    if not force:
        if not click.confirm(f"Remove template '{name}' from {dest}?"):
            click.echo("Aborted.")
            return

    shutil.rmtree(dest)
    click.echo(f"✓ Template '{name}' uninstalled.")


# ---------------------------------------------------------------------------
# Feature #76 — orch templates search <query>
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATE_INDEX_URL = (
    "https://raw.githubusercontent.com/ToscanAI/orchestration-engine/main/"
    "community-templates/index.yaml"
)
_TEMPLATE_INDEX_CACHE = Path.home() / ".orch" / "cache" / "template-index.yaml"


@templates.command("search")
@click.argument("query", default="", required=False)
@click.option(
    "--refresh",
    is_flag=True,
    default=False,
    help="Force re-fetch of the remote index (ignore cache).",
)
@click.option(
    "--index-url",
    default=None,
    help="Override the default community index URL.",
)
def templates_search(query: str, refresh: bool, index_url: Optional[str]) -> None:
    """Search the community template index.

    QUERY is an optional search term (name, description, tags, category).
    Omit to list all available community templates.

    \b
    Examples:
      orch templates search content
      orch templates search --refresh
      orch templates search code-review --index-url https://example.com/index.yaml
    """
    from ..template_index import TemplateIndex  # noqa: PLC0415

    index = TemplateIndex()
    url = index_url or DEFAULT_TEMPLATE_INDEX_URL

    # ── 1. Resolve index data ──────────────────────────────────────────
    loaded = False

    if not refresh and TemplateIndex.is_cache_fresh(_TEMPLATE_INDEX_CACHE):
        try:
            index.load_local(_TEMPLATE_INDEX_CACHE)
            loaded = True
        except Exception:  # noqa: BLE001
            pass  # Fall through to remote fetch

    if not loaded:
        try:
            click.echo(f"Fetching index from {url} …", err=True)
            index.load_remote(url)
            try:
                index.save_cache(_TEMPLATE_INDEX_CACHE)
            except Exception:  # noqa: BLE001
                pass  # Cache save failure is non-fatal
        except Exception as exc:  # noqa: BLE001
            # If remote fails but we have a stale cache, use it
            if _TEMPLATE_INDEX_CACHE.exists():
                click.echo(
                    f"⚠  Remote fetch failed ({exc}); using stale cache.",
                    err=True,
                )
                index.load_local(_TEMPLATE_INDEX_CACHE)
            else:
                click.echo(
                    f"✗ Could not load template index: {exc}",
                    err=True,
                )
                raise SystemExit(1)

    # ── 2. Search ──────────────────────────────────────────────────────
    results = index.search(query)

    # ── 3. Display ─────────────────────────────────────────────────────
    if not results:
        click.echo(f"No templates found matching {query!r}.")
        return

    label = f"({len(results)} result{'s' if len(results) != 1 else ''})"
    if query:
        click.echo(f"Results for {query!r} {label}:\n")
    else:
        click.echo(f"Community templates {label}:\n")

    click.echo(index.format_results(results))


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
    from rich.console import Console  # noqa: PLC0415

    console = Console(highlight=False)

    # Locate hello-pipeline.yaml — try multiple locations for both
    # repo-based development and pip-installed packages.
    _pkg_dir = Path(__file__).parent  # src/orchestration_engine/
    _repo_root = _pkg_dir.parent.parent  # repo root (when running from source)
    candidates = [
        _repo_root / "examples" / "hello-pipeline.yaml",
        Path("./examples/hello-pipeline.yaml"),
        _pkg_dir / "examples" / "hello-pipeline.yaml",  # package data
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
        template_name_or_file=hello_yaml,
        mode="dry-run",
        api_key=None,
        input_json=None,
        input_file=None,
        output_dir=None,
        dry_run_delay=0.0,
        dry_run_failure_rate=0.0,
    )

    # ---- Footer ----
    from ..templates import TemplateEngine as _TE  # noqa: N814, PLC0415

    _tmpl = _TE().load_template(hello_yaml)
    n_phases = len(_tmpl.phases)

    console.print()
    console.print(
        f"[bold green]✓ That's it![/bold green] " f"You just ran a {n_phases}-phase AI pipeline."
    )
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(
        "  [cyan]orch templates list[/cyan]"
        "                              See all available templates"
    )
    console.print(
        "  [cyan]orch templates info hello-pipeline[/cyan]" "        Explore a simple pipeline"
    )
    console.print(
        "  [cyan]orch run hello-pipeline.yaml --mode dry-run[/cyan]"
        "  Try a test run (no API key needed)"
    )
    console.print()


# ---------------------------------------------------------------------------
# Issue #110 — orch templates test
# ---------------------------------------------------------------------------


@templates.command("test")
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Show full error output per template on failure.",
)
@click.option(
    "--fail-fast",
    "-x",
    is_flag=True,
    default=False,
    help="Stop after the first template failure.",
)
def templates_test(verbose: bool, fail_fast: bool) -> None:  # noqa: C901
    """Validate and dry-run every discovered template.

    Discovers all ``.yaml`` / ``.yml`` files in ``templates/`` and
    ``examples/`` (same glob pattern used by the test suite) then runs
    two checks on each:

    \b
    1. Structural + extended validation (equivalent to ``orch validate``)
    2. Dry-run execution (equivalent to ``orch run --mode dry-run``)

    Exits 0 when all templates pass; exits 1 on the first failure
    (if ``--fail-fast``) or after all templates have been checked.

    Examples:

      orch templates test
      orch templates test --verbose
      orch templates test --fail-fast
    """
    import glob as _glob  # noqa: PLC0415
    import traceback as _tb  # noqa: PLC0415

    import yaml as _yaml  # noqa: PLC0415

    from ..pipeline_runner import PipelineRunner  # noqa: PLC0415
    from ..sequencer import PhaseSequencer, StateMachineSequencer  # noqa: PLC0415
    from ..templates import TemplateEngine  # noqa: PLC0415

    OK_MARK = click.style("✓", fg="green")  # noqa: N806
    FAIL_MARK = click.style("✗", fg="red")  # noqa: N806

    # ── 1. Discover templates (same glob as test suite) ──────────────────
    repo_root = Path(__file__).parent.parent.parent.parent
    # Heuristic: walk up until we find a templates/ directory
    _candidate = Path(__file__).resolve()
    for _ in range(6):
        _candidate = _candidate.parent
        if (_candidate / "templates").exists() and (_candidate / "examples").exists():
            repo_root = _candidate
            break

    all_templates: List[str] = sorted(
        _glob.glob(str(repo_root / "templates" / "*.yaml"))
        + _glob.glob(str(repo_root / "templates" / "*.yml"))
        + _glob.glob(str(repo_root / "examples" / "*.yaml"))
        + _glob.glob(str(repo_root / "examples" / "*.yml"))
    )

    if not all_templates:
        click.echo(
            f"{FAIL_MARK} No templates discovered under {repo_root}/ "
            "(looked in templates/ and examples/)",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Discovered {len(all_templates)} template(s) under {repo_root}/\n")

    engine = TemplateEngine()
    passed: List[str] = []
    failed: List[str] = []

    for template_path_str in all_templates:
        template_path = Path(template_path_str)
        template_name = template_path.name
        errors: List[str] = []

        # ── 2a. Validate ──────────────────────────────────────────────────
        try:
            template = engine.load_template(template_path)
            structural_errors = engine.validate_template(template)
            if structural_errors:
                errors.extend([f"[structural] {e}" for e in structural_errors])

            raw_data: Dict[str, Any] = _yaml.safe_load(template_path.read_text())
            ext_errors, _ext_warnings = engine.validate_template_extended(template, raw_data)
            if ext_errors:
                errors.extend([f"[extended] {e}" for e in ext_errors])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[load/validate] {exc}" + (f"\n{_tb.format_exc()}" if verbose else ""))

        # ── 2b. Dry-run ───────────────────────────────────────────────────
        if not errors:
            try:
                input_data: Dict[str, Any] = (
                    template.example_input if template.example_input else {}
                )
                dry_runner = PipelineRunner.dry_run(
                    delay_seconds=0.0,
                    failure_rate=0.0,
                )
                with dry_runner:
                    _has_transitions = any(p.transitions for p in template.phases) or bool(
                        template.default_transitions
                    )
                    _SequencerClass = (  # noqa: N806
                        StateMachineSequencer if _has_transitions else PhaseSequencer
                    )
                    sequencer = _SequencerClass(template, dry_runner, config=input_data)
                    result = sequencer.execute(input_data)

                if result.get("aborted"):
                    failed_phase = result.get("failed_phase", "unknown")
                    errors.append(f"[dry-run] pipeline aborted at phase '{failed_phase}'")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"[dry-run] {exc}" + (f"\n{_tb.format_exc()}" if verbose else ""))

        # ── 3. Report ─────────────────────────────────────────────────────
        if errors:
            failed.append(template_name)
            click.echo(f"  {FAIL_MARK} {template_name}")
            if verbose:
                for err in errors:
                    for line in err.splitlines():
                        click.echo(f"       {line}", err=True)
        else:
            passed.append(template_name)
            click.echo(f"  {OK_MARK} {template_name}")

        if errors and fail_fast:
            click.echo(
                f"\n{FAIL_MARK} Stopped after first failure (--fail-fast).",
                err=True,
            )
            sys.exit(1)

    # ── 4. Summary ────────────────────────────────────────────────────────
    click.echo()
    total = len(passed) + len(failed)
    if failed:
        click.echo(
            f"{FAIL_MARK} {len(failed)}/{total} template(s) failed: " + ", ".join(failed),
            err=True,
        )
        sys.exit(1)
    else:
        click.echo(f"{OK_MARK} All {total} template(s) passed.")


# ---------------------------------------------------------------------------
# Feature #73 — orch new (scaffold a new pipeline template)
# ---------------------------------------------------------------------------


def _build_scaffold_yaml(data: Dict[str, Any]) -> str:
    """Serialise *data* to a commented YAML string.

    Uses ``yaml.dump()`` for individual sections and manually prepends
    ``# comment`` lines before each major block, since PyYAML does not
    support comment generation natively.
    """

    def _dump(obj: Any) -> str:
        return yaml.dump(obj, default_flow_style=False, allow_unicode=True, sort_keys=False)

    lines: List[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines += [
        f"# Pipeline: {data['id']}",
        "# Generated by `orch new` — edit to customize",
        "# Run `orch validate <this-file>` to check validity",
        "",
    ]

    # ── Top-level metadata fields ────────────────────────────────────────────
    top_meta: Dict[str, Any] = {
        k: v for k, v in data.items() if k not in ("config_schema", "phases")
    }
    lines.append(_dump(top_meta).rstrip())
    lines.append("")

    # ── config_schema ────────────────────────────────────────────────────────
    lines += [
        "# config_schema: defines the inputs your pipeline accepts at runtime.",
        "# Add fields under 'properties'; list required field names under 'required'.",
    ]
    lines.append(_dump({"config_schema": data["config_schema"]}).rstrip())
    lines.append("")

    # ── phases ───────────────────────────────────────────────────────────────
    lines += [
        "# phases: the ordered list of pipeline steps.",
        "# A phase runs only after all its depends_on phases have completed.",
        "phases:",
    ]

    for phase in data["phases"]:
        lines.append("")
        lines.append(f"  # ── {phase['id']} " + "─" * max(4, 60 - len(phase["id"])))
        # yaml.dump renders a one-element list; strip trailing newline then indent
        phase_block = _dump([phase]).rstrip()
        indented = "\n".join("  " + row for row in phase_block.splitlines())
        lines.append(indented)

    lines.append("")
    return "\n".join(lines)


@main.command("new")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Non-interactive: generate a template with sensible defaults "
    "(name=my-pipeline, 2 phases, sonnet/low).",
)
@click.option(
    "--from",
    "from_template",
    default=None,
    metavar="TEMPLATE",
    help="Clone an existing template as the starting point. "
    "Accepts a template name, ID, or file path.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file path. Defaults to ./templates/<name>.yaml.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Overwrite the output file if it already exists.",
)
@click.option(
    "--phases",
    "num_phases",
    type=int,
    default=None,
    metavar="N",
    help="Number of phases (primarily used with --yes; default 2).",
)
def new_template(  # noqa: C901
    yes: bool,
    from_template: Optional[str],
    output_path: Optional[Path],
    force: bool,
    num_phases: Optional[int],
) -> None:
    """Scaffold a new pipeline template interactively.

    Walks you through naming the pipeline, adding phases, choosing model tiers
    and thinking levels, and wiring up phase dependencies.  The generated YAML
    file is ready to run with ``orch run`` and passes ``orch validate``.

    \b
    Examples:

      # Fully interactive wizard:
      orch new

      # Non-interactive with sensible defaults:
      orch new --yes

      # Clone an existing template as a starting point:
      orch new --from hello-pipeline

      # Custom output path:
      orch new --yes --output ./my-templates/awesome.yaml
    """

    # ── 0. Validate --phases early (even before prompts) ────────────────────
    if num_phases is not None and num_phases <= 0:
        click.echo("✗ Number of phases must be at least 1.", err=True)
        sys.exit(1)

    # ── 1. Load base template when --from is provided ────────────────────────
    base_data: Optional[Dict[str, Any]] = None
    if from_template:
        from_path, _ = _find_template(from_template)
        with open(from_path) as fh:
            base_data = yaml.safe_load(fh)
        if not yes:
            click.echo(click.style("✓", fg="green") + f" Cloning from: {from_path}")
            click.echo()

    # ── 2. Collect template metadata ─────────────────────────────────────────
    if yes:
        raw_name: str = (base_data or {}).get("name", "my-pipeline")
        description: str = (base_data or {}).get("description", "") or "My pipeline description"
        author: str = (base_data or {}).get("author", "") or "Unknown"
    else:
        click.echo("── Template Metadata " + "─" * 50)
        default_name = (base_data or {}).get("name", "my-pipeline")
        raw_name = click.prompt("  Template name", default=default_name)
        description = click.prompt(
            "  Description",
            default=(base_data or {}).get("description", "") or "",
            show_default=False,
        )
        author = click.prompt(
            "  Author",
            default=(base_data or {}).get("author", "") or "",
            show_default=False,
        )
        click.echo()

    template_id = re.sub(r"[^a-z0-9]+", "-", raw_name.lower()).strip("-") or "my-pipeline"

    # ── 3. Determine & validate output path ──────────────────────────────────
    if output_path is None:
        output_path = Path("templates") / f"{template_id}.yaml"

    if output_path.exists() and not force and not yes:
        click.echo(
            f"✗ Output file already exists: {output_path}\n" f"  Use --force to overwrite.",
            err=True,
        )
        sys.exit(1)

    # ── 4. Collect phases ─────────────────────────────────────────────────────
    base_phases: List[Dict[str, Any]] = (base_data or {}).get("phases") or []

    if yes:
        # --yes mode: use --phases N, base template count, or default 2
        n = num_phases if num_phases is not None else (len(base_phases) if base_phases else 2)
        if base_phases:
            # Clone phases from the base template (up to n)
            phases_data = base_phases[:n]
            # Pad with defaults if --phases exceeds source template count
            if n > len(base_phases):
                phases_data += _build_default_phases(n - len(base_phases))[: n - len(base_phases)]
        else:
            phases_data = _build_default_phases(n)
    else:
        # Interactive
        click.echo("── Phases " + "─" * 62)
        default_n = (
            num_phases if num_phases is not None else (len(base_phases) if base_phases else 2)
        )
        n = click.prompt("  Number of phases", default=default_n, type=int)
        if n <= 0:
            click.echo("✗ Number of phases must be at least 1.", err=True)
            sys.exit(1)
        phases_data = _collect_phases_interactive(n, base_phases)

    # ── 5. Build config_schema ────────────────────────────────────────────────
    if base_data and base_data.get("config_schema"):
        config_schema = base_data["config_schema"]
    else:
        config_schema = {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Main topic or input for the pipeline",
                }
            },
            "required": ["topic"],
        }

    # ── 6. Assemble template dict ─────────────────────────────────────────────
    version = (base_data or {}).get("version", "1.0.0") or "1.0.0"
    template_dict: Dict[str, Any] = {
        "id": template_id,
        "name": raw_name,
        "version": version,
        "description": description,
        "author": author,
        "config_schema": config_schema,
        "phases": phases_data,
    }

    # ── 7. Render YAML with comments ─────────────────────────────────────────
    yaml_content = _build_scaffold_yaml(template_dict)

    # ── 8. Write to disk ──────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_content, encoding="utf-8")

    click.echo(click.style("✓", fg="green") + f" Template written to: {output_path}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  orch validate {output_path}          # Check for errors")
    click.echo(f"  orch run {output_path} --mode dry-run  # Test it")


# ---------------------------------------------------------------------------
# orch import — import external resources into PipelineTemplate YAML
# ---------------------------------------------------------------------------


@main.group("import")
def import_group() -> None:
    """Import external resources and convert them to PipelineTemplate YAML."""


@import_group.command("plugin-command")
@click.argument(
    "command_file",
    type=click.Path(exists=True, path_type=Path),
    metavar="COMMAND_FILE",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Path to write the generated YAML template.  "
        "Defaults to <command-id>.yaml in the current directory."
    ),
)
@click.option(
    "--author",
    default=None,
    help="Author string for the generated template (default: 'orch import plugin-command').",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print the generated YAML to stdout without writing a file.",
)
@click.option(
    "--validate",
    "run_validate",
    is_flag=True,
    default=False,
    help="Run orch validate on the generated file after writing.",
)
def import_plugin_command(  # noqa: C901
    command_file: Path,
    output: Optional[Path],
    author: Optional[str],
    dry_run: bool,
    run_validate: bool,
) -> None:
    """Convert a knowledge-work-plugin command file to a PipelineTemplate YAML.

    COMMAND_FILE is the path to a Markdown plugin command file (with optional
    YAML frontmatter).  The importer:

    \b
    1. Parses the frontmatter for template metadata.
    2. Maps every non-meta H2 section to a pipeline phase (sonnet tier).
    3. Auto-inserts a review phase (opus tier) after each content phase.
    4. Derives config_schema from the ## Inputs section.
    5. Collects skill file references into skill_refs.

    The generated YAML is written to --output (default: <id>.yaml).
    Use --dry-run to preview without writing.  Use --validate to immediately
    check the result with orch validate.

    Examples:

    \b
      orch import plugin-command campaign-plan.md
      orch import plugin-command draft-content.md --output my-draft.yaml
      orch import plugin-command brand-review.md --dry-run
      orch import plugin-command campaign-plan.md --validate
    """
    from ..importers.plugin_command import (  # noqa: PLC0415
        GENERATED_AUTHOR,
    )
    from ..importers.plugin_command import (  # noqa: PLC0415
        import_plugin_command as _do_import,
    )

    OK = click.style("✓", fg="green")  # noqa: N806
    ERR = click.style("✗", fg="red")  # noqa: N806

    # ── 1. Parse and generate YAML ────────────────────────────────────────────
    try:
        yaml_text = _do_import(
            command_file,
            author=author or GENERATED_AUTHOR,
        )
    except ValueError as exc:
        click.echo(f"{ERR} Failed to parse plugin command: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"{ERR} Unexpected error: {exc}", err=True)
        sys.exit(1)

    # ── 2. Dry-run: print and exit ────────────────────────────────────────────
    if dry_run:
        click.echo(yaml_text)
        return

    # ── 3. Determine output path ──────────────────────────────────────────────
    if output is None:
        # Derive stem from the generated YAML's id field.
        # Strip the leading comment header (lines beginning with "#") before
        # parsing so yaml.safe_load receives clean YAML.  The previous
        # approach (lstrip + concatenate) was fragile and produced invalid
        # duplicate-key YAML on some edge-case inputs.
        try:
            data_lines = [line for line in yaml_text.splitlines() if not line.startswith("#")]
            first_pass = yaml.safe_load("\n".join(data_lines))
            template_id = (
                first_pass.get("id", command_file.stem)
                if isinstance(first_pass, dict)
                else command_file.stem
            )
        except Exception:  # noqa: BLE001
            template_id = command_file.stem
        output = Path(f"{template_id}.yaml")

    # ── 4. Write to disk ──────────────────────────────────────────────────────
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml_text, encoding="utf-8")
    except OSError as exc:
        click.echo(f"{ERR} Could not write output file: {exc}", err=True)
        sys.exit(1)

    click.echo(f"{OK} Generated template: {output}")

    # ── 5. Optional: run orch validate on the result ─────────────────────────
    if run_validate:
        # Call the validation logic directly rather than via CliRunner.
        # CliRunner is a *testing* utility: it intercepts stdout/stderr, creates
        # its own Click context, and does not propagate env vars reliably.
        # Using it inside a real CLI invocation is architecturally fragile.
        click.echo()
        OK_v = click.style("✓", fg="green")  # noqa: N806
        ERR_v = click.style("✗", fg="red")  # noqa: N806
        WRN_v = click.style("⚠", fg="yellow")  # noqa: N806
        try:
            from ..templates import PipelineTemplate, TemplateEngine  # noqa: F401, PLC0415

            # 5a. YAML syntax
            yaml_error = _check_yaml_syntax(output)
            if yaml_error:
                click.echo(f"{ERR_v} YAML syntax:  {yaml_error}", err=True)
                sys.exit(1)
            click.echo(f"{OK_v} YAML syntax")

            # 5b. Load raw data
            with open(output) as _fh:
                _raw_data: Dict[str, Any] = yaml.safe_load(_fh)

            # 5c. Structural validation
            _engine = TemplateEngine()
            _tpl: PipelineTemplate = _engine.load_template(output)
            _structural_errors = _engine.validate_template(_tpl)

            if _structural_errors:
                click.echo(f"{ERR_v} Structural checks ({len(_structural_errors)} error(s)):")
                for _e in _structural_errors:
                    click.echo(f"    • {_e}")
            else:
                click.echo(f"{OK_v} Structural checks  ({len(_tpl.phases)} phases, deps OK)")

            # 5d. Extended / linting checks
            _ext_errors, _ext_warnings = _engine.validate_template_extended(_tpl, _raw_data)

            if _ext_errors:
                click.echo(f"{ERR_v} Extended checks ({len(_ext_errors)} error(s)):")
                for _e in _ext_errors:
                    click.echo(f"    • {_e}")
            elif _ext_warnings:
                click.echo(f"{WRN_v} Extended checks ({len(_ext_warnings)} warning(s)):")
                for _w in _ext_warnings:
                    click.echo(f"    • {_w}")
            else:
                click.echo(
                    f"{OK_v} Extended checks  "
                    "(model tiers, thinking levels, variable refs, config_schema)"
                )

            # 5e. Summary
            _total_errors = len(_structural_errors) + len(_ext_errors)
            _total_warnings = len(_ext_warnings)
            if _total_errors:
                click.echo(
                    f"\n{ERR_v} Template {str(output)!r}: "
                    f"{_total_errors} error(s), {_total_warnings} warning(s)"
                )
                sys.exit(1)
            elif _total_warnings:
                click.echo(
                    f"\n{WRN_v} Template {str(output)!r}: "
                    f"valid with {_total_warnings} warning(s)"
                )
            else:
                click.echo(f"\n{OK_v} Template {str(output)!r} is valid")

        except (KeyError, ValueError) as _exc:
            click.echo(f"{ERR_v} Invalid template: {_exc}", err=True)
            sys.exit(1)
        except Exception as _exc:  # noqa: BLE001
            click.echo(f"Error during validation: {_exc}", err=True)
            sys.exit(1)
    else:
        click.echo("\nNext steps:")
        click.echo(f"  orch validate {output}           # Check the template")
        click.echo(f"  orch run {output} --mode dry-run  # Test it")


# ---------------------------------------------------------------------------
# orch serve — local web UI server  (Feature #79)
# ---------------------------------------------------------------------------


@main.command("serve")
@click.option("--port", default=8374, show_default=True, help="Port to serve on.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to.")
@click.option("--no-open", is_flag=True, help="Do not auto-open browser.")
@click.option("--db-path", default=None, help="SQLite DB path for pipeline runs.")
@click.option("--reload", is_flag=True, help="Enable uvicorn auto-reload (dev mode).")
def serve(  # noqa: C901
    port: int, host: str, no_open: bool, db_path: Optional[str], reload: bool
) -> None:
    """Launch the unified web UI + REST API on a single port.

    Serves the Next.js static frontend and the /api/v1/ REST API together.
    No CORS, no proxy, no separate servers needed.

    Requires the optional [web] extra:

      pip install orchestration-engine[web]

    Examples:

      orch serve                    # http://127.0.0.1:8374
      orch serve --port 9000
      orch serve --no-open          # start without opening browser
      orch serve --db-path /tmp/my.db
    """
    try:
        import uvicorn  # noqa: PLC0415

        from ..web.api import create_api_app  # noqa: PLC0415
    except ImportError:
        click.echo("Web UI requires extra dependencies. Install with:", err=True)
        click.echo("  pip install orchestration-engine[web]", err=True)
        sys.exit(1)

    app = create_api_app(db_path=db_path)

    # Mount static frontend if available
    frontend_out = Path(__file__).resolve().parent.parent.parent / "frontend" / "out"
    if frontend_out.exists():
        from fastapi.responses import FileResponse  # noqa: PLC0415

        index_html = frontend_out / "index.html"

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """SPA fallback: serve static files or route-specific HTML for client-side routing.

            Next.js static export generates dynamic route pages as `_.html`
            (e.g. `templates/_.html` for `/templates/[id]`). We must serve
            the correct HTML shell so the client-side router hydrates the
            right page component.
            """
            # 1. Exact static file match (JS, CSS, images, etc.)
            static_file = frontend_out / full_path
            if static_file.is_file() and static_file.resolve().is_relative_to(
                frontend_out.resolve()
            ):
                return FileResponse(str(static_file))

            # 2. Try .html extension (e.g. /runs → runs.html)
            html_file = frontend_out / f"{full_path}.html"
            if html_file.is_file() and html_file.resolve().is_relative_to(frontend_out.resolve()):
                return FileResponse(str(html_file))

            # 3. Dynamic route: /templates/xyz/edit → templates/_/edit.html
            #    Try replacing dynamic segments with '_' (most-specific first)
            parts = full_path.strip("/").split("/")

            # 3a. Try substituting each path segment with '_' from right to left
            #     e.g. /templates/xyz/edit → templates/_/edit.html
            for i in range(len(parts) - 1, 0, -1):
                trial = [*parts]
                trial[i] = "_"
                # Try as .html
                candidate_html = frontend_out / ("/".join(trial) + ".html")
                if candidate_html.is_file() and candidate_html.resolve().is_relative_to(
                    frontend_out.resolve()
                ):
                    return FileResponse(str(candidate_html))
                # Try as directory with index.html
                candidate_index = frontend_out / "/".join(trial) / "index.html"
                if candidate_index.is_file() and candidate_index.resolve().is_relative_to(
                    frontend_out.resolve()
                ):
                    return FileResponse(str(candidate_index))

            # 3b. Walk up the path to find the nearest _.html
            for i in range(len(parts), 0, -1):
                candidate = frontend_out / "/".join(parts[:i]) / "_.html"
                if candidate.is_file() and candidate.resolve().is_relative_to(
                    frontend_out.resolve()
                ):
                    return FileResponse(str(candidate))

            # 4. Ultimate fallback: index.html (dashboard)
            return FileResponse(str(index_html))

        click.echo(f"Frontend: {frontend_out}")
    else:
        click.echo(
            "Warning: Frontend not built. Run 'cd frontend && npm run build'. "
            "API endpoints are still available at /api/v1/",
            err=True,
        )

    if not no_open:
        import threading  # noqa: PLC0415
        import webbrowser  # noqa: PLC0415

        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    click.echo("Orchestration Engine (unified)")
    click.echo(f"  UI:  http://{host}:{port}")
    click.echo(f"  API: http://{host}:{port}/api/v1/docs")
    click.echo("  Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# orch ui — Serve static Next.js frontend export (Issue #310)
# ---------------------------------------------------------------------------


@main.command("ui")
@click.option("--port", default=8080, show_default=True, help="Port to serve the frontend on.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to.")
@click.option("--no-open", is_flag=True, help="Skip auto-opening the browser.")
def ui(port: int, host: str, no_open: bool) -> None:
    """Serve the static Next.js frontend export and open the browser.

    Serves the pre-built frontend from frontend/out/ using Python's built-in
    HTTP server.  Build the frontend first if the directory is missing:

      cd frontend && npm run build

    Examples:

      orch ui                    # http://localhost:8080
      orch ui --port 9090
      orch ui --no-open          # start without opening browser
      orch ui --host 0.0.0.0    # bind to all interfaces
    """
    import http.server  # noqa: PLC0415
    import socketserver  # noqa: PLC0415
    import threading  # noqa: PLC0415
    import webbrowser  # noqa: PLC0415

    frontend_out = Path(__file__).parent.parent.parent / "frontend" / "out"

    if not frontend_out.exists():
        click.echo(
            "✗ frontend/out/ not found. Run 'cd frontend && npm run build' first.",
            err=True,
        )
        sys.exit(1)

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        """Serve from frontend/out/ and suppress request logs."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(frontend_out), **kwargs)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silence per-request output

    url = f"http://{host}:{port}"

    # socketserver.TCPServer with allow_reuse_address so re-runs don't fail
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer((host, port), _QuietHandler)

    if not no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    click.echo("✓ Orchestration Engine frontend (static)")
    click.echo(f"  Serving:  {frontend_out}")
    click.echo(f"  URL:      {url}")
    click.echo("  Press Ctrl+C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        click.echo("\n✓ Server stopped.")


# ---------------------------------------------------------------------------
# orch api-server — REST API server (Issue #257)
# ---------------------------------------------------------------------------


@main.command("api-server")
@click.option("--port", default=8375, show_default=True, help="Port to serve on.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload (dev only).")
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def api_server(port: int, host: str, reload: bool, db_path: Optional[str]) -> None:
    """Launch the REST API server for programmatic pipeline control.

    Starts a FastAPI REST API at /api/v1/ backed by the same daemon-based
    async execution infrastructure used by ``orch launch``.  Intended for
    CI/CD pipelines, OpenClaw, and other programmatic consumers.

    Requires the optional [web] extra:

      pip install orchestration-engine[web]

    Endpoints:

    \b
      GET  /api/v1/health                — health check
      GET  /api/v1/templates             — list all templates
      GET  /api/v1/templates/{name}      — template detail
      POST /api/v1/runs                  — launch a pipeline run
      GET  /api/v1/runs                  — list runs (with filtering/pagination)
      GET  /api/v1/runs/{run_id}         — run status
      GET  /api/v1/runs/{run_id}/logs    — daemon log output
      DELETE /api/v1/runs/{run_id}       — cancel a run

    Examples:

      orch api-server                    # http://127.0.0.1:8375/api/v1/
      orch api-server --port 9000
      orch api-server --reload           # dev mode with auto-reload
    """
    try:
        import uvicorn  # noqa: PLC0415

        from ..web.api import create_api_app  # noqa: PLC0415
    except ImportError:
        click.echo("REST API server requires extra dependencies. Install with:", err=True)
        click.echo("  pip install orchestration-engine[web]", err=True)
        sys.exit(1)

    effective_db_path = db_path or _get_persistent_db_path()
    app = create_api_app(db_path=effective_db_path)

    click.echo("✓ Orchestration Engine REST API server")
    click.echo(f"  Listening on http://{host}:{port}")
    click.echo(f"  Docs:      http://{host}:{port}/api/v1/docs")
    click.echo(f"  DB:        {effective_db_path}")
    click.echo("  Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# orch rubric — skill rubric generation  (AC-1)
# ---------------------------------------------------------------------------


@main.group("rubric")
def rubric() -> None:
    """Generate LLM Judge rubric YAML from skill markdown files."""


@rubric.command("generate")
@click.argument("skill_file", type=click.Path(path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output YAML file path. Defaults to <skill-name>-rubric.yaml in cwd.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Overwrite output file if it already exists.",
)
def rubric_generate(skill_file: Path, output: Optional[Path], force: bool) -> None:
    """Generate a rubric YAML file from a SKILL.md file.

    SKILL_FILE is the path to a skill markdown file (e.g. SKILL.md).

    The generated YAML contains:

    \b
    - rubric: the rubric text to pass to LLMJudgeGrader
    - criteria: machine-readable list of extracted checks
    - name / generated_from / generated_at: metadata

    Examples:

      orch rubric generate path/to/SKILL.md

      orch rubric generate path/to/SKILL.md --output my-rubric.yaml

      orch rubric generate path/to/SKILL.md --output results/rubric.yaml --force
    """
    from ..rubric_generator import generate_rubric_file  # noqa: PLC0415

    try:
        out_path = generate_rubric_file(skill_file, output=output, force=force)
        click.echo(f"✓ Rubric written to: {out_path}")
    except ValueError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"✗ Unexpected error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# orch scenario — E2E autonomous scenario test runner
# ---------------------------------------------------------------------------


@main.group("scenario")
def scenario_group() -> None:
    """Run and inspect end-to-end autonomous scenario tests.

    Scenarios live in ``./scenarios/`` (by default) and combine a pipeline
    template with grading criteria.  The ``run`` sub-command executes the
    referenced template, grades the output, and prints a score report.

    Examples::

        # Dry-run (no API key needed):
        ORCH_DRY_RUN=1 orch scenario run e2e-autonomous --dry-run

        # Live run (requires ANTHROPIC_API_KEY):
        orch scenario run e2e-autonomous
    """


@scenario_group.command("run")
@click.argument("scenario_id")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Execute the pipeline in dry-run mode (no real API calls). "
        "Also sets ORCH_DRY_RUN=1 for downstream graders so LLMJudgeGrader "
        "returns its stub score instead of making API calls."
    ),
)
@click.option(
    "--scenario-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Directory to search for scenario YAML files.  "
        "Defaults to ./scenarios/ in the current working directory."
    ),
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    default=None,
    help="Anthropic API key for live (non-dry-run) mode.",
)
@click.option(
    "--mode",
    type=click.Choice(["standalone", "openclaw"]),
    default="standalone",
    show_default=True,
    help=(
        "Grader routing mode for LLM judge criteria: "
        "'standalone' uses a direct Anthropic API key (--api-key / ANTHROPIC_API_KEY), "
        "'openclaw' routes judge calls through the OpenClaw gateway subscription token "
        "(OPENCLAW_GATEWAY_URL / OPENCLAW_GATEWAY_TOKEN env vars or --gateway-url / "
        "--gateway-token options)."
    ),
)
@click.option(
    "--gateway-url",
    default=None,
    help="OpenClaw gateway URL for openclaw grader mode (or set OPENCLAW_GATEWAY_URL).",
)
@click.option(
    "--gateway-token",
    default=None,
    help="OpenClaw gateway bearer token for openclaw grader mode (or set OPENCLAW_GATEWAY_TOKEN).",
)
def scenario_run(  # noqa: C901
    scenario_id: str,
    dry_run: bool,
    scenario_dir: Optional[Path],
    api_key: Optional[str],
    mode: str,
    gateway_url: Optional[str],
    gateway_token: Optional[str],
) -> None:
    """Run an E2E scenario test and print a score report.

    SCENARIO_ID is the stem of a YAML file inside --scenario-dir, or a
    path to a YAML file directly.

    The command:

    \b
    1. Loads the scenario YAML (validates required keys).
    2. Resolves and executes the referenced pipeline template.
    3. Grades the pipeline output against all acceptance criteria.
    4. Prints a per-criterion breakdown and overall score report.

    Exit code: 0 if the scenario passes (score ≥ threshold), 1 otherwise.

    Examples::

        # Dry-run — safe for CI, no API key needed:
        ORCH_DRY_RUN=1 orch scenario run e2e-autonomous --dry-run

        # Override scenario directory:
        orch scenario run my-scenario --scenario-dir tests/scenarios/ --dry-run

        # Live run with explicit API key:
        orch scenario run e2e-autonomous --api-key sk-ant-...
    """
    import os as _os  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    from ..pipeline_runner import PipelineRunner  # noqa: PLC0415
    from ..sequencer import PhaseSequencer, StateMachineSequencer  # noqa: PLC0415
    from ..templates import TemplateEngine  # noqa: PLC0415

    # Import ScenarioRunner from the scenario_runner package.
    # Try both importable forms (installed package and source layout).
    try:
        from scenario_runner.runner import ScenarioRunner  # noqa: PLC0415
    except ImportError:
        # Fallback: add the project root to sys.path
        import sys as _sys  # noqa: PLC0415

        project_root = Path(__file__).resolve().parent.parent.parent
        _sys.path.insert(0, str(project_root))
        from scenario_runner.runner import ScenarioRunner  # noqa: PLC0415

    console = Console(highlight=False)

    # ------------------------------------------------------------------
    # 1. Resolve scenario file path
    # ------------------------------------------------------------------
    cwd = Path.cwd()
    default_scenarios_dir = cwd / "scenarios"
    base_dir = Path(scenario_dir) if scenario_dir else default_scenarios_dir

    # Accept: bare ID ("e2e-autonomous"), stem with extension, or full path
    if scenario_id.endswith(".yaml") or scenario_id.endswith(".yml"):
        candidate = Path(scenario_id)
    else:
        candidate = base_dir / f"{scenario_id}.yaml"
        if not candidate.exists():
            candidate = base_dir / f"{scenario_id}.yml"

    if not candidate.exists():
        click.echo(
            f"✗ Scenario not found: '{scenario_id}'\n" f"  Searched: {candidate}",
            err=True,
        )
        sys.exit(1)

    scenario_file = candidate.resolve()

    # ------------------------------------------------------------------
    # 2. Build LLM judge executor and create ScenarioRunner
    #
    #    In 'openclaw' mode the LLM judge is routed through the OpenClaw
    #    gateway so that scoring can use the subscription token rather than
    #    a raw Anthropic API key.  In 'standalone' mode the grader falls
    #    back to the api_key / ANTHROPIC_API_KEY path as before.
    #    In dry-run mode no executor is needed (ORCH_DRY_RUN=1 handles it).
    # ------------------------------------------------------------------
    runner_dir = scenario_file.parent

    # Resolve gateway credentials once here so that both the grader executor
    # (section 2) and the pipeline runner (section 3) can reuse the values
    # without duplicating the env-var lookup logic.
    effective_gw_url = gateway_url or _os.environ.get("OPENCLAW_GATEWAY_URL")
    effective_gw_token = gateway_token or _os.environ.get("OPENCLAW_GATEWAY_TOKEN")

    grader_executor = None
    if not dry_run and mode == "openclaw":
        try:
            from ..openclaw_executor import OpenClawExecutor  # noqa: PLC0415

            grader_executor = OpenClawExecutor(
                gateway_url=effective_gw_url,
                gateway_token=effective_gw_token,
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"⚠ Could not create OpenClawExecutor for grader: {exc}\n"
                f"  LLM judge criteria will fall back to ANTHROPIC_API_KEY.",
                err=True,
            )

    scenario_runner = ScenarioRunner(scenarios_dir=runner_dir, executor=grader_executor)

    try:
        scenario = scenario_runner.load_scenario(scenario_file)
    except (ValueError, yaml.YAMLError) as exc:
        click.echo(f"✗ Invalid scenario '{scenario_file.name}': {exc}", err=True)
        sys.exit(1)

    scenario_name = scenario.get("name", scenario["id"])
    console.print(f"\n[bold]Scenario:[/bold] {scenario_name} " f"[dim]({scenario_file.name})[/dim]")
    display_mode = "dry-run" if dry_run else mode
    console.print(f"[bold]Mode:[/bold]     {display_mode}")
    console.print()

    # ------------------------------------------------------------------
    # 3. Execute the pipeline referenced by the scenario
    # ------------------------------------------------------------------
    pipeline_ref: Optional[str] = scenario.get("pipeline")
    if not pipeline_ref:
        click.echo(
            "✗ Scenario has no 'pipeline' key — cannot execute pipeline.\n"
            "  Proceeding with empty pipeline output (all criteria will grade against {}).",
            err=True,
        )
        pipeline_output: dict = {}
    else:
        # Resolve template path: relative to scenario file first, then cwd
        template_path_candidate = scenario_file.parent / pipeline_ref
        if not template_path_candidate.exists():
            template_path_candidate = cwd / pipeline_ref
        if not template_path_candidate.exists():
            # Try resolving as a template name
            template_path_candidate = _resolve_template_arg(pipeline_ref)

        # Load + validate template
        engine = TemplateEngine()
        try:
            template = engine.load_template(template_path_candidate)
        except (FileNotFoundError, KeyError, ValueError, yaml.YAMLError) as exc:
            click.echo(f"✗ Cannot load pipeline template '{pipeline_ref}': {exc}", err=True)
            sys.exit(1)

        template_errors = engine.validate_template(template)
        if template_errors:
            click.echo(
                f"✗ Template '{pipeline_ref}' has {len(template_errors)} error(s):",
                err=True,
            )
            for err in template_errors:
                click.echo(f"  • {err}", err=True)
            sys.exit(1)

        # Build initial input from scenario
        initial_input: Dict[str, Any] = scenario.get("input", {}) or {}

        # Build PipelineRunner
        try:
            if dry_run:
                pipe_runner = PipelineRunner.dry_run(delay_seconds=0.0)
            elif mode == "openclaw":
                pipe_runner = PipelineRunner.openclaw(
                    gateway_url=effective_gw_url,
                    gateway_token=effective_gw_token,
                )
            else:
                pipe_runner = PipelineRunner.standalone(api_key=api_key)
        except ValueError as exc:
            click.echo(f"✗ {exc}", err=True)
            sys.exit(1)

        # Execute
        console.print(
            f"[bold]Pipeline:[/bold] {template.name!r}  "
            f"({len(template.phases)} phase{'s' if len(template.phases) != 1 else ''})"
        )
        console.print()

        with pipe_runner:
            _has_transitions = any(p.transitions for p in template.phases) or bool(
                template.default_transitions
            )
            _SequencerClass = (  # noqa: N806
                StateMachineSequencer if _has_transitions else PhaseSequencer
            )
            # Apply schema defaults for optional fields (#835) — same rationale
            # as run_template / pipeline_launch above. Belt-and-suspenders so
            # scenario-driven runs benefit from the same backward-compat shim.
            apply_config_schema_defaults(initial_input, getattr(template, "config_schema", None))
            sequencer = _SequencerClass(template, pipe_runner, config=initial_input)
            try:
                exec_result = sequencer.execute(initial_input)
            except Exception as exc:  # noqa: BLE001
                click.echo(f"✗ Pipeline execution failed: {exc}", err=True)
                sys.exit(1)

        if exec_result.get("aborted"):
            failed_phase = exec_result.get("failed_phase", "unknown")
            click.echo(f"✗ Pipeline aborted at phase '{failed_phase}'", err=True)
            sys.exit(2)

        # Build grading input: expose both the final phase output AND all
        # phase outputs so that criteria can inspect earlier phases.
        #
        # Schema seen by graders:
        #   {
        #     "final":  <last phase output dict>,   # most criteria use this
        #     "phases": <dict[phase_id → output]>,  # allows inspecting earlier phases
        #   }
        #
        # Backward-compatibility note: graders that call output.get("article")
        # will still work for any pipeline whose final phase emits an "article"
        # key (the "final" sub-dict is preserved verbatim).
        final_output = exec_result.get("final_output", {})
        phase_outputs = exec_result.get("phase_outputs", {})
        pipeline_output = {"final": final_output, "phases": phase_outputs}

        phase_count = len(phase_outputs)
        console.print(
            f"[green]✓[/green] Pipeline completed  "
            f"({phase_count} phase{'s' if phase_count != 1 else ''})"
        )
        console.print()

    # ------------------------------------------------------------------
    # 4. Grade the pipeline output against scenario criteria.
    #
    #    ORCH_DRY_RUN=1 is set here (not at function entry) so that it is
    #    only active during grading and is ALWAYS cleaned up afterwards —
    #    even when sys.exit() is called.  This prevents the env var from
    #    leaking into subsequent test invocations in Click's CliRunner
    #    (single-process) context.
    # ------------------------------------------------------------------
    _dry_run_env_owned = dry_run and _os.environ.get("ORCH_DRY_RUN") != "1"
    if dry_run:
        _os.environ["ORCH_DRY_RUN"] = "1"
    try:
        score_result = scenario_runner.run_scenario(scenario, pipeline_output)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"✗ Scenario grading failed: {exc}", err=True)
        sys.exit(1)
    finally:
        # Only remove the var if WE set it (don't clobber a pre-existing value).
        if _dry_run_env_owned:
            _os.environ.pop("ORCH_DRY_RUN", None)

    # ------------------------------------------------------------------
    # 5. Print score report
    # ------------------------------------------------------------------
    _print_score_report(console, score_result, scenario)

    # ------------------------------------------------------------------
    # 6. Exit with appropriate code
    # ------------------------------------------------------------------
    sys.exit(0 if score_result.passed else 1)


def _print_score_report(console, score_result, scenario: dict) -> None:
    """Print a rich score report to stdout.

    Format (AC-5):
    - Scenario ID, overall weighted score (0–100), pass/fail verdict
    - Per-criterion rows: ID, type, weight/gate, score (0–100), pass/fail
    - Gate criteria are labelled [GATE]
    - Overall summary line at the bottom
    """
    from rich.table import Table  # noqa: PLC0415

    # ── Per-criterion table ────────────────────────────────────────────
    crit_table = Table(
        title="Acceptance Criteria",
        show_header=True,
        header_style="bold cyan",
    )
    crit_table.add_column("Criterion", style="cyan", no_wrap=True)
    crit_table.add_column("Type", justify="center")
    crit_table.add_column("Weight", justify="center")
    crit_table.add_column("Score", justify="right")
    crit_table.add_column("Result", justify="center")

    for cr in score_result.criterion_results:
        weight_label = "[GATE]" if cr.is_gate else str(cr.weight)
        score_pct = f"{cr.grade.score * 100:.1f}"
        result_icon = "[green]✓ PASS[/green]" if cr.grade.passed else "[red]✗ FAIL[/red]"
        crit_table.add_row(
            cr.criterion_id,
            cr.grade.grader_type,
            weight_label,
            score_pct,
            result_icon,
        )

    console.print(crit_table)
    console.print()

    # ── Summary ────────────────────────────────────────────────────────
    overall_pct = score_result.weighted_score * 100
    threshold_pct = float(scenario.get("scoring", {}).get("pass_threshold", 0.70)) * 100
    verdict = (
        "[bold green]✓ PASS[/bold green]" if score_result.passed else "[bold red]✗ FAIL[/bold red]"
    )
    gate_status = (
        "[green]all passed[/green]"
        if score_result.gates_passed
        else "[red]one or more FAILED[/red]"
    )

    console.print(f"[bold]Scenario:[/bold]  {score_result.scenario_id}")
    console.print(
        f"[bold]Score:[/bold]     {overall_pct:.1f} / 100  " f"(threshold {threshold_pct:.0f})"
    )
    console.print(f"[bold]Gates:[/bold]     {gate_status}")
    console.print(f"[bold]Verdict:[/bold]   {verdict}")
    console.print()


# ---------------------------------------------------------------------------
# Review Queue Commands (Issue #331.4)
# ---------------------------------------------------------------------------


@main.group()
def reviews() -> None:
    """Manage the human review queue for pipeline runs."""


@reviews.command(name="list")
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum number of items.")
@click.option("--offset", type=int, default=0, show_default=True, help="Number of items to skip.")
@click.option(
    "--db-path",
    "reviews_db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the orchestration engine database.",
)
def reviews_list(limit: int, offset: int, reviews_db_path: Optional[Path]) -> None:
    """List pipeline runs pending human review."""
    from ..db import Database as _Database  # noqa: PLC0415

    _db_path = reviews_db_path or default_db_path()
    db = _Database(_db_path)
    items = db.list_pending_reviews(limit=limit, offset=offset)
    total = db.count_pending_reviews()

    if not items:
        click.echo("No runs pending review.")
        return

    click.echo(f"Pending reviews: {total} total  (showing {len(items)}  offset={offset})\n")
    headers = ["RUN ID", "TEMPLATE", "CREATED AT", "SCORE", "TIER"]
    rows = []
    for r in items:
        rows.append(  # noqa: PERF401
            [
                r.get("run_id", ""),
                r.get("template_id", ""),
                str(r.get("created_at", ""))[:19],
                (
                    f"{r.get('confidence_score', ''):.4f}"
                    if r.get("confidence_score") is not None
                    else "n/a"
                ),
                r.get("tier_name", "n/a"),
            ]
        )
    print_table(headers, rows)


@reviews.command(name="approve")
@click.argument("run_id")
@click.option("--reviewed-by", default=None, help="Reviewer identifier.")
@click.option("--note", default=None, help="Review note.")
@click.option(
    "--db-path",
    "reviews_db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the orchestration engine database.",
)
def reviews_approve(
    run_id: str, reviewed_by: Optional[str], note: Optional[str], reviews_db_path: Optional[Path]
) -> None:
    """Approve a pipeline run that is pending human review."""
    from ..db import Database as _Database  # noqa: PLC0415

    _db_path = reviews_db_path or default_db_path()
    db = _Database(_db_path)

    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"Error: run '{run_id}' not found.", err=True)
        sys.exit(1)
    if run.get("status") != "pending_review":
        click.echo(
            f"Error: run '{run_id}' is in status '{run.get('status')}', " "not 'pending_review'.",
            err=True,
        )
        sys.exit(1)

    updated = db.approve_pipeline_run(run_id, reviewed_by=reviewed_by, note=note)
    if updated:
        click.echo(f"✓ Run '{run_id}' approved (status → success).")
    else:
        click.echo(f"✗ Could not approve run '{run_id}'.", err=True)
        sys.exit(1)


@reviews.command(name="reject")
@click.argument("run_id")
@click.argument("reason")
@click.option("--reviewed-by", default=None, help="Reviewer identifier.")
@click.option(
    "--db-path",
    "reviews_db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the orchestration engine database.",
)
def reviews_reject(
    run_id: str, reason: str, reviewed_by: Optional[str], reviews_db_path: Optional[Path]
) -> None:
    """Reject a pipeline run that is pending human review.

    REASON is a short description of why the run was rejected.
    """
    from ..db import Database as _Database  # noqa: PLC0415

    _db_path = reviews_db_path or default_db_path()
    db = _Database(_db_path)

    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"Error: run '{run_id}' not found.", err=True)
        sys.exit(1)
    if run.get("status") != "pending_review":
        click.echo(
            f"Error: run '{run_id}' is in status '{run.get('status')}', " "not 'pending_review'.",
            err=True,
        )
        sys.exit(1)

    updated = db.reject_pipeline_run(run_id, reason=reason, reviewed_by=reviewed_by)
    if updated:
        click.echo(f"✓ Run '{run_id}' rejected (status → rejected).")
    else:
        click.echo(f"✗ Could not reject run '{run_id}'.", err=True)
        sys.exit(1)


@main.command("mcp")
@click.option(
    "--transport",
    default="stdio",
    help="Transport protocol: stdio or sse",
)
@click.option(
    "--port",
    default=8000,
    type=int,
    show_default=True,
    help="Port for SSE transport (default: 8000)",
)
def mcp_server(transport: str, port: int) -> None:
    """Start the MCP server for IDE integration (Claude Code, Cursor)."""
    supported = ["stdio", "sse"]
    if transport not in supported:
        click.echo(
            f"Unsupported transport: {transport}. Supported: stdio, sse",
            err=True,
        )
        sys.exit(1)
    if not (1 <= port <= 65535):
        click.echo(
            f"Invalid port: {port}. Port must be between 1 and 65535",
            err=True,
        )
        sys.exit(1)
    from ..mcp import run_mcp_server  # noqa: PLC0415

    run_mcp_server(transport=transport, port=port)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Public facade re-exports (EPIC #942 / issue #998, 950a)
#
# ``orchestration_engine.cli`` must keep exposing the exact pre-refactor surface:
# the ``main`` Click group (the ``orch`` entry point) plus the private internals
# imported by the test-suite. ``main`` / shared helpers come from the sibling
# modules imported above; the test-imported command-coupled privates below are
# defined inline in this module and are listed here for explicit, self-checking
# completeness. A dropped name is caught by tests/test_facade_surface_942.py.
# ---------------------------------------------------------------------------
__all__ = [
    "main",
    "get_queue",
    "format_datetime",
    "format_duration",
    "print_table",
    "_validate_required_config",
    "_read_openclaw_token",
    "_watch_pipeline_run",
    "_print_watch_event",
    "_print_run_detail",
    "_safe_write_phase_output",
    "_is_github_shorthand",
    "_install_from_git",
    "_find_yaml_in_dir",
    "_apply_fixes",
    "_check_yaml_syntax",
    "_normalize_git_url",
    "_slugify_title",
]
