"""Template-management & scaffolding command groups for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1003, 950c). The ``templates``
group (list / info / install / uninstall / search / test) plus the top-level
``validate``, ``list-phases``, ``quickstart`` and ``new`` commands previously
lived inline in ``cli/__init__.py``; their bodies are moved here VERBATIM. Each
command self-registers on the shared ``main`` Click group (imported from
``._root``) at import time via its ``@main.command`` / ``@templates.command``
decorator, so the facade only needs to import this module for the registration
side effect.

Several module-globals and helpers below (``_USER_TEMPLATES_DIR``,
``_TEMPLATE_INDEX_CACHE``, ``_install_from_git``, ``_find_yaml_in_dir``) are
patched by the existing test-suite via ``patch("orchestration_engine.cli.<name>")``
and then exercised through these (now-relocated) commands. To keep those patches
effective, the command bodies resolve those names as *call-time attributes of the
``orchestration_engine.cli`` facade* (``_cli.<name>``) rather than as direct
locals — identical to the 950b ``queue_cmds`` / ``pipeline_cmds`` approach. The
attributes are read at call time, so the partially-initialised facade module
during package import is never a problem.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import yaml

# Call-time facade reference so tests that
# ``patch("orchestration_engine.cli._USER_TEMPLATES_DIR" | "._TEMPLATE_INDEX_CACHE"
#  | "._install_from_git" | "._find_yaml_in_dir")`` keep intercepting the relocated
# command bodies (EPIC #942 / 950c; see module docstring).
import orchestration_engine.cli as _cli  # noqa: E402  (call-time facade ref; see note)

from ._helpers import (
    _find_template,
    _resolve_template_arg,
    _scan_templates,
    _template_resolution_paths,
    _yaml_str,
)
from ._root import main
from .pipeline_cmds import (
    _build_default_phases,
    _collect_phases_interactive,
    run_template,
)


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


@main.group()
def templates() -> None:
    """Browse and inspect pipeline templates."""


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

    dest = _cli._USER_TEMPLATES_DIR / re.sub(r"[^\w\-]", "_", name)

    if dest.exists():
        if not force:
            raise click.ClickException(
                f"Template '{name}' already installed at {dest}.\n" f"  Use --force to overwrite."
            )
        import shutil  # noqa: PLC0415

        shutil.rmtree(dest)

    _cli._USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

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
        dest = _cli._install_from_git(source, install_name, force)

    elif is_shorthand:
        # GitHub shorthand → https://github.com/user/repo
        url = f"https://github.com/{source}.git"
        install_name = name or source.rsplit("/", maxsplit=1)[-1]
        console.print(f"[bold]Installing from GitHub:[/bold] {source}")
        dest = _cli._install_from_git(url, install_name, force)

    elif is_local:
        # Local YAML file — copy to ~/.orch/templates/
        local_path = Path(source)
        if not local_path.exists():
            raise click.ClickException(f"File not found: {source}")

        install_name = name or local_path.stem
        safe_name = re.sub(r"[^\w\-]", "_", install_name)
        dest = _cli._USER_TEMPLATES_DIR / safe_name

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
    yaml_path = _cli._find_yaml_in_dir(dest)
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
    dest = _cli._USER_TEMPLATES_DIR / safe_name

    if not dest.exists():
        raise click.ClickException(f"Template '{name}' not found in {_cli._USER_TEMPLATES_DIR}")

    if not force:
        if not click.confirm(f"Remove template '{name}' from {dest}?"):
            click.echo("Aborted.")
            return

    shutil.rmtree(dest)
    click.echo(f"✓ Template '{name}' uninstalled.")


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

    if not refresh and TemplateIndex.is_cache_fresh(_cli._TEMPLATE_INDEX_CACHE):
        try:
            index.load_local(_cli._TEMPLATE_INDEX_CACHE)
            loaded = True
        except Exception:  # noqa: BLE001
            pass  # Fall through to remote fetch

    if not loaded:
        try:
            click.echo(f"Fetching index from {url} …", err=True)
            index.load_remote(url)
            try:
                index.save_cache(_cli._TEMPLATE_INDEX_CACHE)
            except Exception:  # noqa: BLE001
                pass  # Cache save failure is non-fatal
        except Exception as exc:  # noqa: BLE001
            # If remote fails but we have a stale cache, use it
            if _cli._TEMPLATE_INDEX_CACHE.exists():
                click.echo(
                    f"⚠  Remote fetch failed ({exc}); using stale cache.",
                    err=True,
                )
                index.load_local(_cli._TEMPLATE_INDEX_CACHE)
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
