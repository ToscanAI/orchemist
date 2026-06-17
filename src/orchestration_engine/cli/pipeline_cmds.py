"""Pipeline-run command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1002, 950b). These command
functions (run / launch / logs / children / chain / wait / resume / watch /
start) previously lived inline in ``cli/__init__.py``; their bodies are moved
here VERBATIM. Each command self-registers on the shared ``main`` Click group
(imported from ``._root``) at import time via its ``@main.command`` decorator,
so the facade only needs to import this module for the registration side effect.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import yaml

# Several pipeline commands read ``Database``, ``subprocess``, ``time``,
# ``apply_config_schema_defaults`` and the git/issue/token helpers
# (``_infer_git_context`` / ``_fetch_issue_strict`` / ``_read_openclaw_token``)
# through the ``orchestration_engine.cli`` facade rather than via names bound in
# this module. The existing tests ``patch("orchestration_engine.cli.<name>")`` and
# then invoke these (relocated) commands; resolving through the facade keeps those
# patches effective exactly as when this code lived inline in ``cli/__init__.py``
# (EPIC #942 / 950b). The attributes are read at call time, so the
# partially-initialised facade module during package import is never a problem.
import orchestration_engine.cli as _cli  # noqa: E402  (call-time facade ref; see note)

from ..db import Database, default_db_path
from ..output_utils import (
    extract_output_text as _extract_output_text,
)
from ..output_utils import (
    safe_write_phase_output as _safe_write_phase_output,
)
from ..timestamps import now_utc
from ._helpers import (
    _find_template,
    _get_persistent_db_path,
    _resolve_template_arg,
    _slugify_title,
    _validate_required_config,
)
from ._root import logger, main


@main.command()
@click.argument("run_id", required=True)
@click.option(
    "--json-output", "--json", "json_mode", is_flag=True, help="Machine-readable JSON output"
)
@click.option("--refresh", default=3, help="Refresh interval in seconds")
def watch(run_id: str, json_mode: bool, refresh: int) -> None:  # noqa: C901
    """Watch pipeline run progress in real-time (#414).

    Streams phase transitions, scoring results, warnings, and errors
    as they happen.  Exits when the pipeline reaches a terminal state.

    \b
    Examples:
      orch watch a3f8c2d1           # live-follow a running pipeline
      orch watch a3f8c2d1 --json    # machine-readable event stream

    Also works with legacy task IDs (falls back to old task-queue watch).
    """

    # ── Try pipeline run first ──────────────────────────────────────
    try:
        _db = _cli.Database()
        run = _db.get_pipeline_run(run_id)
    except Exception:  # noqa: BLE001
        run = None

    if run is not None:
        _watch_pipeline_run(run_id, _db, json_mode, refresh)
        return

    # ── Fallback: legacy task-queue watch ────────────────────────────
    try:
        from ..progress import ProgressTracker  # noqa: PLC0415

        db = _cli.Database()
        tracker = ProgressTracker(db)

        def display_progress():
            progress = tracker.get_task_progress(run_id, include_events=True)
            if not progress:
                click.echo(f"❌ Task {run_id} not found")
                return False

            click.clear()
            click.echo(f"📊 Task Progress: {run_id}")
            click.echo("=" * 60)

            status_emoji = {
                "queued": "⏳",
                "running": "🔄",
                "success": "✅",
                "failed": "❌",
                "retry": "🔁",
                "permanently_failed": "💀",
                "cancelled": "🚫",
            }
            emoji = status_emoji.get(progress.current_state.value, "❓")
            click.echo(f"Status: {emoji} {progress.current_state.value.upper()}")

            if progress.current_message:
                click.echo(f"Message: {progress.current_message}")
            click.echo(f"Progress: {progress.progress_percentage:.1f}%")
            if progress.execution_time_seconds:
                click.echo(f"Runtime: {progress.execution_time_seconds:.1f}s")
            if progress.events:
                click.echo(f"\n📋 Recent Events ({len(progress.events)}):")
                for event in progress.events[-5:]:
                    timestamp = event.timestamp.strftime("%H:%M:%S")
                    click.echo(f"  {timestamp} - {event.event_type}: {event.message or 'N/A'}")
            return progress.is_active

        is_active = display_progress()
        if is_active:
            click.echo(f"\n🔄 Following (refresh every {refresh}s, Ctrl+C to stop)...")
            try:
                while is_active:
                    _cli.time.sleep(refresh)
                    is_active = display_progress()
                click.echo("\n✅ Task completed")
            except KeyboardInterrupt:
                click.echo("\n👋 Stopped")
    except Exception:  # noqa: BLE001
        click.echo(f"Error: '{run_id}' not found in pipeline runs or task queue.", err=True)
        sys.exit(1)


def _watch_pipeline_run(  # noqa: C901
    run_id: str, db: "Database", json_mode: bool, refresh: int
) -> None:
    """Stream pipeline run events in real-time (#414).

    Polls the pipeline_run_events table and prints formatted output
    as phases start, complete, stall, or score.
    """
    import json as _json  # noqa: PLC0415

    terminal_states = {
        "success",
        "failed",
        "cancelled",
        "crashed",
        "scoring_failed",
        "pending_review",
        "rejected",
    }

    last_event_id = 0
    last_status = None
    header_printed = False

    try:
        while True:
            run = db.get_pipeline_run(run_id)
            if run is None:
                click.echo(f"✗ Run '{run_id}' not found.", err=True)
                sys.exit(1)

            current_status = run["status"]

            # Check PID liveness
            if current_status == "running" and run.get("pid"):
                try:
                    from ..daemon import is_process_alive  # noqa: PLC0415

                    if not is_process_alive(run["pid"]):
                        current_status = "crashed"
                        db.update_pipeline_run(run_id, status="crashed")
                except Exception:  # noqa: BLE001
                    pass

            # Print header once
            if not header_printed:
                template = run.get("template_id", "?")
                click.echo(f"🔄 Pipeline {run_id} — {template}")
                header_printed = True

            # Fetch new events
            events = db.list_pipeline_run_events(run_id, after_id=last_event_id, limit=100)
            for evt in events:
                last_event_id = evt["id"]
                _print_watch_event(evt, json_mode)

            # Status change
            if current_status != last_status:
                if current_status in terminal_states:
                    _scoring = run.get("scoring_status")
                    _score = run.get("scoring_score")
                    score_str = f" (score={_score:.3f})" if _score is not None else ""
                    icon = {
                        "success": "✅",
                        "pending_review": "📋",
                        "failed": "❌",
                        "crashed": "💀",
                        "scoring_failed": "🔴",
                    }.get(current_status, "❓")
                    ts = now_utc().strftime("%H:%M:%S")
                    if json_mode:
                        click.echo(
                            _json.dumps(
                                {
                                    "time": ts,
                                    "type": "run_complete",
                                    "status": current_status,
                                    "scoring_status": _scoring,
                                    "scoring_score": _score,
                                }
                            )
                        )
                    else:
                        click.echo(
                            f"  [{ts}] 🏁 Pipeline complete — "
                            f"{icon} {current_status}{score_str}"
                        )
                    return
                last_status = current_status

            _cli.time.sleep(refresh)

    except KeyboardInterrupt:
        click.echo("\n👋 Stopped watching")


def _print_watch_event(evt: dict, json_mode: bool) -> None:
    """Format and print a single pipeline run event (#414)."""
    import json as _json  # noqa: PLC0415

    event_type = evt.get("event_type", "")
    phase = evt.get("phase_id") or ""
    tokens = evt.get("tokens_consumed")
    state = evt.get("state")
    ts = now_utc().strftime("%H:%M:%S")

    try:
        meta = _json.loads(evt.get("metadata_json", "{}"))
    except (TypeError, _json.JSONDecodeError):
        meta = {}

    if json_mode:
        click.echo(
            _json.dumps(
                {
                    "time": ts,
                    "type": event_type,
                    "phase": phase,
                    "tokens": tokens,
                    "state": state,
                    "metadata": meta,
                }
            )
        )
        return

    # Human-friendly formatting
    if event_type == "phase_started":
        click.echo(f"  [{ts}] ▶ {phase} started")
    elif event_type == "phase_completed":
        tokens_str = f", {tokens:,} tokens" if tokens else ""
        state_str = f" — {state}" if state else ""
        click.echo(f"  [{ts}] ✓ {phase} completed{tokens_str}{state_str}")
    elif event_type == "stall_detected":
        msg = meta.get("message", "No token progress detected")
        click.echo(f"  [{ts}] ⚠️  {msg}")
    elif event_type == "status_changed":
        new_status = meta.get("new_status") or state or "?"
        click.echo(f"  [{ts}] 🔄 status → {new_status}")
    else:
        click.echo(f"  [{ts}] {event_type}: {phase or '(run)'}")


@main.command("run")
@click.argument("template_name_or_file")
@click.option(
    "--mode",
    type=click.Choice(["standalone", "openclaw", "openrouter", "dry-run"]),
    default="standalone",
    show_default=True,
    help="Execution mode: standalone (direct API), openclaw (sub-agent), openrouter (multi-provider), dry-run (mock).",  # noqa: E501
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    default=None,
    help="API key for standalone (ANTHROPIC_API_KEY) or openrouter (OPENROUTER_API_KEY) mode.",
)
@click.option(
    "--input",
    "input_json",
    default=None,
    help='Pipeline input as a JSON string, e.g. \'{"brief": "AI safety"}\'.',
)
@click.option(
    "--input-file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to a JSON file containing pipeline input.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to write phase outputs. Created if missing. "
    "Defaults to ./output/<template-id>-<YYYYMMDD-HHMMSS>/",
)
@click.option(
    "--gateway-url",
    default=None,
    help="OpenClaw gateway URL for openclaw mode (or set OPENCLAW_GATEWAY_URL).",
)
@click.option(
    "--gateway-token",
    default=None,
    help="OpenClaw gateway bearer token for openclaw mode (or set OPENCLAW_GATEWAY_TOKEN).",
)
@click.option(
    "--dry-run-delay",
    type=float,
    default=0.0,
    hidden=True,
    help="(dry-run mode only) Simulated per-phase delay in seconds.",
)
@click.option(
    "--dry-run-failure-rate",
    type=float,
    default=0.0,
    hidden=True,
    help="(dry-run mode only) Probability [0.0-1.0] of simulated phase failure.",
)
@click.option(
    "--skip-scoring",
    is_flag=True,
    default=False,
    help="Skip auto-scoring even if the template declares a scenario.",
)
@click.option(
    "--score-only",
    is_flag=True,
    default=False,
    help=(
        "Run scoring on an existing output directory without re-running the pipeline. "
        "Requires --output-dir pointing to a completed run."
    ),
)
@click.option(
    "--issue",
    "issue_number",
    type=int,
    default=None,
    help="GitHub issue number to auto-fetch and merge into pipeline input.",
)
@click.option(
    "--repo",
    "repo",
    default=None,
    help="GitHub repository slug (e.g. owner/repo) for --issue lookup.",
)
@click.option(
    "--executor",
    type=click.Choice(["api", "claudecode", "auto"]),
    default="auto",
    show_default=True,
    help="Executor backend for standalone mode: api (ANTHROPIC_API_KEY), claudecode, or auto.",
)
@click.option(
    "--model-map",
    "model_map_json",
    default=None,
    help='JSON model tier overrides for openrouter mode, e.g. \'{"sonnet": "openai/gpt-4o"}\'.',
)
@click.option(
    "--base-url",
    default=None,
    help=(
        "Custom OpenAI-compatible base URL for openrouter mode — point at a local "
        "server (Ollama, LM Studio, vLLM), e.g. http://localhost:11434/v1. Include the "
        "/v1 suffix. No API key is required when this targets a non-default endpoint."
    ),
)
def run_template(  # noqa: C901
    template_name_or_file: str,
    mode: str,
    api_key: Optional[str],
    input_json: Optional[str],
    input_file: Optional[Path],
    output_dir: Optional[Path],
    gateway_url: Optional[str],
    gateway_token: Optional[str],
    dry_run_delay: float,
    dry_run_failure_rate: float,
    skip_scoring: bool,
    score_only: bool,
    issue_number: Optional[int],
    repo: Optional[str],
    executor: str,
    model_map_json: Optional[str],
    base_url: Optional[str],
) -> None:
    """Execute a pipeline template end-to-end.

    Persists a ``pipeline_runs`` record + per-phase costs to
    ``~/.orchestration-engine/engine.db`` (created on first use); persistence
    failures are non-fatal and warn once.

    TEMPLATE_NAME_OR_FILE is a template name (e.g. content-pipeline) or a
    path to a YAML file.  Template names are resolved using the search order:
    ORCH_TEMPLATES_PATH → ./templates/ → ~/.orch/templates/ → bundled.

    Examples:

      # By name (resolved automatically):
      orch run content-pipeline --mode dry-run

      # By path:
      orch run pipeline.yaml --mode standalone \\
        --api-key sk-ant-... \\
        --input '{"brief": "AI safety"}'

      # Standalone reading input from file:
      orch run pipeline.yaml --input-file brief.json \\
        --output-dir ./results/

      # OpenClaw sub-agent mode:
      orch run pipeline.yaml --mode openclaw

      # Dry-run (no API calls):
      orch run pipeline.yaml --mode dry-run
    """
    import json as _json  # noqa: PLC0415

    # --executor is only valid with --mode standalone (dry-run ignores it; openclaw/openrouter are incompatible)  # noqa: E501
    if mode in ("openclaw", "openrouter") and executor != "auto":
        click.echo("Error: --executor is only valid with --mode standalone", err=True)
        sys.exit(1)

    # NB (#969): the --model-map / --base-url mode guards are deferred until
    # AFTER the template is loaded (just below the structural-validation block)
    # so the mixed-provider auto-upgrade — which forwards these run-level
    # OpenRouter flags to the openrouter executor it builds — can consume them
    # in a non-openrouter mode whose template declares per-phase providers.

    import sys as _sys  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415

    from ..heartbeat import ProgressHeartbeat  # noqa: PLC0415
    from ..pipeline_runner import PipelineRunner  # noqa: PLC0415
    from ..sequencer import PhaseSequencer, StateMachineSequencer  # noqa: PLC0415
    from ..templates import TemplateEngine  # noqa: PLC0415

    # Force plain text output in non-TTY environments (background, nohup, pipes)
    console = Console(
        highlight=False,
        force_terminal=_sys.stdout.isatty(),
        no_color=not _sys.stdout.isatty(),
    )
    run_start = _cli.time.time()

    # --- 1. Resolve template path (name or path) ----------------------
    template_file = _resolve_template_arg(template_name_or_file)

    # --- 2. Load and validate template --------------------------------
    try:
        engine = TemplateEngine()
        template = engine.load_template(template_file)
    except FileNotFoundError as exc:
        click.echo(f"✗ Template file not found: {exc}", err=True)
        sys.exit(1)
    except (KeyError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"✗ Invalid template: {exc}", err=True)
        sys.exit(1)

    errors = engine.validate_template(template)
    # Issue #295: --skip-scoring opts out of the mandatory-scenario check
    if skip_scoring:
        errors = [e for e in errors if "require a scenario" not in e]
    if errors:
        click.echo(f"✗ Template has {len(errors)} structural error(s):", err=True)
        for err in errors:
            click.echo(f"  • {err}", err=True)
        sys.exit(1)

    # --- 2a. Per-phase provider auto-upgrade detection (#969) ---------
    # Does the template declare any per-phase ``provider:``? When it does (and
    # we are not already in openrouter mode), the runner build below
    # auto-upgrades to PipelineRunner.from_providers — building one executor per
    # referenced provider so the run "just works" when both credentials are
    # present (the headline mixed-provider story).
    _declared_providers = {p.provider for p in template.phases if getattr(p, "provider", None)}

    # --model-map / --base-url mode guards (relaxed per #969 F1): these stay
    # OpenRouter-only run flags, but are ALSO accepted when a non-openrouter
    # mode's template declares per-phase providers (the auto-upgrade forwards
    # them to the openrouter executor it builds). Otherwise they remain rejected.
    _mixed_upgrade = bool(_declared_providers) and mode in ("standalone", "openrouter")
    if model_map_json and mode != "openrouter" and not _mixed_upgrade:
        click.echo("Error: --model-map is only valid with --mode openrouter", err=True)
        sys.exit(1)
    if base_url and mode != "openrouter" and not _mixed_upgrade:
        click.echo("Error: --base-url is only valid with --mode openrouter", err=True)
        sys.exit(1)

    # --- Score-only mode (Issue #172) ---------------------------------
    if score_only:
        if output_dir is None:
            click.echo(
                "✗ --score-only requires --output-dir pointing to a completed run.",
                err=True,
            )
            sys.exit(1)
        if not template.scenario:
            click.echo(
                "✗ Template has no 'scenario' field — nothing to score.",
                err=True,
            )
            sys.exit(1)
        # Build an executor for the LLM judge grader so that scoring uses the
        # same authentication path as the mode specified by the caller.
        # In openclaw mode this routes judge calls through the gateway token
        # instead of a raw ANTHROPIC_API_KEY.  Issue #272.
        import os as _os_so  # noqa: PLC0415

        from ..scoring import run_scoring as _run_scoring  # noqa: PLC0415

        _so_executor = None
        if mode == "openclaw":
            try:
                from ..openclaw_executor import OpenClawExecutor  # noqa: PLC0415

                _so_url = gateway_url or _os_so.environ.get("OPENCLAW_GATEWAY_URL")
                _so_token = gateway_token or _os_so.environ.get("OPENCLAW_GATEWAY_TOKEN")
                _so_executor = OpenClawExecutor(
                    gateway_url=_so_url,
                    gateway_token=_so_token,
                )
            except Exception as _so_exc:  # noqa: BLE001
                click.echo(
                    f"⚠ Could not create OpenClawExecutor for grader: {_so_exc}\n"
                    "  LLM judge criteria will fall back to ANTHROPIC_API_KEY.",
                    err=True,
                )
        elif mode == "standalone" and api_key:
            try:
                from ..executors.anthropic_executor import AnthropicExecutor  # noqa: PLC0415

                _so_executor = AnthropicExecutor(api_key=api_key)
            except Exception:  # noqa: BLE001
                pass  # fall back to ANTHROPIC_API_KEY env var
        _run_scoring(
            template,
            output_dir=output_dir,
            console=console,
            template_file=template_file,
            exit_on_failure=True,
            executor=_so_executor,
        )
        sys.exit(0)

    # --- 1b. Default output directory (Feature #72) ------------------
    from uuid import uuid4  # noqa: PLC0415

    run_id = str(uuid4())[:8]
    if output_dir is None:
        _safe_id = re.sub(r"[^\w\-]", "_", template.id)
        _ts = now_utc().strftime("%Y%m%d-%H%M%S")
        output_dir = Path(f"./output/{_safe_id}-{_ts}-{run_id}")

    # --- 2. Resolve pipeline input ------------------------------------
    if input_file and input_json:
        click.echo("⚠ Both --input and --input-file provided; using --input-file", err=True)

    initial_input: Dict[str, Any] = {}
    if input_file:
        try:
            initial_input = _json.loads(input_file.read_text())
        except (_json.JSONDecodeError, OSError) as exc:
            click.echo(f"✗ Could not read input file: {exc}", err=True)
            sys.exit(1)
    elif input_json:
        try:
            initial_input = _json.loads(input_json)
        except _json.JSONDecodeError as exc:
            click.echo(f"✗ Invalid JSON in --input: {exc}", err=True)
            sys.exit(1)

    # --- 2b. Auto-fetch GitHub issue data (#507) ---
    if issue_number is not None:
        if not repo:
            click.echo(
                "⚠ --issue requires --repo (e.g. owner/repo). Skipping GitHub fetch.", err=True
            )
        else:
            try:
                from ..github_fetcher import fetch_github_issue  # noqa: PLC0415

                issue_data = fetch_github_issue(repo=repo, issue_number=issue_number)
                if issue_data:
                    initial_input = issue_data.merge_into(initial_input)
                    click.echo(
                        f"  ✓ GitHub issue #{issue_number} fetched: {issue_data.title!r}",
                        err=True,
                    )
                else:
                    click.echo(
                        f"  ⚠ Could not fetch GitHub issue #{issue_number} from {repo} — continuing with provided input.",  # noqa: E501
                        err=True,
                    )
            except Exception as _gh_exc:  # noqa: BLE001
                click.echo(
                    f"  ⚠ GitHub fetch error: {_gh_exc} — continuing with provided input.",
                    err=True,
                )

    # --- 2c. Validate required config fields (#411) ---
    missing = _validate_required_config(template, initial_input)
    # Apply schema defaults for optional fields (#835) — runs AFTER required-
    # field validation so missing-required errors are still reported on the
    # original input, but BEFORE the sequencer reads `initial_input` for prompt
    # rendering. Note (#676): _validate_required_config treats a field that is
    # both required AND defaulted as satisfiable (it will be filled here), so
    # such a field is not reported missing; truly-required fields with no
    # default still error. Without applying defaults, pre-v2.1 consumers
    # running `orch run` against the v2.1.0 standard pipeline would see
    # <MISSING:ui_primitive_paths> (and similar) literals in Phase 0 prompts.
    _cli.apply_config_schema_defaults(initial_input, getattr(template, "config_schema", None))
    if missing:
        if mode == "dry-run":
            # In dry-run mode, missing required fields are non-fatal: phases run with
            # synthetic output anyway, so warn but continue (issue #659).
            click.echo(
                f"⚠ Dry-run: {len(missing)} required field(s) not provided "
                f"({', '.join(missing)}) — continuing with synthetic output.",
                err=True,
            )
        else:
            click.echo(f"✗ Missing {len(missing)} required config field(s):", err=True)
            for field in missing:
                click.echo(f"  • {field}", err=True)
            click.echo(
                "\nThese fields are required by the template's config_schema. "
                "Add them to your --input or --input-file JSON.",
                err=True,
            )
            sys.exit(1)

    # --- 3. Build PipelineRunner based on mode -----------------------
    import os as _os_env  # noqa: PLC0415

    try:
        if _mixed_upgrade:
            # Mixed-provider auto-upgrade (#969): build one executor per
            # referenced provider. The current --mode picks the DEFAULT
            # (first/fallback) executor so executors[0] stays the no-provider
            # selection (INV-1). --api-key feeds ANTHROPIC only; openrouter
            # sources its key from OPENROUTER_API_KEY env exclusively (F2 — one
            # key cannot satisfy both providers).
            _default = "openrouter" if mode == "openrouter" else "anthropic"
            _model_map = None
            if model_map_json:
                try:
                    _model_map = json.loads(model_map_json)
                except json.JSONDecodeError as e:
                    click.echo(f"Error: --model-map is not valid JSON: {e}", err=True)
                    sys.exit(1)
            runner = PipelineRunner.from_providers(
                template,
                anthropic_api_key=api_key,
                openrouter_api_key=_os_env.environ.get("OPENROUTER_API_KEY", ""),
                openrouter_base_url=base_url,
                openrouter_model_map=_model_map,
                default_provider=_default,
            )
        elif mode == "standalone":
            runner = PipelineRunner.standalone(api_key=api_key, executor_type=executor)
        elif mode == "openclaw":
            # Read env vars only when actually needed (avoid leaking in dry-run tracebacks)
            effective_url = gateway_url or _os_env.environ.get("OPENCLAW_GATEWAY_URL")
            effective_token = gateway_token or _os_env.environ.get("OPENCLAW_GATEWAY_TOKEN")
            runner = PipelineRunner.openclaw(
                gateway_url=effective_url,
                gateway_token=effective_token,
            )
        elif mode == "openrouter":
            effective_key = api_key or _os_env.environ.get("OPENROUTER_API_KEY", "")
            model_map = None
            if model_map_json:
                try:
                    model_map = json.loads(model_map_json)
                except json.JSONDecodeError as e:
                    click.echo(f"Error: --model-map is not valid JSON: {e}", err=True)
                    sys.exit(1)
            runner = PipelineRunner.openrouter(
                api_key=effective_key, model_map=model_map, base_url=base_url
            )
        else:  # dry-run
            runner = PipelineRunner.dry_run(
                delay_seconds=dry_run_delay,
                failure_rate=dry_run_failure_rate,
            )
    except ValueError as exc:
        click.echo(f"✗ {exc}", err=True)
        sys.exit(1)

    # --- 3a. Run persistence (#979/#980) ------------------------------
    # Foreground runs are submitter+executor in one process, so we INSERT the
    # pipeline_runs row here (status=running) — mirroring `orch start`
    # (cli.py ~2132) — then drive per-phase + terminal updates via the
    # callbacks/handlers below using the daemon's importable helpers (#954).
    # Failure to open the DB must NEVER kill the run (req 5): on any error we
    # warn ONCE and continue with persistence disabled (db is None).
    from ..daemon import (  # noqa: PLC0415
        _persist_phase_complete,
        _persist_phase_start,
    )

    db = None
    _cost_tracker = None
    _fg_completed_phases: list = []
    _fg_phase_outputs: Dict[str, Any] = {}
    try:
        db = _cli.Database()
        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": str(template_file.resolve()),
                "template_id": template.id,
                "input_json": _json.dumps(initial_input),
                "mode": mode,
                "output_dir": str(output_dir.resolve()),
                "gateway_url": gateway_url,
                "skip_scoring": int(skip_scoring),
                "status": "running",
            }
        )
        db.update_pipeline_run(
            run_id,
            pid=os.getpid(),
            started_at=now_utc().isoformat(),
        )
    except Exception as _db_exc:  # noqa: BLE001
        try:
            _db_path_repr = str(default_db_path())
        except Exception:  # noqa: BLE001
            _db_path_repr = "~/.orchestration-engine/engine.db"
        logger.warning(
            "Run persistence disabled — could not open run database at %s: %s",
            _db_path_repr,
            _db_exc,
        )
        db = None
    if db is not None:
        try:
            from ..cost_tracker import CostTracker  # noqa: PLC0415

            _cost_tracker = CostTracker(db)
        except Exception as _ct_exc:  # noqa: BLE001
            logger.warning("CostTracker init failed (non-fatal): %s", _ct_exc)
            _cost_tracker = None

    # --- 4. Execute pipeline -----------------------------------------
    n_phases = len(template.phases)
    console.print(
        f"[bold]Pipeline:[/bold] {template.name!r}  "
        f"({n_phases} phase{'s' if n_phases != 1 else ''})"
    )
    console.print(f"[bold]Mode:[/bold]     {mode}")
    console.print(f"[bold]Output:[/bold]   {output_dir}/")
    console.print()

    # --- 3b. Non-TTY heartbeat (Issue #186) --------------------------
    # In non-TTY environments (piped, nohup, CI) the CLI goes silent between
    # phase completions.  ProgressHeartbeat emits a status line every 30s so
    # operators can confirm the pipeline is alive.  In TTY mode (isatty==True)
    # the class is automatically inactive and the Rich progress bar is
    # unaffected.
    heartbeat = ProgressHeartbeat(
        total_phases=n_phases,
        start_time=run_start,
    )

    # --- 3c. Set up git integration if template has it enabled --------
    from ..git_integration import GitContext, GitError  # noqa: PLC0415

    git_ctx: Optional[GitContext] = None
    _gate_result = None  # MergeGateResult set by on_pipeline_complete hook

    if template.git_config is not None and template.git_config.enabled and mode != "dry-run":
        git_ctx = GitContext(
            config=template.git_config,
            pipeline_id=template.id,
            run_id=run_id,
            output_dir=output_dir,
            issue_number=initial_input.get("issue_number"),  # None if not in input
        )
        console.print(
            f"[bold]Git:[/bold]      enabled  "
            f"(branch_pattern={template.git_config.branch_pattern!r})"
        )
    elif template.git_config is not None and template.git_config.enabled and mode == "dry-run":
        console.print("[yellow]Git:[/yellow]      skipped in dry-run mode")

    # Build git lifecycle hooks (no-ops when git_ctx is None)
    def _on_pipeline_start_hook(pipeline_context: dict) -> None:
        """Create feature branch and populate pipeline_context."""
        if git_ctx is None:
            return
        try:
            branch_info = git_ctx.on_pipeline_start()
            pipeline_context["branch_name"] = branch_info.branch_name
            pipeline_context["base_branch"] = branch_info.base_branch
            console.print(
                f"  [cyan]git[/cyan] branch created: "
                f"[bold]{branch_info.branch_name}[/bold] "
                f"(from {branch_info.base_branch})"
            )
        except GitError as exc:
            console.print(f"  [red]✗ git setup failed:[/red] {exc}", highlight=False)
            raise

    def _on_phase_complete_git(phase_id: str, phase_result: dict) -> None:
        """Stage + commit after code phases, and refresh diff in context."""
        if git_ctx is None:
            return
        try:
            commit = git_ctx.on_phase_complete(phase_id, phase_result)
            if commit:
                console.print(
                    f"  [cyan]git[/cyan] committed phase '{phase_id}' → "
                    f"{commit.sha[:8]}  ({commit.files_changed} file(s))"
                )
            # Refresh diff in context so downstream review phases see it
            diff = git_ctx.get_branch_diff()
            if diff and hasattr(git_ctx, "_branch_info") and git_ctx._branch_info:
                # Update pipeline_context (accessed via sequencer reference)
                _pipeline_context_ref[0]["git_diff"] = diff
        except GitError as exc:
            console.print(f"  [yellow]⚠ git commit failed:[/yellow] {exc}", highlight=False)

    # We need a mutable reference so the nested closure can update pipeline_context.
    # The list is populated after the sequencer is created.
    _pipeline_context_ref: list = [{}]

    def _on_pipeline_complete_hook(
        pipeline_context: dict,  # noqa: ARG001
        result: Optional[dict],
    ) -> None:
        """Push and enter merge gate (or cleanup on failure)."""
        nonlocal _gate_result
        if git_ctx is None:
            return
        success = result is not None and not result.get("aborted", False)
        try:
            gate_result = git_ctx.on_pipeline_complete(success=success)
            _gate_result = gate_result
        except GitError as exc:
            console.print(
                f"  [yellow]⚠ git pipeline-complete failed:[/yellow] {exc}",
                highlight=False,
            )
        finally:
            git_ctx.cleanup(success=success)

    # Heartbeat phase-start callback (Issue #186)
    def _on_phase_start_cb(phase_id: str, phase, wave_index: int) -> None:  # noqa: ARG001
        """Notify the heartbeat that a new phase has started.

        Updates the heartbeat's current-phase display so the next emitted
        line shows the correct phase name.  This is a no-op when the
        heartbeat is inactive (TTY mode).
        """
        heartbeat.set_current_phase(phase_id)
        # Persist the running phase so `orch status`/the harness reflect it (#516/#979).
        # No-op when persistence is disabled (db is None — see degradation contract).
        if db is not None:
            try:
                _persist_phase_start(db, run_id, phase_id)
            except Exception as _persist_exc:  # noqa: BLE001
                logger.warning(
                    "Phase-start persistence failed for '%s' (non-fatal): %s",
                    phase_id,
                    _persist_exc,
                )

    # Live phase-completion callback (Feature #70)
    def _on_phase_complete(phase_id: str, phase_result: dict) -> None:
        # --- #979/#980 persistence (mirrors daemon.py _on_phase_complete) ---
        # Mutate the foreground accumulators BEFORE the serialize-only helper
        # (daemon.py:565-566 → 618 ordering). No-op when db is None.
        if db is not None:
            _fg_completed_phases.append(phase_id)
            _fg_phase_outputs[phase_id] = phase_result
            try:
                _persist_phase_complete(
                    db, run_id, phase_id, _fg_completed_phases, _fg_phase_outputs
                )
            except Exception as _persist_exc:  # noqa: BLE001
                logger.warning(
                    "Phase-complete persistence failed for '%s' (non-fatal): %s",
                    phase_id,
                    _persist_exc,
                )
        # Record per-phase cost (verbatim daemon.py:663-693 extraction). No-op
        # when the CostTracker is unavailable; dry-run results carry 0 tokens
        # so both branches are skipped (zero cost rows — req 4).
        if _cost_tracker is not None:
            try:
                _model = phase_result.get("model_used") or "unknown"
                _in = phase_result.get("input_tokens")
                _out = phase_result.get("output_tokens")
                if _in is not None and _out is not None and (_in or _out):
                    _cost_tracker.record_phase(
                        run_id=run_id,
                        phase_id=phase_id,
                        model=_model,
                        input_tokens=int(_in),
                        output_tokens=int(_out),
                    )
                else:
                    _total_tokens = phase_result.get("tokens_consumed") or 0
                    if _total_tokens > 0:
                        _cost_tracker.record_phase(
                            run_id=run_id,
                            phase_id=phase_id,
                            model=_model,
                            input_tokens=0,
                            output_tokens=_total_tokens,
                        )
            except Exception as _cost_exc:  # noqa: BLE001
                logger.warning(
                    "Cost recording failed for phase '%s' (non-fatal): %s",
                    phase_id,
                    _cost_exc,
                )
        _st = phase_result.get("state", "unknown")
        state_val = _st.value if hasattr(_st, "value") else str(_st)
        tokens = phase_result.get("tokens_consumed", 0)
        cost = phase_result.get("cost_usd", 0)
        cost_str = f"${float(cost):.4f}" if cost else "n/a"
        safe_pid = re.sub(r"[^\w\-]", "_", phase_id)
        if state_val in ("failed", "permanently_failed"):
            console.print(
                f"  [red]✗[/red] {safe_pid:30s}  state={state_val}  "
                f"tokens={tokens}  cost={cost_str}"
            )
        else:
            console.print(
                f"  [green]✓[/green] {safe_pid:30s}  state={state_val}  "
                f"tokens={tokens}  cost={cost_str}"
            )
        # Advance the heartbeat completed-phase counter (Issue #186).
        # Counts both successful and failed phases (pipeline aborts on first failure).
        heartbeat.on_phase_complete()

        # Write phase output to disk immediately (#239 follow-up).
        # This allows the sequencer to read from disk for prompt building,
        # avoiding truncation from in-memory session history capture.
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            phase_text = _extract_output_text(phase_result)
            if phase_text:
                out_path = output_dir / f"{safe_pid}.md"
                new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
                _safe_write_phase_output(out_path, new_content, phase_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to write phase output to disk: {exc}")

        # Run git commit hook after progress display
        _on_phase_complete_git(phase_id, phase_result)

    with runner:
        _has_transitions = any(p.transitions for p in template.phases) or bool(
            template.default_transitions
        )
        _SequencerClass = (  # noqa: N806
            StateMachineSequencer if _has_transitions else PhaseSequencer
        )
        sequencer = _SequencerClass(
            template,
            runner,
            config=initial_input,
            on_phase_complete=_on_phase_complete,
            on_phase_start=_on_phase_start_cb,
            on_pipeline_start=_on_pipeline_start_hook,
            on_pipeline_complete=_on_pipeline_complete_hook,
            output_dir=output_dir,
        )
        # Give the git diff closure access to the sequencer's pipeline_context
        _pipeline_context_ref[0] = sequencer.pipeline_context

        try:
            # Start the non-TTY heartbeat for the duration of pipeline execution.
            # In TTY mode the heartbeat is automatically inactive, so this is a no-op.
            with heartbeat:
                result = sequencer.execute(initial_input)
        except KeyboardInterrupt:
            # SIGINT (#975): mark cancelled, then re-raise so Click emits
            # "Aborted!" with its conventional exit code. KeyboardInterrupt is a
            # BaseException, so neither sequencer.execute's `except Exception`
            # (sequencer.py:235) nor the arm below catches it — this arm must.
            if db is not None:
                try:
                    db.update_pipeline_run(
                        run_id,
                        status="cancelled",
                        completed_at=now_utc().isoformat(),
                        error_message="Cancelled by user (SIGINT)",
                    )
                except Exception as _term_exc:  # noqa: BLE001
                    logger.warning("Cancelled-status write failed (non-fatal): %s", _term_exc)
            if git_ctx:
                git_ctx.cleanup(success=False)
            raise
        except GitError as exc:
            console.print(f"\n[red]✗ Git error:[/red] {exc}", highlight=False)
            if db is not None:
                try:
                    db.update_pipeline_run(
                        run_id,
                        status="failed",
                        completed_at=now_utc().isoformat(),
                        error_message=f"Git error: {exc}",
                    )
                except Exception as _term_exc:  # noqa: BLE001
                    logger.warning("Failed-status write failed (non-fatal): %s", _term_exc)
            if git_ctx:
                git_ctx.cleanup(success=False)
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"✗ Pipeline execution crashed: {exc}", err=True)
            if db is not None:
                try:
                    db.update_pipeline_run(
                        run_id,
                        status="failed",
                        completed_at=now_utc().isoformat(),
                        error_message=f"Pipeline execution crashed: {exc}",
                    )
                except Exception as _term_exc:  # noqa: BLE001
                    logger.warning("Failed-status write failed (non-fatal): %s", _term_exc)
            if git_ctx:
                git_ctx.cleanup(success=False)
            sys.exit(1)

    # --- 5. Report result (Feature #70 — rich summary table) ---------
    if result.get("aborted"):
        failed_phase = result.get("failed_phase", "unknown")
        click.echo(f"✗ Pipeline aborted at phase '{failed_phase}'", err=True)
        click.echo(f"  Completed phases: {[*result['phase_outputs'].keys()]}", err=True)
        if db is not None:
            try:
                db.update_pipeline_run(
                    run_id,
                    status="failed",
                    completed_at=now_utc().isoformat(),
                    error_message=f"Pipeline aborted at phase '{failed_phase}'",
                )
            except Exception as _term_exc:  # noqa: BLE001
                logger.warning("Failed-status write failed (non-fatal): %s", _term_exc)
        sys.exit(2)

    # Pipeline reached a clean end: mark success (#979). Foreground does not run
    # the daemon's confidence-routing, so the terminal status is a plain success.
    if db is not None:
        try:
            db.update_pipeline_run(
                run_id,
                status="success",
                completed_at=now_utc().isoformat(),
            )
        except Exception as _term_exc:  # noqa: BLE001
            logger.warning("Success-status write failed (non-fatal): %s", _term_exc)

    completed_phases = [*result["phase_outputs"].keys()]
    elapsed = _cli.time.time() - run_start

    # Build rich summary table
    table = Table(
        title=f"Pipeline completed — {len(completed_phases)} phases in {elapsed:.1f}s",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Phase", style="cyan", no_wrap=True)
    table.add_column("State", justify="center")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")

    total_tokens = 0
    total_cost = 0.0
    for phase_id in completed_phases:
        safe_id = re.sub(r"[^\w\-]", "_", phase_id)
        out = result["phase_outputs"][phase_id]
        _state = out.get("state", "unknown")
        state = _state.value if hasattr(_state, "value") else str(_state)
        tokens = out.get("tokens_consumed", 0)
        cost = out.get("cost_usd", 0)
        cost_float = float(cost) if cost else 0.0
        cost_str = f"${cost_float:.4f}" if cost else "n/a"
        total_tokens += tokens
        total_cost += cost_float
        state_display = (
            f"[green]✓ {state}[/green]" if state == "success" else f"[red]✗ {state}[/red]"
        )
        table.add_row(safe_id, state_display, str(tokens), cost_str)

    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        "",
        f"[bold]{total_tokens}[/bold]",
        f"[bold]${total_cost:.4f}[/bold]",
    )
    console.print()
    console.print(table)

    # --- 5b. Git merge gate output ------------------------------------
    if _gate_result is not None:
        if _gate_result.status == "awaiting_approval":
            console.print()
            console.print("[bold cyan]═══ Merge Gate ═══════════════════════════[/bold cyan]")
            console.print(f"  Branch ready for review.  Run ID: [bold]{run_id}[/bold]")
            if git_ctx and git_ctx._branch_info:
                console.print(
                    f"  Branch: [bold]{git_ctx._branch_info.branch_name}[/bold]"
                    f" → {git_ctx._branch_info.base_branch}"
                )
            console.print()
            console.print(f"  [green]Approve:[/green]  orch gate approve {run_id}")
            console.print(f"  [red]Reject:[/red]   orch gate reject {run_id}")
            console.print(f"  [cyan]Info:[/cyan]     orch gate info {run_id}")
            console.print("[bold cyan]══════════════════════════════════════════[/bold cyan]")
        elif _gate_result.status == "skipped" and _gate_result.message:
            console.print(f"\n[dim]Git: {_gate_result.message}[/dim]")

    # --- 6. Write outputs to disk (always — default dir if not specified) ---
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[yellow]⚠ Could not create output directory: {exc}[/yellow]", stderr=True)
        sys.exit(0)  # Pipeline succeeded, just can't write

    for phase_id, phase_out in result["phase_outputs"].items():
        safe_id = re.sub(r"[^\w\-]", "_", phase_id)

        # JSON (existing behaviour)
        (output_dir / f"{safe_id}.json").write_text(_json.dumps(phase_out, indent=2, default=str))

        # Markdown per phase (Feature #71)
        # Issue #210: if the sub-agent already wrote a larger file, keep it.
        phase_text = _extract_output_text(phase_out)
        out_path = output_dir / f"{safe_id}.md"
        new_content = f"# Phase: {phase_id}\n\n{phase_text}\n"
        _safe_write_phase_output(out_path, new_content, phase_id)

    # _final_output.json
    (output_dir / "_final_output.json").write_text(
        _json.dumps(result.get("final_output", {}), indent=2, default=str)
    )

    # _final_output.md (Feature #71)
    final_text = _extract_output_text(result.get("final_output", {}))
    (output_dir / "_final_output.md").write_text(f"# Final Output\n\n{final_text}\n")

    # _summary.md (Feature #71)
    run_date = now_utc().strftime("%Y-%m-%d %H:%M:%S")
    summary_lines = [
        f"# Run Summary: {template.name}",
        "",
        f"**Date:** {run_date}",
        f"**Template ID:** {template.id}",
        f"**Mode:** {mode}",
        f"**Elapsed:** {elapsed:.1f}s",
        "",
        "## Phases Completed",
        "",
        "| Phase | State | Tokens | Cost |",
        "|-------|-------|--------|------|",
    ]
    for phase_id in completed_phases:
        out = result["phase_outputs"][phase_id]
        _state = out.get("state", "unknown")
        state = _state.value if hasattr(_state, "value") else str(_state)
        tokens = out.get("tokens_consumed", 0)
        cost = out.get("cost_usd", 0)
        cost_float = float(cost) if cost else 0.0
        cost_str = f"${cost_float:.4f}" if cost else "n/a"
        safe_id = re.sub(r"[^\w\-]", "_", phase_id)
        summary_lines.append(f"| {safe_id} | {state} | {tokens} | {cost_str} |")
    summary_lines += [
        "",
        f"**Total Tokens:** {total_tokens}",
        f"**Total Cost:** ${total_cost:.4f}",
        "",
    ]
    (output_dir / "_summary.md").write_text("\n".join(summary_lines))

    console.print(f"\n[bold]Outputs written to:[/bold] {output_dir}/")

    # --- Auto-scoring (Issue #172) ------------------------------------
    if not skip_scoring and template.scenario:
        from ..git_integration import GitContext as _GitContext  # noqa: PLC0415
        from ..scoring import run_scoring as _run_scoring_auto  # noqa: PLC0415

        # Forward the pipeline executor so that LLM judge criteria are routed
        # through the same authentication path as the pipeline itself (e.g. the
        # OpenClaw subscription token in openclaw mode).  Issue #272.
        _scoring_executor = runner.executors[0] if runner.executors else None
        _scoring_passed: Optional[bool] = None
        _scoring_score_val: Optional[float] = None
        try:
            # Use exit_on_failure=False so we can persist results to the gate
            # file *before* exiting.  We replicate the exit logic below after
            # updating the gate.
            _scoring_passed, _scoring_score_val = _run_scoring_auto(
                template,
                output_dir=output_dir,
                console=console,
                template_file=template_file,
                exit_on_failure=False,
                executor=_scoring_executor,
            )
        except SystemExit as _se:
            # Guard: if scoring unexpectedly exits, treat as error
            _scoring_passed = False
            _scoring_score_val = None

        # Persist scoring results to the gate file (Issue #289)
        if _gate_result is not None and _gate_result.status == "awaiting_approval":
            if _scoring_passed is None:
                _gate_scoring_status = "error"
            elif _scoring_passed:
                _gate_scoring_status = "passed"
            else:
                _gate_scoring_status = "failed"
            try:
                _GitContext.update_gate_scoring(run_id, _gate_scoring_status, _scoring_score_val)
            except Exception as _ge:  # noqa: BLE001
                logger.warning(f"Could not update gate scoring: {_ge}")

        # Now enforce exit on scoring failure (replaces exit_on_failure=True)
        if _scoring_passed is False:
            sys.exit(1)


@main.command("launch")
@click.argument("template_name_or_file")
@click.option(
    "--mode",
    type=click.Choice(["standalone", "openclaw", "openrouter", "dry-run"]),
    default="standalone",
    show_default=True,
    help="Execution mode.",
)
@click.option(
    "--input",
    "input_json",
    default=None,
    help="Pipeline input as a JSON string.",
)
@click.option(
    "--input-file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to a JSON file containing pipeline input.",
)
@click.option(
    "--issue",
    "issue_number",
    type=int,
    default=None,
    help=(
        "GitHub issue number to auto-fetch as pipeline input. "
        "Fetched fields (title, body, labels, assignees, milestone) are "
        "merged as the base input; explicit --input / --input-file keys take "
        "precedence over fetched values."
    ),
)
@click.option(
    "--repo",
    default=None,
    envvar="GITHUB_REPOSITORY",
    help=(
        "Repository slug (owner/repo) used with --issue to fetch issue data. "
        "Defaults to the GITHUB_REPOSITORY environment variable when set."
    ),
)
@click.option(
    "--branch",
    "branch_name_override",
    default=None,
    help="Override the auto-generated branch name (Issue #591).",
)
@click.option(
    "--test-command",
    "test_command_override",
    default=None,
    help="Override the default test command for the pipeline (Issue #591).",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to write phase outputs (created if missing).",
)
@click.option(
    "--gateway-url",
    default=None,
    envvar="OPENCLAW_GATEWAY_URL",
    help="OpenClaw gateway URL for openclaw mode.",
)
@click.option(
    "--gateway-token",
    default=None,
    help="OpenClaw gateway bearer token for openclaw mode (or set OPENCLAW_GATEWAY_TOKEN).",
)
@click.option(
    "--skip-scoring",
    is_flag=True,
    default=False,
    help="Skip auto-scoring even if the template declares a scenario.",
)
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
@click.option(
    "--executor",
    type=click.Choice(["api", "claudecode", "auto"]),
    default="auto",
    show_default=True,
    help="Executor backend for standalone mode: api (ANTHROPIC_API_KEY), claudecode, or auto.",
)
def pipeline_launch(  # noqa: C901
    template_name_or_file: str,
    mode: str,
    input_json: Optional[str],
    input_file: Optional[Path],
    issue_number: Optional[int],
    repo: Optional[str],
    branch_name_override: Optional[str],
    test_command_override: Optional[str],
    output_dir: Optional[Path],
    gateway_url: Optional[str],
    gateway_token: Optional[str],
    skip_scoring: bool,
    db_path: Optional[str],
    executor: str,
) -> None:
    """Launch a pipeline in the background and return immediately.

    Spawns a daemon process that runs the pipeline, then exits.  Use
    'orch status <run-id>' to check progress.

    \b
    Examples:
      orch launch content-pipeline --mode openclaw --input '{"brief": "AI"}'
      orch launch coding-pipeline-v1 --issue 42 --repo owner/repo
      orch launch coding-pipeline-v1 --issue 42 --branch my-feature
      orch status <run-id>
      orch logs <run-id> --follow
      orch wait <run-id>
    """
    import uuid  # noqa: PLC0415

    from ..templates import TemplateEngine  # noqa: PLC0415

    # --- Validate issue_number (Issue #591): must be positive integer ---
    if issue_number is not None and issue_number <= 0:
        click.echo("Error: Issue number must be a positive integer.", err=True)
        sys.exit(1)

    # --executor is only valid with --mode standalone (openclaw/openrouter are incompatible)
    if mode in ("openclaw", "openrouter") and executor != "auto":
        click.echo("Error: --executor is only valid with --mode standalone", err=True)
        sys.exit(1)

    # --- Resolve template (with launch_fmt error messages) ---
    template_file = _resolve_template_arg(template_name_or_file, launch_fmt=True)

    try:
        engine = TemplateEngine()
        template = engine.load_template(template_file)
    except (FileNotFoundError, KeyError, ValueError, yaml.YAMLError) as exc:
        click.echo(f"✗ Template error: {exc}", err=True)
        sys.exit(1)

    errors = engine.validate_template(template)
    # Issue #295: --skip-scoring opts out of the mandatory-scenario check
    if skip_scoring:
        errors = [e for e in errors if "require a scenario" not in e]
    if errors:
        click.echo(f"✗ Template has {len(errors)} error(s):", err=True)
        for err in errors:
            click.echo(f"  • {err}", err=True)
        sys.exit(1)

    # --- Resolve input ---
    if input_file and input_json:
        click.echo("⚠ Both --input and --input-file provided; using --input-file", err=True)

    initial_input: Dict[str, Any] = {}
    if input_file:
        try:
            initial_input = json.loads(input_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(f"✗ Could not read input file: {exc}", err=True)
            sys.exit(1)
    elif input_json:
        try:
            initial_input = json.loads(input_json)
        except json.JSONDecodeError as exc:
            click.echo(f"✗ Invalid JSON in --input: {exc}", err=True)
            sys.exit(1)

    # --- Issue #591: Auto-infer git context + strict issue fetch ---
    if issue_number is not None:
        repo_path_inferred, repo_url_inferred = _cli._infer_git_context()

        # Determine effective_repo for the GitHub API call
        effective_repo = repo  # from --repo flag if provided
        if not effective_repo:
            if repo_url_inferred:
                # Extract owner/repo from the normalized HTTPS URL
                m = re.match(r"https://[^/]+/(.+)", repo_url_inferred)
                effective_repo = m.group(1) if m else None

        if not effective_repo:
            # No --repo flag and no origin remote to infer from
            if repo_path_inferred is None:
                click.echo(
                    "Error: Not inside a git repository. Use --repo to specify the path.",
                    err=True,
                )
            else:
                click.echo(
                    "Error: Cannot determine GitHub repository from git remote. "
                    "Use --repo to specify it.",
                    err=True,
                )
            sys.exit(1)

        # Fetch issue with strict error handling
        issue_raw = _cli._fetch_issue_strict(effective_repo, issue_number)

        # Inject canonical fields (always overwrite, never from --input-file)
        issue_fields = {
            "issue_number": issue_raw["number"],
            "issue_title": issue_raw["title"],
            "issue_body": issue_raw.get("body", ""),
        }
        initial_input.update(issue_fields)

        # Inject repo_path from git root if not already in input
        if repo_path_inferred is not None and "repo_path" not in initial_input:
            initial_input["repo_path"] = repo_path_inferred
        # Note: if --repo provided and CWD is outside git, repo_path_inferred is None
        # repo_path is simply omitted (missing-fields validation catches it if required)

        # Determine repo_url for pipeline input
        if repo:
            # --repo explicitly given → construct HTTPS URL from slug
            initial_input.setdefault("repo_url", f"https://github.com/{repo}")
        elif repo_url_inferred and "repo_url" not in initial_input:
            initial_input["repo_url"] = repo_url_inferred

        # Generate branch_name as default (--branch override applied later)
        if "branch_name" not in initial_input:
            slug = _slugify_title(issue_raw["title"])
            initial_input["branch_name"] = f"fix/{issue_number}-{slug}"

        # --test-command injection
        if test_command_override:
            initial_input["test_command"] = test_command_override

    # --- --branch always wins (over --input-file, auto-generated, issue-inferred) ---
    # This runs unconditionally — not gated on issue_number — so --branch works
    # both with and without --issue (Issue #591).
    if branch_name_override:
        initial_input["branch_name"] = branch_name_override

    # If --test-command was provided without --issue, inject it now
    if test_command_override and issue_number is None:
        initial_input["test_command"] = test_command_override

    # --- Persist executor_type so the daemon can forward it to the runner factory ---
    # Only stored when non-default to avoid polluting input for the common case.
    if executor != "auto":
        initial_input["_executor_type"] = executor

    # --- Dry-run defaults: fill initial_input with template's example_input (Issue #591) ---
    # In dry-run mode the pipeline won't actually execute, but we still validate required fields
    # so that the validation code path (and mock) can be exercised.  Example_input provides
    # safe placeholder values for any fields not already in initial_input.
    if mode == "dry-run":
        example = getattr(template, "example_input", None) or {}
        for k, v in example.items():
            if k not in initial_input:
                initial_input[k] = v

    # --- Gateway token auto-read for openclaw mode (Issue #591) ---
    # NOTE: This runs BEFORE missing-fields validation so that "No gateway token found"
    # is the error shown when mode=openclaw and no token is available — not a fields error.
    if mode == "openclaw":
        effective_token = (
            gateway_token or os.environ.get("OPENCLAW_GATEWAY_TOKEN") or _cli._read_openclaw_token()
        )
        if not effective_token:
            click.echo(
                "No gateway token found. Set OPENCLAW_GATEWAY_TOKEN or check "
                "~/.openclaw/openclaw.json",
                err=True,
            )
            sys.exit(1)
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = effective_token

    # --- Validate required config fields (#411) with new single-line format (#591) ---
    missing = _validate_required_config(template, initial_input)
    # Apply schema defaults for optional fields (#835) — see commentary in
    # the run_template path above for rationale.
    _cli.apply_config_schema_defaults(initial_input, getattr(template, "config_schema", None))
    if missing:
        sorted_missing = sorted(missing)
        click.echo(
            f"Error: Missing required fields: {', '.join(sorted_missing)}",
            err=True,
        )
        sys.exit(1)

    # --- Build run_id and output_dir ---
    run_id = str(uuid.uuid4())[:8]
    if output_dir is None:
        _safe_id = re.sub(r"[^\w\-]", "_", template.id)
        _ts = now_utc().strftime("%Y%m%d-%H%M%S")
        output_dir = Path(f"./output/{_safe_id}-{_ts}-{run_id}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Persist run record to DB ---
    effective_db_path = db_path or _get_persistent_db_path()
    db = _cli.Database(Path(effective_db_path))
    db.insert_pipeline_run(
        {
            "run_id": run_id,
            "template_path": str(template_file.resolve()),
            "template_id": template.id,
            "input_json": json.dumps(initial_input),
            "mode": mode,
            "output_dir": str(output_dir.resolve()),
            "gateway_url": gateway_url,
            "skip_scoring": int(skip_scoring),
            "status": "pending",
        }
    )

    # --- Spawn daemon ---
    log_file_path = output_dir / ".orch-daemon.log"
    log_fh = open(str(log_file_path), "a")

    proc = _cli.subprocess.Popen(
        [sys.executable, "-m", "orchestration_engine.daemon", run_id, effective_db_path],
        start_new_session=True,
        stdout=log_fh,
        stderr=log_fh,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        bufsize=0,
    )
    log_fh.close()

    db.update_pipeline_run(run_id, pid=proc.pid)

    click.echo("✓ Pipeline launched in background")
    click.echo(f"  Run ID:  {run_id}")
    click.echo(f"  PID:     {proc.pid}")
    click.echo(f"  Output:  {output_dir}/")
    click.echo("")
    click.echo(f"  Status:  orch status {run_id}")
    click.echo(f"  Logs:    orch logs {run_id}")
    click.echo(f"  Wait:    orch wait {run_id}")


@main.command("logs")
@click.argument("run_id")
@click.option("--follow", "-f", is_flag=True, default=False, help="Tail the log file.")
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def pipeline_logs(run_id: str, follow: bool, db_path: Optional[str]) -> None:
    """Show daemon logs for a pipeline run.

    \b
    Examples:
      orch logs a3f8c2d1
      orch logs a3f8c2d1 --follow
    """
    effective_db_path = db_path or _get_persistent_db_path()
    db = _cli.Database(Path(effective_db_path))
    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"✗ Run '{run_id}' not found.", err=True)
        sys.exit(1)

    log_path = Path(run["output_dir"]) / ".orch-daemon.log"
    if not log_path.exists():
        click.echo(f"✗ Log file not found: {log_path}", err=True)
        click.echo("  The run may not have started yet or the output dir is missing.")
        sys.exit(1)

    if follow:
        try:
            _cli.subprocess.run(["tail", "-f", str(log_path)])
        except KeyboardInterrupt:
            pass
    else:
        click.echo(log_path.read_text())


@main.command("children")
@click.argument("run_id")
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def pipeline_children(run_id: str, db_path: Optional[str]) -> None:
    """List child pipeline runs spawned by a parent run.

    Prints a table of all child runs (run_id, template_id, chain_depth,
    status) ordered by creation time.  Exits non-zero when the parent run
    ID is not found.

    \b
    Examples:
      orch children a3f8c2d1
      orch children a3f8c2d1 --db-path /tmp/custom.db
    """  # Issue #330.3: children CLI command
    from orchestration_engine.db import Database  # noqa: PLC0415

    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))

    run = db.get_pipeline_run(run_id)
    if run is None:
        click.echo(f"✗ Run '{run_id}' not found.", err=True)
        sys.exit(1)

    children = db.list_pipeline_run_children(run_id)

    if not children:
        click.echo(f"No child runs found for '{run_id}'.")
        return

    # Header
    click.echo(f"{'RUN ID':<36}  {'TEMPLATE':<30}  {'DEPTH':>5}  {'STATUS'}")
    click.echo("-" * 90)
    for child in children:
        click.echo(
            f"{child.get('run_id', ''):<36}  "
            f"{child.get('template_id', ''):<30}  "
            f"{child.get('chain_depth', 0):>5}  "
            f"{child.get('status', '')}"
        )


