"""
Chain execution engine for pipeline chaining (Issue #330.2).

After a pipeline run completes, the daemon calls ``evaluate_on_complete()`` to
determine which child pipelines should be spawned, then ``spawn_chain_runs()``
to persist DB records and launch daemon subprocesses.

Public API
----------
- ``interpolate_input_map(input_map, context)`` — placeholder substitution
- ``evaluate_on_complete(template, run, result, final_status)`` — pick children,
  resolve templates, enforce depth
- ``spawn_chain_runs(child_configs, db, db_path, parent_run_id)`` — persist + spawn daemons
"""

import json
import logging
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Regex to match {{placeholder}} and {{dotted.path}} tokens
_PLACEHOLDER_RE = re.compile(r'\{\{(\w+(?:\.\w+)*)\}\}')

# Hard upper-bound safety cap (independent of template max_chain_depth)
MAX_ALLOWED_CHAIN_DEPTH = 20


# ---------------------------------------------------------------------------
# Placeholder interpolation
# ---------------------------------------------------------------------------

def _resolve_dotted(key: str, context: Dict[str, Any]) -> Optional[str]:
    """Resolve a dotted key path against a context dict.

    For example, ``"final_output.summary"`` looks up
    ``context["final_output"]["summary"]``.  Returns ``None`` when any
    segment is missing or the intermediate value is not a dict.

    Args:
        key: Dot-separated key path (e.g. ``"final_output.summary"``).
        context: Context mapping.

    Returns:
        Resolved value as ``str``, or ``None`` if the path is not found.
    """
    parts = key.split(".")
    current: Any = context
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return str(current) if current is not None else None


