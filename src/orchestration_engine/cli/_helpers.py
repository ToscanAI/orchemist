"""Shared helper functions for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #998, 950a). These helpers were
previously module-level functions in ``cli.py``; they are imported back into
``cli/__init__.py`` so the public ``orchestration_engine.cli`` namespace is
byte-identical to the pre-refactor module.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401

import click

from ..db import default_db_path


def format_datetime(dt: Optional[datetime]) -> str:
    """Format datetime for display."""
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: Optional[float]) -> str:
    """Format duration in seconds to human readable format."""
    if seconds is None or seconds == 0:
        return "N/A"

    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"


def print_table(headers: List[str], rows: List[List[str]]) -> None:
    """Print a simple table with headers and rows."""
    if not rows:
        click.echo("No data to display")
        return

    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    # Print header
    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    click.echo(header_line)
    click.echo("-" * len(header_line))

    # Print rows
    for row in rows:
        row_line = " | ".join(
            str(row[i] if i < len(row) else "").ljust(col_widths[i]) for i in range(len(headers))
        )
        click.echo(row_line)


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as Xm Ys or Xs."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m {s}s"


def _get_persistent_db_path() -> str:
    """Return the path to the persistent on-disk DB used by async runs.

    Thin string-returning wrapper around :func:`orchestration_engine.db.default_db_path`
    preserved for callsite signature compatibility (Issue #864 consolidation).
    """
    return str(default_db_path())


def _yaml_str(val: Any) -> str:
    """Convert a YAML-parsed value to string, mapping YAML booleans back to their
    original keyword (e.g. False → 'off', True → 'on')."""
    if val is False:
        return "off"
    if val is True:
        return "on"
    return str(val) if val is not None else ""


def _template_resolution_paths() -> List[tuple]:
    """Return list of (Path, source_label) for template scanning.

    Scanned in order (each directory may or may not exist):
    1. Paths from ``ORCH_TEMPLATES_PATH`` env var (colon-separated)  → "custom"
    2. ``./templates/``   (project-local)                            → "project"
    3. ``./examples/``    (project examples — backward compat)       → "examples"
    4. ``~/.orch/templates/`` (user-global, if it exists)            → "user"

    Labels are consistent with :meth:`TemplateEngine.get_search_paths` (e.g.
    ``"project"`` for ``./templates/`` rather than the old ``"templates"``).

    Note: bundled/package templates are handled by TemplateEngine.resolve_template()
    for name-based lookup but are not listed here to keep the scan focused on
    user-visible template sources.
    """
    import os as _os  # noqa: PLC0415

    paths: List[tuple] = []

    # 1. ORCH_TEMPLATES_PATH (colon-separated)
    env_raw = _os.environ.get("ORCH_TEMPLATES_PATH", "")
    if env_raw:
        for part in env_raw.split(":"):
            part = part.strip()
            if part:
                paths.append((Path(part), "custom"))

    # 2+3. Project-local dirs — use "project" to match TemplateEngine.get_search_paths()
    paths.append((Path("./templates"), "project"))
    paths.append((Path("./examples"), "examples"))

    # 4. User-global (only if it exists, to avoid creating it on scan)
    user_dir = Path.home() / ".orch" / "templates"
    if user_dir.exists():
        paths.append((user_dir, "user"))

    return paths


def _scan_templates(resolution_paths: Optional[List[tuple]] = None) -> List[tuple]:
    """Scan resolution paths for YAML templates.

    Returns:
        List of (filepath, source_label, PipelineTemplate) tuples.
    """
    from ..templates import TemplateEngine  # noqa: PLC0415

    if resolution_paths is None:
        resolution_paths = _template_resolution_paths()

    engine = TemplateEngine()
    found = []
    seen_stems: dict = {}  # stem → first source label

    for search_path, source_label in resolution_paths:
        if not search_path.exists():
            continue
        for filepath in sorted(search_path.glob("*.yaml")) + sorted(search_path.glob("*.yml")):
            stem = filepath.stem
            if stem in seen_stems:
                continue
            try:
                template = engine.load_template(filepath)
                seen_stems[stem] = source_label
                found.append((filepath, source_label, template))
            except Exception as exc:  # noqa: BLE001
                click.echo(f"[warn] Skipping {filepath}: {exc}", err=True)

    return found


def _validate_required_config(template, initial_input: Dict[str, Any]) -> List[str]:
    """Validate that all required config fields are present in the input.

    Checks the template's ``config_schema.required`` list against the keys
    provided in *initial_input*.  Returns a list of missing field names
    (empty list means all required fields are present).
    """
    schema = getattr(template, "config_schema", None)
    if not schema:
        return []
    required = schema.get("required", [])
    if not required:
        return []
    # A required field is only reported missing when it is absent from the
    # input AND the schema has no default for it (#676). Fields that are both
    # required AND defaulted are filled by apply_config_schema_defaults right
    # after this validation, so reporting them as missing is a false positive.
    # Validation still sees the original (pre-default-fill) input — it simply
    # treats a defaulted field as satisfiable. Truly-required fields with no
    # default still error.
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return [
        field
        for field in required
        if field not in initial_input
        and not (isinstance(properties.get(field), dict) and "default" in properties[field])
    ]


def _slugify_title(title: str) -> str:
    """Slugify an issue title for branch name generation (Issue #591).

    Algorithm: lowercase → replace runs of non-alphanumeric chars with hyphen
    → strip leading/trailing hyphens → truncate to 49 chars → strip trailing
    hyphen after truncation → fallback to 'untitled' if empty.

    Note: truncates to 49 chars (not 50) to match acceptance test expectations.
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:49]
    slug = slug.rstrip("-")
    return slug or "untitled"


def _read_openclaw_token() -> Optional[str]:
    """Read gateway token from ~/.openclaw/openclaw.json (Issue #591).

    Returns None if the file is missing, invalid JSON, or the key path
    gateway.auth.token is absent or null.
    """
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        data = json.loads(config_path.read_text())
        return data.get("gateway", {}).get("auth", {}).get("token") or None
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def _normalize_git_url(url: str) -> str:
    """Normalize git remote URL to HTTPS form (Issue #591).

    Handles:
      - SCP-style SSH:   git@github.com:owner/repo.git → https://github.com/owner/repo
      - RFC 3986 SSH:    ssh://git@github.com/owner/repo.git → https://github.com/owner/repo
      - HTTPS:           https://github.com/owner/repo.git → https://github.com/owner/repo
    """
    # SCP-style: git@host:path
    scp_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if scp_match:
        host, path = scp_match.groups()
        return f"https://{host}/{path}"
    # RFC 3986 SSH: ssh://git@host/path
    ssh2_match = re.match(r"ssh://git@([^/]+)/(.+?)(?:\.git)?$", url)
    if ssh2_match:
        host, path = ssh2_match.groups()
        return f"https://{host}/{path}"
    # HTTPS: strip trailing .git
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _infer_git_context() -> tuple:
    """Return (repo_path, repo_url) inferred from CWD (Issue #591).

    repo_url is None when no remote named 'origin' exists.
    Both are None when CWD is not inside a git repository.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        repo_path = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None, None

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
        )
        repo_url = _normalize_git_url(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        repo_url = None

    return repo_path, repo_url


def _fetch_issue_strict(repo: str, issue_number: int) -> dict:
    """Fetch a GitHub issue or exit with a precise error message (Issue #591).

    Distinguishes: missing credentials (no-token message, exit 1) from
    issue-not-found (not-found message, exit 1).
    """
    # Detect missing credentials before API call
    has_env_token = bool(os.environ.get("GITHUB_TOKEN"))
    if not has_env_token:
        try:
            auth_result = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
            )
            if auth_result.returncode != 0:
                click.echo(
                    "Error: No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'.",
                    err=True,
                )
                sys.exit(1)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            click.echo(
                "Error: No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'.", err=True
            )
            sys.exit(1)

    # Fetch issue via GitHub API
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_number}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        click.echo(f"Error: GitHub API request timed out fetching issue #{issue_number}.", err=True)
        sys.exit(1)
    if result.returncode != 0:
        combined = (result.stderr + result.stdout).lower()
        if "404" in combined or "not found" in combined:
            click.echo(
                f"Error: Issue #{issue_number} not found. "
                "Check the issue number and your GITHUB_TOKEN.",
                err=True,
            )
        else:
            click.echo(
                "Error: No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'.", err=True
            )
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        click.echo(f"✗ Invalid JSON from GitHub API: {exc}", err=True)
        sys.exit(1)


def _resolve_template_arg(name_or_path: str, launch_fmt: bool = False) -> Path:
    """Resolve a CLI template argument to a :class:`Path`.

    Accepts:
    * A :class:`Path` object → returned directly (already resolved, e.g. from
      ``ctx.invoke``).
    * A direct file path string (absolute or relative) → existence-checked.
    * A bare template name (e.g. ``content-pipeline``) → resolved via
      :meth:`TemplateEngine.resolve_template`.

    When *launch_fmt* is True (used by ``orch launch``), uses the exact error
    format required by Issue #591: single-line, no tip suffix.

    Exits with an error message on failure.
    """
    import os as _os  # noqa: PLC0415

    from ..templates import TemplateEngine, TemplateNotFoundError  # noqa: PLC0415

    # Already a Path — accept directly
    if isinstance(name_or_path, Path):
        if not name_or_path.exists():
            click.echo(f"✗ Template file not found: {name_or_path}", err=True)
            sys.exit(1)
        return name_or_path

    # Heuristic: treat as a path when it has a path separator or YAML extension
    looks_like_path = (
        name_or_path.endswith(".yaml")
        or name_or_path.endswith(".yml")
        or _os.sep in name_or_path
        or "/" in name_or_path
    )

    if looks_like_path:
        p = Path(name_or_path)
        if not p.exists():
            if launch_fmt:
                click.echo(
                    f"Error: Template not found: {name_or_path}. "
                    "Run 'orch templates list' to see available templates.",
                    err=True,
                )
            else:
                click.echo(f"✗ Template file not found: {name_or_path}", err=True)
            sys.exit(1)
        return p

    # Name-based resolution
    engine = TemplateEngine()
    try:
        return engine.resolve_template(name_or_path)
    except TemplateNotFoundError as exc:
        if launch_fmt:
            click.echo(
                f"Error: Template not found: {name_or_path}. "
                "Run 'orch templates list' to see available templates.",
                err=True,
            )
        else:
            click.echo(f"✗ {exc}", err=True)
            click.echo(
                "\nTip: run 'orch templates list' to see all available templates.",
                err=True,
            )
        sys.exit(1)


def _find_template(name_or_path: str):  # noqa: C901
    """Locate a template by file path OR by name/ID.

    Resolution strategy:
    1. If the argument looks like a path (has separators or .yaml/.yml), load
       it directly.
    2. Exact template ID match (scanning all search paths).
    3. Exact template display-name match (case-insensitive).
    4. :meth:`TemplateEngine.resolve_template` stem-based lookup — only returns
       when the resolved template's ID or name also matches the query exactly
       (prevents false positives when file stem differs from template ID).
    5. Partial/slug matching with suggestions on ambiguous or no match.

    Returns:
        (template_path: Path, template: PipelineTemplate)

    Raises SystemExit on failure.
    """
    import os as _os  # noqa: PLC0415

    from ..templates import TemplateEngine, TemplateNotFoundError  # noqa: PLC0415

    engine = TemplateEngine()

    is_path = (
        name_or_path.endswith(".yaml")
        or name_or_path.endswith(".yml")
        or _os.sep in name_or_path
        or "/" in name_or_path
    )

    if is_path:
        p = Path(name_or_path)
        try:
            template = engine.load_template(p)
            return p, template
        except FileNotFoundError:
            click.echo(f"✗ Template file not found: {name_or_path}")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"✗ Could not load template: {exc}")
            sys.exit(1)

    search = name_or_path.lower()
    found_all = _scan_templates()

    # 1. Exact ID match
    for filepath, _source, tmpl in found_all:
        if tmpl.id.lower() == search:
            return filepath, tmpl

    # 2. Exact name match
    for filepath, _source, tmpl in found_all:
        if tmpl.name.lower() == search:
            return filepath, tmpl

    # 3. Stem-based resolution via resolve_template (respects all search paths).
    #    Only accept the result when the resolved template's ID or display name
    #    also matches the query — avoids returning an unrelated template whose
    #    file stem happens to equal the query but whose logical ID differs.
    try:
        resolved_path = engine.resolve_template(name_or_path)
        template = engine.load_template(resolved_path)
        if (
            template.id.lower() == search
            or template.name.lower() == search
            or template.id.lower().startswith(search + "-")
        ):
            return resolved_path, template
    except TemplateNotFoundError:
        pass

    # 4. Partial match: search string appears in ID or name — suggest, don't auto-resolve
    partial_matches = [
        f"{tmpl.name} (id: {tmpl.id})"
        for _, _source, tmpl in found_all
        if search in tmpl.id.lower() or search in tmpl.name.lower()
    ]

    # Not found — suggest similar
    candidates = partial_matches or [
        f"{tmpl.name} (id: {tmpl.id})"
        for _, _, tmpl in found_all
        if search in tmpl.id.lower() or search in tmpl.name.lower()
    ]
    click.echo(f"✗ Template '{name_or_path}' not found.")
    if candidates:
        click.echo("\nDid you mean one of these?")
        for c in candidates:
            click.echo(f"  • {c}")
    else:
        click.echo(
            "\nNo similar templates found. Run 'orch templates list' to see all.",
        )
    sys.exit(1)
