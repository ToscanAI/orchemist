"""Merge-gate command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1004, 950d). The ``gate`` group
(list / approve / reject / info) previously lived inline in ``cli/__init__.py``;
its command bodies are moved here VERBATIM. Each command self-registers on the
shared ``main`` Click group (imported from ``._root``) at import time via its
``@main.group`` / ``@gate_group.command`` decorator, so the facade only needs to
import this module for the registration side effect.

The gate commands reach ``GitContext`` / ``GitError`` / ``GitConfig`` through
*function-local lazy imports* of ``..git_integration`` (exactly as they did
inline), so the 950b/950c ``_cli.<dep>`` call-time facade indirection is NOT
needed: the test-suite patches ``orchestration_engine.git_integration.GitContext.*``
on the source module, which these lazy imports observe regardless of where the
command body now lives.
"""

import sys
from typing import Optional

import click

from ._helpers import print_table
from ._root import main

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
