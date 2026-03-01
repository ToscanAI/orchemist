"""Post-pipeline auto-scoring helper (Issue #172).

This module encapsulates the scoring logic invoked by the ``orch run`` CLI
command after a pipeline completes.  It is also callable standalone for
testing or scripting purposes.

Typical usage::

    from pathlib import Path
    from orchestration_engine.scoring import run_scoring
    from orchestration_engine.templates import TemplateEngine

    engine = TemplateEngine()
    template = engine.load_template(Path("my-template.yaml"))
    run_scoring(template, output_dir=Path("output/my-run-dir"))
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _load_pipeline_output(output_dir: Path) -> Dict[str, Any]:
    """Load pipeline output from *output_dir* into a grading-compatible dict.

    Reads ``_final_output.json`` (final phase output) and the per-phase
    ``.json`` files, assembling the structure expected by
    :class:`~scenario_runner.runner.ScenarioRunner.run_scenario`:

    .. code-block:: python

        {
            "final":  <final output dict>,
            "phases": {<phase_id>: <phase output dict>, ...},
        }

    Args:
        output_dir: Resolved path to a completed pipeline run directory.

    Returns:
        Dict suitable for passing to ``ScenarioRunner.run_scenario()``.
    """
    output_dir = Path(output_dir)

    # Load final output
    final_output: Dict[str, Any] = {}
    final_json = output_dir / "_final_output.json"
    if final_json.exists():
        try:
            final_output = json.loads(final_json.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read _final_output.json: %s", exc)

    # Load per-phase outputs (all *.json files except _ prefixed ones)
    phases: Dict[str, Any] = {}
    for json_file in sorted(output_dir.glob("*.json")):
        if json_file.name.startswith("_"):
            continue
        phase_id = json_file.stem
        try:
            phases[phase_id] = json.loads(json_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read phase output %s: %s", json_file.name, exc)

    return {"final": final_output, "phases": phases}


def run_scoring(
    template: Any,
    output_dir: Path,
    console: Optional[Any] = None,
    template_file: Optional[Path] = None,
    exit_on_failure: bool = True,
) -> bool:
    """Run post-pipeline auto-scoring for a completed pipeline run.

    Resolves the scenario file from ``template.scenario``, loads the
    scenario, reads pipeline output from *output_dir*, invokes the
    :class:`~scenario_runner.runner.ScenarioRunner`, prints the score
    report, and optionally exits with code 1 if the scenario fails.

    Args:
        template: A loaded :class:`~orchestration_engine.templates.PipelineTemplate`
                  with a non-None ``scenario`` field.
        output_dir: Path to the directory containing phase output files from a
                    completed pipeline run.
        console: An optional :class:`rich.console.Console` instance for
                 formatted output.  When ``None`` a plain-text console is
                 created automatically.
        template_file: Optional path to the template YAML file.  Used as the
                       base directory for resolving relative scenario paths.
                       Falls back to ``template.template_path`` when not
                       provided.
        exit_on_failure: When ``True`` (the default), calls ``sys.exit(1)``
                         if the scenario does not pass.  Set to ``False``
                         in tests to avoid process termination.

    Returns:
        ``True`` if the scenario passed, ``False`` otherwise.
        (Only meaningful when *exit_on_failure* is ``False``.)

    Raises:
        ValueError: If ``template.scenario`` is ``None`` or empty.
        FileNotFoundError: If the scenario YAML file cannot be found.
    """
    if not template.scenario:
        raise ValueError(
            "template.scenario is not set — nothing to score. "
            "Add a 'scenario:' key to the template YAML."
        )

    # ── 1. Set up console ─────────────────────────────────────────────
    if console is None:
        from rich.console import Console
        console = Console(highlight=False)

    # ── 2. Resolve scenario file path ─────────────────────────────────
    # Relative paths are resolved against the template file's directory.
    scenario_path = Path(template.scenario)
    if not scenario_path.is_absolute():
        # Prefer the explicit template_file argument; fall back to the
        # path stored on the template object itself.
        base_dir: Optional[Path] = None
        if template_file is not None:
            base_dir = Path(template_file).resolve().parent
        elif getattr(template, "template_path", None) is not None:
            base_dir = Path(template.template_path).parent

        if base_dir is not None:
            candidate = base_dir / scenario_path
            if candidate.exists():
                scenario_path = candidate.resolve()
            else:
                # Also try relative to cwd
                cwd_candidate = Path.cwd() / scenario_path
                if cwd_candidate.exists():
                    scenario_path = cwd_candidate.resolve()
                # else: keep original path (will raise FileNotFoundError below)
        else:
            cwd_candidate = Path.cwd() / scenario_path
            if cwd_candidate.exists():
                scenario_path = cwd_candidate.resolve()

    if not scenario_path.exists():
        raise FileNotFoundError(
            f"Scenario file not found: '{template.scenario}' "
            f"(resolved to '{scenario_path}')"
        )

    # ── 3. Import ScenarioRunner ──────────────────────────────────────
    try:
        from scenario_runner.runner import ScenarioRunner
    except ImportError:
        # Fallback: add project root to sys.path
        import sys as _sys
        project_root = Path(__file__).resolve().parent.parent.parent
        _sys.path.insert(0, str(project_root))
        from scenario_runner.runner import ScenarioRunner  # type: ignore[no-redef]

    # ── 4. Load scenario ──────────────────────────────────────────────
    import yaml

    scenario_runner = ScenarioRunner(scenarios_dir=scenario_path.parent)
    try:
        scenario = scenario_runner.load_scenario(scenario_path)
    except (ValueError, yaml.YAMLError) as exc:
        console.print(
            f"[red]✗ Auto-scoring failed:[/red] invalid scenario file: {exc}",
            highlight=False,
        )
        if exit_on_failure:
            sys.exit(1)
        return False

    scenario_name = scenario.get("name", scenario.get("id", scenario_path.stem))
    console.print()
    console.print(
        f"[bold]Auto-scoring:[/bold] {scenario_name} "
        f"[dim]({scenario_path.name})[/dim]"
    )

    # ── 5. Load pipeline output ───────────────────────────────────────
    pipeline_output = _load_pipeline_output(output_dir)

    # ── 6. Grade ──────────────────────────────────────────────────────
    try:
        score_result = scenario_runner.run_scenario(scenario, pipeline_output)
    except Exception as exc:
        console.print(
            f"[red]✗ Auto-scoring failed:[/red] grading error: {exc}",
            highlight=False,
        )
        logger.exception("Auto-scoring grading error")
        if exit_on_failure:
            sys.exit(1)
        return False

    # ── 7. Print score report ─────────────────────────────────────────
    _print_score_report(console, score_result, scenario)

    # ── 8. Return / exit ──────────────────────────────────────────────
    if not score_result.passed and exit_on_failure:
        sys.exit(1)

    return score_result.passed


def _print_score_report(console: Any, score_result: Any, scenario: dict) -> None:
    """Print a rich score report for an auto-scoring run.

    Mirrors :func:`orchestration_engine.cli._print_score_report` but lives
    here so it can be reused without importing the entire CLI module.

    Args:
        console: A :class:`rich.console.Console` instance.
        score_result: A :class:`~scenario_runner.models.ScenarioResult`.
        scenario: The raw scenario dict (for threshold extraction).
    """
    from rich.table import Table

    # ── Per-criterion table ────────────────────────────────────────────
    crit_table = Table(
        title="Auto-Scoring: Acceptance Criteria",
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
        result_icon = (
            "[green]✓ PASS[/green]"
            if cr.grade.passed
            else "[red]✗ FAIL[/red]"
        )
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
        "[bold green]✓ PASS[/bold green]"
        if score_result.passed
        else "[bold red]✗ FAIL[/bold red]"
    )
    gate_status = (
        "[green]all passed[/green]"
        if score_result.gates_passed
        else "[red]one or more FAILED[/red]"
    )

    console.print(f"[bold]Scenario:[/bold]  {score_result.scenario_id}")
    console.print(
        f"[bold]Score:[/bold]     {overall_pct:.1f} / 100  "
        f"(threshold {threshold_pct:.0f})"
    )
    console.print(f"[bold]Gates:[/bold]     {gate_status}")
    console.print(f"[bold]Verdict:[/bold]   {verdict}")
    console.print()