@main.command("chain")
@click.argument("run_id", required=False, default=None)
@click.option(
    "--active",
    is_flag=True,
    default=False,
    help="List all currently active (non-terminal) chains.",
)
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def pipeline_chain(
    run_id: Optional[str],
    active: bool,
    db_path: Optional[str],
) -> None:
    """Monitor pipeline chain execution status.

    \b
    Examples:
      orch chain a3f8c2d1          # Show full chain for a given run ID
      orch chain --active          # List all currently running chains
      orch chain a3f8c2d1 --db-path /tmp/custom.db
    """  # Issue #508: chain monitoring CLI
    from orchestration_engine import chain_monitor  # noqa: PLC0415
    from orchestration_engine.db import Database  # noqa: PLC0415

    # Validate: exactly one of run_id or --active must be provided
    if not run_id and not active:
        raise click.UsageError(
            "Provide a RUN_ID to inspect a chain, or use --active to list all active chains."
        )
    if run_id and active:
        raise click.UsageError("Cannot use both RUN_ID and --active together.")

    effective_db_path = db_path or _get_persistent_db_path()
    db = Database(Path(effective_db_path))

    if active:
        click.echo(chain_monitor.build_active_chains_display(db))
        return

    # run_id mode: find root, then display full chain
    root = chain_monitor.find_chain_root(db, run_id)
    if root is None:
        click.echo(f"✗ Run '{run_id}' not found.", err=True)
        sys.exit(1)

    root_run_id = root["run_id"]
    if root_run_id != run_id:
        click.echo(f"(Showing chain from root: {root_run_id})")

    click.echo(chain_monitor.build_chain_display(db, root_run_id))


