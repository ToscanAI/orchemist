"""Command Line Interface for the Orchestration Engine.

Provides CLI commands for task queue management: submit, status, list, cancel, etc.
Uses Click for command structure and rich formatting for output.
"""

# After 950e, NO ``@main.command`` / ``@main.group`` bodies remain inline in this
# module — every command lives in a ``*_cmds`` submodule. The imports below are
# therefore no longer used by inline code; they are kept (unused-import
# suppressed) to preserve the ``orchestration_engine.cli`` public surface
# byte-identically AND because the relocated commands resolve some of them as
# *facade attributes* at call time (the 950b/950c/950e ``_cli.<name>`` late-bind):
#   * ``subprocess`` / ``Database`` / ``time`` — read as ``_cli.subprocess`` /
#     ``_cli.Database`` / ``_cli.time`` by queue_cmds / pipeline_cmds, and patched
#     on this module by the test-suite (EPIC #942 / 950b).
#   * ``apply_config_schema_defaults`` — read as
#     ``_cli.apply_config_schema_defaults`` by pipeline_cmds (EPIC #942 / 950b).
#   * ``Path`` — a documented ``patch("orchestration_engine.cli.Path")`` target
#     (tests/test_cli_launch_shorthand.py); the attribute must exist for patch().
# ``datetime`` / ``timezone`` / ``Decimal`` / ``now_utc`` / the schema enums /
# ``Any`` / ``Dict`` / ``Optional`` / ``yaml`` are retained purely to keep the
# pre-refactor surface stable. (``json`` / ``os`` were dropped in 950d when the
# ``providers`` group — their only remaining inline consumer — moved to
# ``providers_cmds``; neither is part of the facade's test-observed surface.)
import subprocess  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from decimal import Decimal  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Dict, Optional  # noqa: F401

import click  # noqa: F401
import yaml  # noqa: F401

from ..daemon import apply_config_schema_defaults  # noqa: F401
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
# side effect (EPIC #942 / 950b + 950c + 950d + 950e: registration-by-import).
# 950e completes module #950: ``serve_cmds`` (serve / ui / api-server / mcp) and
# ``eval_cmds`` (rubric / scenario / reviews) were the last inline groups, so this
# facade now carries NO ``@main.command`` / ``@main.group`` body of its own. Every
# command remains reachable through ``main`` (``orch <cmd>``) and as an attribute
# of its ``queue_cmds`` / ``pipeline_cmds`` / ``templates_cmds`` / ``import_cmds``
# / ``providers_cmds`` / ``gate_cmds`` / ``admin_cmds`` / ``serve_cmds`` /
# ``eval_cmds`` submodule; no caller imports the command functions by name from
# this facade.
#
# The explicit re-exports keep the names that ARE referenced through the facade:
#  * the test-imported privates ``_print_run_detail`` / ``_watch_pipeline_run`` /
#    ``_print_watch_event`` (see tests/test_cli_watch.py, tests/test_daemon.py +
#    the derived facade gate tests/test_facade_surface_942.py),
#  * ``run_template`` / ``_build_default_phases`` / ``_collect_phases_interactive``
#    (pipeline_cmds), still invoked by the ``new_template`` / ``quickstart``
#    commands now in ``templates_cmds``, and
#  * the templates/import privates the test-suite imports OR patches on this
#    facade — ``_check_yaml_syntax`` / ``_apply_fixes`` / ``_is_github_shorthand``
#    / ``_install_from_git`` / ``_find_yaml_in_dir`` (imported) plus the
#    ``_USER_TEMPLATES_DIR`` / ``_TEMPLATE_INDEX_CACHE`` module-globals (patched;
#    ``templates_cmds`` reads them as ``_cli.<name>`` at call time so the patch on
#    THIS module is what the relocated command bodies observe — EPIC #942 / 950c).
from . import (  # noqa: E402,F401
    admin_cmds,
    eval_cmds,
    gate_cmds,
    import_cmds,
    pipeline_cmds,
    providers_cmds,
    queue_cmds,
    serve_cmds,
    templates_cmds,
)
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
from .templates_cmds import (  # noqa: E402,F401
    _TEMPLATE_INDEX_CACHE,
    _USER_TEMPLATES_DIR,
    _apply_fixes,
    _check_yaml_syntax,
    _find_yaml_in_dir,
    _install_from_git,
    _is_github_shorthand,
)

if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Public facade re-exports (EPIC #942 / issue #998, 950a)
#
# ``orchestration_engine.cli`` must keep exposing the exact pre-refactor surface:
# the ``main`` Click group (the ``orch`` entry point) plus the private internals
# imported by the test-suite. After 950e every command and helper lives in a
# sibling ``*_cmds`` / ``_helpers`` / ``_root`` module imported above; this facade
# defines nothing inline. The names below are re-listed for explicit, self-checking
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
