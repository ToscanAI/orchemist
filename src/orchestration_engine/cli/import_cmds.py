"""Resource-import command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1003, 950c). The ``import``
command group (``orch import plugin-command``) previously lived inline in
``cli/__init__.py``; its body is moved here VERBATIM. The group and its commands
self-register on the shared ``main`` Click group (imported from ``._root``) at
import time via their ``@main.group`` / ``@import_group.command`` decorators, so
the facade only needs to import this module for the registration side effect.
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click
import yaml

from ._root import main
from .templates_cmds import _check_yaml_syntax


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