@main.command("wait")
@click.argument("run_id")
@click.option(
    "--timeout",
    type=int,
    default=1800,
    show_default=True,
    help="Maximum seconds to wait before giving up.",
)
@click.option(
    "--interval", type=int, default=3, show_default=True, help="Poll interval in seconds."
)
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def pipeline_wait(run_id: str, timeout: int, interval: int, db_path: Optional[str]) -> None:
    """Block until a pipeline run finishes.

    Exits 0 on success, exits 2 on failure or timeout.

    \b
    Examples:
      orch wait a3f8c2d1
      orch wait a3f8c2d1 --timeout 120
    """
    effective_db_path = db_path or _get_persistent_db_path()
    db = _cli.Database(Path(effective_db_path))

    terminal_states = {
        "success",
        "failed",
        "cancelled",
        "crashed",
        "scoring_failed",
        "pending_review",
        "rejected",
    }
    deadline = _cli.time.time() + timeout
    last_phase = None

    click.echo(f"Waiting for run '{run_id}' (timeout={timeout}s) …")

    while _cli.time.time() < deadline:
        run = db.get_pipeline_run(run_id)
        if run is None:
            click.echo(f"✗ Run '{run_id}' not found.", err=True)
            sys.exit(2)

        current_status = run["status"]

        # Check PID liveness if still running
        if current_status == "running":
            pid = run.get("pid")
            if pid:
                try:
                    from ..daemon import is_process_alive  # noqa: PLC0415

                    if not is_process_alive(pid):
                        current_status = "crashed"
                        db.update_pipeline_run(run_id, status="crashed")
                except Exception:  # noqa: BLE001
                    pass

        current_phase = run.get("current_phase") or "(none)"
        if current_phase != last_phase:
            click.echo(
                f"  [{now_utc().strftime('%H:%M:%S')}] status={current_status}  phase={current_phase}"  # noqa: E501
            )
            last_phase = current_phase

        if current_status in terminal_states:
            click.echo(f"\nRun '{run_id}' finished with status: {current_status}")
            if current_status == "success":
                sys.exit(0)
            else:
                sys.exit(2)

        _cli.time.sleep(interval)

    click.echo(f"\n✗ Timeout after {timeout}s — run '{run_id}' still in progress.", err=True)
    sys.exit(2)


