"""Evaluation/review command groups for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1005, 950e). The ``rubric`` /
``scenario`` / ``reviews`` groups (and the ``_print_score_report`` helper used by
``scenario run``) previously lived inline in ``cli/__init__.py``; their bodies are
moved here VERBATIM. Each group and its commands self-register on the shared
``main`` Click group (imported from ``._root``) at import time via their
``@main.group`` / ``@<group>.command`` decorators, so the facade only needs to
import this module for the registration side effect.

All dependencies the commands touch are either module-locals brought in below
(``yaml`` / ``apply_config_schema_defaults`` / ``default_db_path`` /
``_resolve_template_arg`` / ``print_table``) or function-local lazy imports
(``generate_rubric_file``, ``ScenarioRunner``, ``PipelineRunner``, the
``..db.Database`` re-import the reviews commands do as ``_Database``, etc.), NONE
of which the test-suite patches on the ``orchestration_engine.cli`` facade — so
the 950b/950c ``_cli.<dep>`` call-time indirection is NOT needed here (the rubric,
scenario, and review-queue tests invoke these commands through the ``main`` group
with real paths / a real ``Database``, and the dry-run scenario tests drive
``ScenarioRunner`` directly, never the cli facade attributes).
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click
import yaml

from ..daemon import apply_config_schema_defaults
from ..db import default_db_path
from ._helpers import _resolve_template_arg, print_table
from ._root import main

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
