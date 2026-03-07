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
    executor: Optional[Any] = None,
    audit: bool = False,
) -> tuple[bool, float]:
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
        executor: Optional executor object (e.g. ``OpenClawExecutor`` or
                  ``AnthropicExecutor``).  When provided, LLM judge criteria
                  are routed through ``executor.execute()`` instead of direct
                  urllib calls.  Pass the same executor used for the pipeline
                  run so that judge scoring shares the same authentication path
                  (e.g. the OpenClaw subscription token in openclaw mode).
        audit:    When ``True``, run an adversarial :class:`~audit.AuditPhase`
                  after normal scoring completes.  The audit reads the review
                  phase output from *output_dir* and logs a
                  :class:`~audit.AuditResult` summary.  Defaults to ``False``
                  for backward compatibility.  This is additive — it does not
                  affect the return value or exit behaviour.

    Returns:
        A ``(passed: bool, weighted_score: float)`` tuple.  ``passed`` is
        ``True`` if the scenario passed, ``False`` otherwise.
        ``weighted_score`` is the 0.0–1.0 weighted score from the grader;
        ``0.0`` is returned on error paths.
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

    scenario_runner = ScenarioRunner(scenarios_dir=scenario_path.parent, executor=executor)
    try:
        scenario = scenario_runner.load_scenario(scenario_path)
    except (ValueError, yaml.YAMLError) as exc:
        console.print(
            f"[red]✗ Auto-scoring failed:[/red] invalid scenario file: {exc}",
            highlight=False,
        )
        if exit_on_failure:
            sys.exit(1)
        return False, 0.0

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
        score_result = scenario_runner.run_scenario(
            scenario, pipeline_output, output_dir=output_dir
        )
    except Exception as exc:
        console.print(
            f"[red]✗ Auto-scoring failed:[/red] grading error: {exc}",
            highlight=False,
        )
        logger.exception("Auto-scoring grading error")
        if exit_on_failure:
            sys.exit(1)
        return False, 0.0

    # ── 7. Print score report ─────────────────────────────────────────
    _print_score_report(console, score_result, scenario)

    # ── 8. Return / exit ──────────────────────────────────────────────
    if not score_result.passed and exit_on_failure:
        sys.exit(1)

    # ── 9. Optional adversarial audit (Issue #4.1.4) ──────────────────
    if audit:
        _run_adversarial_audit(output_dir=output_dir, executor=executor, console=console)

    return score_result.passed, score_result.weighted_score


def _run_adversarial_audit(
    output_dir: Path,
    executor: Optional[Any],
    console: Optional[Any],
) -> None:
    """Run the adversarial :class:`~audit.AuditPhase` on a completed pipeline run.

    Reads the review phase output from *output_dir*, builds a minimal
    ``review_outcome`` dict, and runs :class:`~audit.AuditPhase`.
    The result is logged and printed to *console* but does NOT affect the
    pipeline's pass/fail verdict (additive-only).

    Args:
        output_dir: Path to the pipeline output directory.
        executor:   Optional executor to use for the audit LLM call.
        console:    Rich Console for formatted output.
    """
    from .audit import AuditPhase

    try:
        # Reconstruct a minimal review_outcome from the output directory.
        # Look for a JSON file whose name contains "review".
        review_outcome: Dict[str, Any] = {"verdict": None, "issues_found": []}
        run_id = output_dir.name  # Use directory name as run identifier

        for json_file in sorted(output_dir.glob("*.json")):
            if json_file.name.startswith("_"):
                continue
            if "review" in json_file.name.lower():
                try:
                    data = json.loads(json_file.read_text())
                    if isinstance(data, dict):
                        # Extract verdict and issues_found if present
                        if "verdict" in data:
                            review_outcome["verdict"] = data["verdict"]
                        if "issues_found" in data:
                            review_outcome["issues_found"] = data["issues_found"]
                        elif "result" in data and isinstance(data["result"], dict):
                            text = data["result"].get("text", "")
                            if text:
                                from .review_parser import parse_review_output
                                parsed = parse_review_output(text)
                                review_outcome["verdict"] = parsed.verdict
                                review_outcome["issues_found"] = [
                                    {
                                        "severity": i.severity.value,
                                        "category": i.category,
                                        "description": i.description,
                                        "raw": i.raw,
                                    }
                                    for i in parsed.issues
                                ]
                        break
                except Exception as exc:
                    logger.debug("Could not read review output %s: %s", json_file.name, exc)

        auditor = AuditPhase(executor=executor, model=getattr(executor, "model", "audit-model"))
        audit_result = auditor.run(
            run_id=run_id,
            review_outcome=review_outcome,
        )

        # Log the audit result
        logger.info(
            "Adversarial audit complete: run_id=%s accuracy=%.2f false_approval=%s "
            "issues=%d",
            audit_result.run_id,
            audit_result.reviewer_accuracy_score,
            audit_result.false_approval,
            len(audit_result.caught_issues),
        )

        if console is not None:
            _print_audit_summary(console, audit_result)

    except Exception as exc:
        logger.warning("Adversarial audit failed: %s", exc)
        if console is not None:
            console.print(
                f"[yellow]⚠ Adversarial audit skipped:[/yellow] {exc}",
                highlight=False,
            )


def _print_audit_summary(console: Any, audit_result: Any) -> None:
    """Print a concise adversarial audit summary to *console*.

    Args:
        console:      A :class:`rich.console.Console` instance.
        audit_result: A :class:`~audit.AuditResult`.
    """
    accuracy_pct = audit_result.reviewer_accuracy_score * 100
    verdict_icon = (
        "[green]APPROVE[/green]"
        if audit_result.audit_verdict == "APPROVE"
        else "[red]REQUEST_CHANGES[/red]"
        if audit_result.audit_verdict == "REQUEST_CHANGES"
        else "[dim]unknown[/dim]"
    )
    false_approval_tag = (
        " [bold red](FALSE APPROVAL DETECTED)[/bold red]"
        if audit_result.false_approval
        else ""
    )

    console.print()
    console.print("[bold]Adversarial Audit:[/bold]")
    console.print(f"  Audit verdict:        {verdict_icon}{false_approval_tag}")
    console.print(f"  Reviewer accuracy:    {accuracy_pct:.1f}%")
    console.print(f"  Issues found:         {len(audit_result.caught_issues)}")
    missed = [i for i in audit_result.caught_issues if i.missed_by_reviewer]
    if missed:
        console.print(f"  Missed by reviewer:   {len(missed)}")
        for issue in missed:
            console.print(
                f"    [yellow]•[/yellow] [{issue.severity}][{issue.category}] "
                f"{issue.description}"
            )
    console.print()


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
