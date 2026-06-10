"""Chain Monitoring utilities for the Orchestration Engine (Issue #508).

Provides helpers to traverse, format, and display pipeline run chains.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

MAX_ALLOWED_CHAIN_DEPTH = 20


# ---------------------------------------------------------------------------
# Chain traversal helpers
# ---------------------------------------------------------------------------


def find_chain_root(db: Any, run_id: str) -> Optional[Dict[str, Any]]:
    """Walk parent_run_id links upward from run_id to find the chain root.

    Bounded by MAX_ALLOWED_CHAIN_DEPTH to prevent infinite loops on
    malformed data.

    Args:
        db: Open Database instance.
        run_id: Starting run ID; may be the root itself or any descendant.

    Returns:
        Pipeline run dict for the chain root, or None when run_id is not found.
    """
    current = db.get_pipeline_run(run_id)
    if current is None:
        return None

    depth = 0
    while current.get("parent_run_id") and depth < MAX_ALLOWED_CHAIN_DEPTH:
        parent = db.get_pipeline_run(current["parent_run_id"])
        if parent is None:
            break
        current = parent
        depth += 1

    return current


def get_issue_for_run(db: Any, run_id: str) -> Optional[int]:
    """Return the GitHub issue number linked to run_id, or None.

    Looks up issue_pipeline_map via get_issue_pipeline_map_by_run_id and
    returns the issue_number field cast to int.

    Args:
        db: Open Database instance.
        run_id: Pipeline run ID to look up.

    Returns:
        Integer issue number, or None when no mapping exists.
    """
    row = db.get_issue_pipeline_map_by_run_id(run_id)
    if row is None:
        return None
    val = row.get("issue_number")
    return int(val) if val is not None else None


# ---------------------------------------------------------------------------
# Elapsed-time helpers
# ---------------------------------------------------------------------------


def compute_elapsed(run: Dict[str, Any]) -> Optional[float]:
    """Return elapsed wall-clock seconds for a pipeline run, or None.

    Uses completed_at when present; falls back to current UTC time for
    in-progress runs.  Handles both datetime objects and ISO-8601 strings.

    Args:
        run: Pipeline run dict.

    Returns:
        Non-negative float seconds, or None when started_at is absent.
    """
    raw_start = run.get("started_at")
    if not raw_start:
        return None

    def _parse(val: Any) -> Optional[datetime]:
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    start = _parse(raw_start)
    if start is None:
        return None

    raw_end = run.get("completed_at")
    end = _parse(raw_end) if raw_end else None
    if end is None:
        end = datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _fmt_elapsed(seconds: Optional[float]) -> str:
    """Format elapsed seconds as a human-readable string.

    Examples::

        _fmt_elapsed(45)    → "45s"
        _fmt_elapsed(90)    → "1m 30s"
        _fmt_elapsed(3661)  → "1h 1m 1s"
        _fmt_elapsed(None)  → "—"

    Args:
        seconds: Total elapsed seconds, or None.

    Returns:
        Formatted string.
    """
    if seconds is None:
        return "\u2014"  # em dash

    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


# ---------------------------------------------------------------------------
# Row formatting
# ---------------------------------------------------------------------------


def format_chain_row(
    run: Dict[str, Any],
    issue_number: Optional[int] = None,
    depth: int = 0,
) -> str:
    """Format a single run as a display row with indentation.

    Format (fixed-width columns, depth-indented)::

        <indent><run_id[:8]>  #<issue|—>  <status:14>  <score:6>  <elapsed:8>  <template[:24]>

    Args:
        run: Pipeline run dict.
        issue_number: GitHub issue number, or None when not linked.
        depth: Nesting depth (0 = root); drives 2-space-per-level indentation.

    Returns:
        A formatted string line (no trailing newline).
    """
    indent = "  " * depth
    run_id_short = (run.get("run_id") or "")[:8]
    issue_str = f"#{issue_number}" if issue_number is not None else "\u2014"
    status = (run.get("status") or "unknown")[:14]
    score = run.get("scoring_score")
    score_str = f"{score:.3f}" if score is not None else "\u2014"
    elapsed_str = _fmt_elapsed(compute_elapsed(run))
    template = (run.get("template_id") or "")[:24]

    return (
        f"{indent}{run_id_short}  "
        f"{issue_str:<8}  "
        f"{status:<14}  "
        f"{score_str:>6}  "
        f"{elapsed_str:>8}  "
        f"{template}"
    )


# ---------------------------------------------------------------------------
# Multi-run display builders
# ---------------------------------------------------------------------------


def build_chain_display(db: Any, root_run_id: str) -> str:
    """Build the full chain tree display string starting from a given run.

    Finds the chain root (walks up from root_run_id if needed), fetches all
    descendants, and formats them as an indented tree.

    Args:
        db: Open Database instance.
        root_run_id: Any run ID in the chain (root or descendant).

    Returns:
        Multi-line string ready for display, or an error message string
        when root_run_id is not found.
    """
    root = find_chain_root(db, root_run_id)
    if root is None:
        return f"Run '{root_run_id}' not found."

    runs = db.get_full_chain(root["run_id"])
    if not runs:
        return f"No runs found for chain rooted at '{root['run_id']}'."

    # Build depth map from chain_depth column (set by execution engine)
    header = (
        f"{'RUN ID':<10}  {'ISSUE':<8}  {'STATUS':<14}  {'SCORE':>6}  " f"{'ELAPSED':>8}  TEMPLATE"
    )
    separator = "\u2500" * 80
    lines = [header, separator]

    for run in runs:
        depth = int(run.get("chain_depth") or 0)
        issue_number = get_issue_for_run(db, run["run_id"])
        lines.append(format_chain_row(run, issue_number=issue_number, depth=depth))

    return "\n".join(lines)


def build_active_chains_display(db: Any) -> str:
    """Build a display of all currently active chains.

    Shows one section per active chain root: root run ID, template, status,
    chain_depth, and creation timestamp.

    Args:
        db: Open Database instance.

    Returns:
        Multi-line string ready for display, or "No active chains found."
    """
    roots = db.list_active_chain_roots()
    if not roots:
        return "No active chains found."

    header = f"{'ROOT RUN':<10}  {'TEMPLATE':<30}  {'STATUS':<14}  {'DEPTH':>5}  CREATED"
    separator = "\u2500" * 80
    lines = [header, separator]

    for root in roots:
        run_id_short = (root.get("run_id") or "")[:8]
        template = (root.get("template_id") or "")[:30]
        status = (root.get("status") or "")[:14]
        depth = int(root.get("chain_depth") or 0)
        created_at = str(root.get("created_at") or "")[:19]
        lines.append(f"{run_id_short:<10}  {template:<30}  {status:<14}  {depth:>5}  {created_at}")

    return "\n".join(lines)