@main.command("resume")
@click.argument("run_id")
def pipeline_resume(run_id: str) -> None:  # noqa: ARG001
    """Resume a failed or crashed pipeline run from the last completed phase.

    This is a v2 feature — not yet implemented.

    \b
    Examples:
      orch resume a3f8c2d1
    """
    click.echo("✗ 'orch resume' is not yet implemented (v2 feature).", err=True)
    click.echo("  To re-run from scratch:  orch launch <template> [options]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Feature #66 — orch start (interactive wizard)
# ---------------------------------------------------------------------------


def _prompt_for_field(
    field_name: str,
    field_info: Dict[str, Any],
    required_fields: set,
    yes: bool,
) -> Optional[str]:
    """Prompt the user for a single config-schema field.

    When *yes* is True, skip the prompt and return the field's default (or "").
    Returns None if the field is optional and the user leaves it blank.
    """
    field_info = field_info or {}
    field_type = field_info.get("type", "string")
    field_desc = field_info.get("description", "")
    field_default = field_info.get("default", None)
    field_enum = field_info.get("enum", None)
    is_required = field_name in required_fields

    # Build label
    req_tag = ", required" if is_required else ""
    label = f"  {field_name} ({field_type}{req_tag})"
    if field_desc:
        label += f": {field_desc}"

    if yes:
        # Non-interactive: use default or empty string
        return (
            str(field_default) if field_default is not None else (None if not is_required else "")
        )

    click.echo(label)

    prompt_text = "  > "
    if field_enum:
        choice = click.Choice(field_enum)
        value = click.prompt(
            prompt_text,
            default=field_default or "",
            type=choice,
            prompt_suffix="",
        )
    elif field_default is not None:
        # Show default in brackets
        click.echo(f"  [default: {field_default}]")
        value = click.prompt(
            prompt_text,
            default=str(field_default),
            show_default=False,
            prompt_suffix="",
        )
    else:
        if is_required:
            value = click.prompt(prompt_text, prompt_suffix="")
        else:
            value = click.prompt(prompt_text, default="", show_default=False, prompt_suffix="")

    click.echo()
    return value if value != "" else (None if not is_required else "")


@main.command("start")
@click.argument("template_name_or_path")
@click.option(
    "--mode",
    type=click.Choice(["standalone", "openclaw", "dry-run"]),
    default="dry-run",
    show_default=True,
    help="Execution mode (dry-run is safe — no API calls).",
)
@click.option(
    "--api-key",
    envvar="ANTHROPIC_API_KEY",
    default=None,
    help="Anthropic API key for standalone mode (or set ANTHROPIC_API_KEY).",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip prompts and use default values for all fields.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to write phase outputs.",
)
@click.pass_context
def pipeline_start(  # noqa: C901
    ctx: click.Context,
    template_name_or_path: str,
    mode: str,
    api_key: Optional[str],
    yes: bool,
    output_dir: Optional[Path],
) -> None:
    """Interactive wizard to fill in a template's inputs and run it.

    TEMPLATE_NAME_OR_PATH can be a template name/ID (e.g. hello-pipeline)
    or a path to a .yaml file.

    Examples:

      # Interactive wizard with defaults:
      orch start hello-pipeline

      # Non-interactive (use all defaults), standalone mode:
      orch start hello-pipeline --mode standalone --yes

      # Point at a local file:
      orch start ./my-template.yaml --mode dry-run
    """
    import json as _json  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415

    console = Console(highlight=False)

    # ---- 1. Find and load template ----
    template_path, template = _find_template(template_name_or_path)

    # ---- 2. Show header ----
    console.print()
    console.print(f"[bold cyan]{template.name}[/bold cyan] " f"[dim](v{template.version})[/dim]")
    if template.description:
        console.print(template.description)
    console.print()

    # ---- 3. Collect inputs from config_schema ----
    config_schema: Dict[str, Any] = template.config_schema or {}
    props: Dict[str, Any] = config_schema.get("properties", {}) or {}
    required_fields: set = set(config_schema.get("required", []))

    collected: Dict[str, str] = {}

    try:
        if props:
            if not yes:
                console.print("[bold]Fill in the pipeline inputs:[/bold]")
                console.print()

            for field_name, field_info in props.items():
                value = _prompt_for_field(field_name, field_info, required_fields, yes)
                if value is not None:
                    collected[field_name] = value
        elif not yes:
            console.print("[dim]This template has no configurable inputs.[/dim]")
            console.print()

        # ---- 4. Summary + confirmation ----
        if collected and not yes:
            console.print("[bold]Summary:[/bold]")
            for k, v in collected.items():
                console.print(f"  {k}: {v}")
            console.print()
            if not click.confirm("Proceed?", default=True):
                click.echo("Aborted.")
                return
    except (click.Abort, KeyboardInterrupt):
        console.print("\n[dim]Aborted.[/dim]")
        return

    # ---- 5. Run via the existing run_template command ----
    input_json_str = _json.dumps(collected) if collected else None

    ctx.invoke(
        run_template,
        template_name_or_file=template_path,
        mode=mode,
        api_key=api_key,
        input_json=input_json_str,
        input_file=None,
        output_dir=output_dir,
        dry_run_delay=0.0,
        dry_run_failure_rate=0.0,
    )


_VALID_MODEL_TIERS = ["haiku", "sonnet", "opus"]
_VALID_THINKING_LEVELS = ["off", "low", "medium", "high"]


def _build_default_phases(n_phases: int) -> List[Dict[str, Any]]:
    """Return a list of minimal phase dicts for --yes / non-interactive mode."""
    phases: List[Dict[str, Any]] = []
    for i in range(1, n_phases + 1):
        phase_id = f"phase-{i}"
        phases.append(
            {
                "id": phase_id,
                "name": f"Phase {i}",
                "description": f"Phase {i} of the pipeline",
                "task_type": "content",
                "model_tier": "sonnet",
                "thinking_level": "low",
                "depends_on": [f"phase-{i - 1}"] if i > 1 else [],
                "timeout_minutes": 30,
                "prompt_template": "Process the following input:\n{input[topic]}\n",
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "result": {"type": "string"},
                    },
                },
            }
        )
    return phases