def interpolate_input_map(
    input_map: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """Interpolate ``{{placeholder}}`` tokens in *input_map* values using *context*.

    Supported placeholders:

    ==================  ================================================
    Token               Resolves to
    ==================  ================================================
    ``{{output_dir}}``  ``context['output_dir']`` (str)
    ``{{run_id}}``      ``context['run_id']`` (str)
    ``{{status}}``      ``context['status']`` (str, e.g. ``'success'``)
    ``{{final_output.key}}``  Nested lookup in ``context['final_output']``
    ``{{any_key}}``     Top-level or dotted lookup in *context*
    ==================  ================================================

    Unknown placeholders are left verbatim (``{{unknown}}`` → ``"{{unknown}}"``)
    so that downstream pipelines can see what was unresolved rather than
    silently receiving empty strings.

    Args:
        input_map: Dict of key → value (possibly containing ``{{...}}`` tokens
                   in string values).
        context: Flat/nested mapping used to resolve placeholders.  Typically
                 built from the parent run's DB record and pipeline result.

    Returns:
        New dict with the same keys but all ``{{...}}`` tokens replaced.
    """
    result: Dict[str, Any] = {}
    for map_key, map_value in input_map.items():
        if not isinstance(map_value, str):
            # Non-string values (int, bool, list, dict) are passed through as-is
            result[map_key] = map_value
            continue

        def _replace(match: re.Match) -> str:  # type: ignore[type-arg]
            placeholder = match.group(1)
            resolved = _resolve_dotted(placeholder, context)
            if resolved is None:
                logger.debug(
                    "interpolate_input_map: unresolved placeholder '{{%s}}' — leaving verbatim",
                    placeholder,
                )
                return match.group(0)  # return original {{...}} unchanged
            return resolved

        result[map_key] = _PLACEHOLDER_RE.sub(_replace, map_value)
    return result


# ---------------------------------------------------------------------------
# Child pipeline evaluation
# ---------------------------------------------------------------------------

def evaluate_on_complete(
    template: Any,
    run: Dict[str, Any],
    result: Dict[str, Any],
    final_status: str,
) -> List[Dict[str, Any]]:
    """Evaluate ``template.on_complete`` and return a list of child run configs.

    For each entry in the appropriate list (``on_complete.success`` or
    ``on_complete.failed``), builds a dict describing the child run to be
    spawned.  Enforces ``max_chain_depth`` to prevent infinite loops.

    Args:
        template: Loaded :class:`~orchestration_engine.templates.PipelineTemplate`.
        run:      DB record of the parent run (as returned by
                  ``db.get_pipeline_run()``).
        result:   Pipeline result dict (sequencer output).  Used to extract
                  ``final_output`` for placeholder interpolation.
        final_status: Terminal status string of the parent run
                      (``'success'``, ``'failed'``, ``'scoring_failed'``, …).

    Returns:
        List of child config dicts, each with the keys expected by
        :func:`spawn_chain_runs`.  Returns an empty list when:

        - ``template.on_complete`` is ``None``
        - The matching entry list is empty
        - The parent's ``chain_depth`` already meets or exceeds
          ``max_chain_depth`` (depth-limit enforcement)
    """
    on_complete = getattr(template, "on_complete", None)
    if on_complete is None:
        return []

    # Determine which entry list to use
    # 'success' maps to the success list; anything else (failed, scoring_failed, …)
    # maps to the failed list.
    if final_status == "success":
        entries = list(on_complete.success or [])
    else:
        entries = list(on_complete.failed or [])

    if not entries:
        return []

    # Depth enforcement
    parent_depth: int = int(run.get("chain_depth") or 0)
    max_depth: int = min(
        int(on_complete.max_chain_depth),
        MAX_ALLOWED_CHAIN_DEPTH,
    )

    if parent_depth >= max_depth:
        logger.warning(
            "Chain depth limit reached (parent_depth=%d, max_chain_depth=%d) "
            "for run '%s' — skipping child pipelines.",
            parent_depth, max_depth, run.get("run_id", "?"),
        )
        return []

    # Build interpolation context from the parent run + result
    final_output_dict: Dict[str, Any] = {}
    raw_final = result.get("final_output", {})
    if isinstance(raw_final, dict):
        inner = raw_final.get("result", raw_final)
        if isinstance(inner, dict):
            final_output_dict = inner

    context: Dict[str, Any] = {
        "run_id": run.get("run_id", ""),
        "output_dir": run.get("output_dir", ""),
        "status": final_status,
        "final_output": final_output_dict,
        # Also expose all parent input keys at top level for convenience
        **_safe_parse_json(run.get("input_json", "{}")),
    }

    child_configs: List[Dict[str, Any]] = []
    child_depth = parent_depth + 1

    for entry in entries:
        resolved_input_map = interpolate_input_map(
            dict(entry.input_map or {}),
            context,
        )

        child_configs.append({
            "template_name": entry.template,
            "input_map": resolved_input_map,
            "chain_depth": child_depth,
            "parent_run_id": run.get("run_id", ""),
            # Inherit mode and gateway settings from the parent run
            "mode": run.get("mode", "dry-run"),
            "gateway_url": run.get("gateway_url"),
            "skip_scoring": bool(run.get("skip_scoring", 0)),
        })

    return child_configs


# ---------------------------------------------------------------------------
# Child pipeline spawning
# ---------------------------------------------------------------------------

def spawn_chain_runs(
    child_configs: List[Dict[str, Any]],
    db: Any,
    db_path: str,
    parent_run_id: str,
) -> List[str]:
    """Persist child run records to the DB and spawn daemon subprocesses.

    For each entry in *child_configs* (as returned by :func:`evaluate_on_complete`):

    1. Resolves the child template path using
       :class:`~orchestration_engine.templates.TemplateEngine`.
    2. Inserts a new row into ``pipeline_runs`` with ``parent_run_id`` and
       ``chain_depth`` set.
    3. Spawns an independent daemon subprocess via ``subprocess.Popen``.

    Failures for individual children are logged and skipped — one bad child
    config should not prevent the other children from being spawned.

    Args:
        child_configs: List of dicts as returned by :func:`evaluate_on_complete`.
        db:            An open :class:`~orchestration_engine.db.Database` instance.
        db_path:       Filesystem path to the DB file (passed to child daemons).
        parent_run_id: The parent run's ID (used for logging).

    Returns:
        List of successfully created child run IDs.
    """
    from .templates import TemplateEngine

    engine = TemplateEngine()
    spawned_run_ids: List[str] = []

    for config in child_configs:
        template_name: str = config.get("template_name", "")
        try:
            template_path = _resolve_template_path(engine, template_name)
        except Exception as exc:
            logger.warning(
                "Chain spawn: could not resolve template '%s' for parent '%s': %s",
                template_name, parent_run_id, exc,
            )
            continue

        # Load template to get its ID
        try:
            child_template = engine.load_template(template_path)
            template_id = child_template.id
        except Exception as exc:
            logger.warning(
                "Chain spawn: could not load template '%s': %s",
                template_name, exc,
            )
            continue

        child_run_id = str(uuid.uuid4())
        child_output_dir = _make_child_output_dir(
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
        )

        run_data: Dict[str, Any] = {
            "run_id": child_run_id,
            "template_path": str(template_path),
            "template_id": template_id,
            "input_json": json.dumps(config.get("input_map", {})),
            "mode": config.get("mode", "dry-run"),
            "output_dir": child_output_dir,
            "status": "pending",
            "gateway_url": config.get("gateway_url"),
            "skip_scoring": int(bool(config.get("skip_scoring", False))),
            "parent_run_id": parent_run_id,
            "chain_depth": int(config.get("chain_depth", 1)),
        }

        # Persist to DB
        try:
            db.insert_pipeline_run(run_data)
            logger.info(
                "Chain spawn: inserted child run '%s' (template='%s', depth=%d, parent='%s')",
                child_run_id, template_name, run_data["chain_depth"], parent_run_id,
            )
        except Exception as exc:
            logger.warning(
                "Chain spawn: could not insert child run for template '%s': %s",
                template_name, exc,
            )
            continue

        # Spawn daemon subprocess
        try:
            _spawn_daemon(child_run_id, db_path)
            logger.info(
                "Chain spawn: daemon started for child run '%s'", child_run_id
            )
            spawned_run_ids.append(child_run_id)
        except Exception as exc:
            logger.warning(
                "Chain spawn: could not start daemon for child run '%s': %s",
                child_run_id, exc,
            )
            # Mark as failed in DB so status queries don't stall
            try:
                from datetime import datetime
                db.update_pipeline_run(
                    child_run_id,
                    status="failed",
                    completed_at=datetime.now().isoformat(),
                    error_message=f"Daemon spawn failed: {exc}",
                )
            except Exception:
                pass

    return spawned_run_ids


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_template_path(engine: Any, template_name: str) -> Path:
    """Resolve a template name to an absolute path.

    Tries ``engine.resolve_template(name)`` first.  If the name looks like
    an absolute or relative path that exists on disk, uses it directly.

    Args:
        engine: A :class:`~orchestration_engine.templates.TemplateEngine` instance.
        template_name: Template name (e.g. ``"notify-pipeline"``) or path.

    Returns:
        Resolved :class:`Path` to the template file.

    Raises:
        FileNotFoundError / TemplateNotFoundError: When the template cannot be found.
    """
    # If it looks like a path (contains / or .yaml/.yml), try as file first
    candidate = Path(template_name)
    if (candidate.suffix in (".yaml", ".yml") or "/" in template_name) and candidate.exists():
        return candidate.resolve()
    # Fall back to engine's name-based resolution
    return engine.resolve_template(template_name)


def _make_child_output_dir(parent_run_id: str, child_run_id: str) -> str:
    """Build an output directory path for a child run.

    Uses ``/tmp/orch-chains/<parent_run_id>/<child_run_id>`` so child
    outputs are co-located under the parent's namespace but isolated.

    Args:
        parent_run_id: The parent pipeline run ID.
        child_run_id:  The newly generated child run ID.

    Returns:
        Absolute path string for the child output directory.
    """
    base = Path("/tmp/orch-chains") / parent_run_id[:8] / child_run_id[:8]
    return str(base)


def _spawn_daemon(run_id: str, db_path: str) -> None:
    """Spawn a background daemon process for *run_id*.

    Launches ``python -m orchestration_engine.daemon <run_id> <db_path>``
    as a detached subprocess, inheriting the current process's environment
    (so ``ANTHROPIC_API_KEY`` / ``OPENCLAW_GATEWAY_TOKEN`` are available).

    Args:
        run_id:  The child run ID.
        db_path: Path to the SQLite DB file.
    """
    cmd = [sys.executable, "-m", "orchestration_engine.daemon", run_id, db_path]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Detach from the parent process group so the child survives
        # even if the parent daemon exits immediately after spawning.
        start_new_session=True,
    )


def _safe_parse_json(raw: Any) -> Dict[str, Any]:
    """Parse *raw* as JSON, returning an empty dict on any error.

    Args:
        raw: String (or anything) to parse.

    Returns:
        Parsed dict, or ``{}`` on failure.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