def _collect_phases_interactive(
    n_phases: int,
    base_phases: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Interactively prompt for each phase's settings.

    Uses *base_phases* (may be empty) as defaults when cloning from an
    existing template.
    """
    phases: List[Dict[str, Any]] = []

    for i in range(1, n_phases + 1):
        base: Dict[str, Any] = base_phases[i - 1] if i <= len(base_phases) else {}

        click.echo(f"\n── Phase {i} of {n_phases} " + "─" * 50)

        phase_id = click.prompt("  Phase ID", default=base.get("id", f"phase-{i}"))
        phase_name = click.prompt("  Phase name", default=base.get("name", f"Phase {i}"))
        phase_desc = click.prompt(
            "  Description",
            default=base.get("description", ""),
            show_default=False,
        )

        model_tier = click.prompt(
            "  Model tier",
            default=base.get("model_tier", "sonnet"),
            type=click.Choice(_VALID_MODEL_TIERS),
        )
        thinking_level = click.prompt(
            "  Thinking level",
            default=base.get("thinking_level", "low"),
            type=click.Choice(_VALID_THINKING_LEVELS),
        )

        # Dependencies: offer to choose from already-defined phases
        previous_ids = [p["id"] for p in phases]
        deps: List[str] = []
        if previous_ids:
            click.echo(f"  Previous phases: {', '.join(previous_ids)}")
            dep_input = click.prompt(
                "  Dependencies (comma-separated IDs, or blank for none)",
                default="",
                show_default=False,
            )
            if dep_input.strip():
                deps = [d.strip() for d in dep_input.split(",") if d.strip()]
                unknown = [d for d in deps if d not in previous_ids]
                if unknown:
                    click.echo(
                        click.style(
                            f"  ⚠ Unknown phase ID(s) {unknown} — added anyway.", fg="yellow"
                        )
                    )
        else:
            # Inherit base deps (if any) that still reference valid IDs
            base_deps = base.get("depends_on") or []
            deps = [d for d in base_deps if d in previous_ids]

        phases.append(
            {
                "id": phase_id,
                "name": phase_name,
                "description": phase_desc,
                "task_type": base.get("task_type", "content"),
                "model_tier": model_tier,
                "thinking_level": thinking_level,
                "depends_on": deps,
                "timeout_minutes": base.get("timeout_minutes", 30),
                "prompt_template": base.get(
                    "prompt_template", "Process the following input:\n{input[topic]}\n"
                ),
                "output_schema": base.get(
                    "output_schema",
                    {"type": "object", "properties": {"result": {"type": "string"}}},
                ),
            }
        )

    return phases
