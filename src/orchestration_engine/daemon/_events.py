"""Daemon phase-event / SSE / summary writers.

Repo-slug extraction, phase-lifecycle event rows for SSE streaming, the #516
running-phase / completed-phase progress persisters, and the success-path
summary-file writer.  Extracted verbatim from
:mod:`orchestration_engine.daemon` (wave b of #1034); the public surface is
re-exported by the package facade, so callers continue to import these names
from ``orchestration_engine.daemon``.
"""

# ruff: noqa: E501

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..output_utils import (
    extract_output_text as _extract_output_text,
)
from ..timestamps import now_utc

logger = logging.getLogger(__name__)


def _extract_repo_slug(repo_url: str) -> str:
    """Extract an ``owner/repo`` slug from a full GitHub URL or pass-through.

    Converts common GitHub URL formats to a plain ``owner/repo`` slug:

    * ``https://github.com/owner/repo``  →  ``owner/repo``
    * ``https://github.com/owner/repo.git``  →  ``owner/repo``
    * ``git@github.com:owner/repo.git``  →  ``owner/repo``
    * ``owner/repo``  →  ``owner/repo``  (passed through unchanged)

    An empty or unrecognised string is returned as-is so callers can treat an
    empty string as "no repo context available".

    Args:
        repo_url: Raw repository URL or slug from the pipeline input.

    Returns:
        An ``owner/repo`` slug string, or the original value if it could not
        be parsed.
    """
    if not repo_url:
        return ""
    url = repo_url.strip()
    # HTTPS GitHub URL
    if "github.com/" in url:
        idx = url.index("github.com/") + len("github.com/")
        slug = url[idx:].rstrip("/").removesuffix(".git")
        return slug
    # SSH GitHub URL: git@github.com:owner/repo.git
    if url.startswith("git@github.com:"):
        slug = url[len("git@github.com:") :].rstrip("/").removesuffix(".git")
        return slug
    # Already a slug or something else — return as-is
    return url


def _write_phase_event(
    db: Any,
    run_id: str,
    phase_id: str,
    event_type: str,
    phase_result: Optional[Dict[str, Any]] = None,
    tokens_consumed: Optional[int] = None,
    cost_usd: Optional[float] = None,
    state: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a phase lifecycle event to the DB for SSE live-progress streaming.

    Writes a row to ``pipeline_run_events`` so the SSE endpoint can emit
    fine-grained ``phase_started`` / ``phase_completed`` events to connected
    clients.  Failures are logged and swallowed so that a DB write error
    never aborts the pipeline.

    Args:
        db: The :class:`~orchestration_engine.db.Database` instance.
        run_id: The pipeline run identifier.
        phase_id: The phase identifier.
        event_type: One of ``'phase_started'`` or ``'phase_completed'``.
        phase_result: Raw phase result dict (used for metadata).  May be
            ``None`` for ``phase_started`` events.
        tokens_consumed: Override for token count (pre-extracted by caller).
        cost_usd: Override for cost in USD (pre-extracted by caller).
        state: Serialised state string (pre-extracted by caller).
    """
    try:
        metadata: Dict[str, Any] = {}
        if extra_metadata:
            metadata.update(extra_metadata)
        if phase_result:
            # Capture a lightweight summary rather than the full result blob
            result_inner = phase_result.get("result", {})
            if isinstance(result_inner, dict):
                metadata["word_count"] = len(str(result_inner.get("output") or "").split())
        db.insert_pipeline_run_event(
            run_id=run_id,
            event_type=event_type,
            phase_id=phase_id,
            tokens_consumed=tokens_consumed,
            cost_usd=cost_usd,
            state=state,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not write phase event (run=%s phase=%s type=%s): %s",
            run_id,
            phase_id,
            event_type,
            exc,
        )


def _persist_phase_start(db: Any, run_id: str, phase_id: str) -> None:
    """Persist the running phase so ``orch status`` reflects it immediately (#516).

    Touches ONLY ``current_phase`` — ``completed_phases`` and ``phase_outputs``
    are unchanged at phase START. Extracted from the ``_on_phase_start`` closure
    (was ``daemon.py:550``) so the #516 write is execution-path testable.
    """
    db.update_pipeline_run(run_id, current_phase=phase_id)


def _persist_phase_complete(
    db: Any,
    run_id: str,
    phase_id: str,
    completed_phases: List[str],
    phase_outputs: Dict[str, Any],
) -> None:
    """Atomic progress write after a phase completes — mirrors the former
    ``_on_phase_complete`` write (was ``daemon.py:611-617``).

    Sets all three columns in a single ``update_pipeline_run`` call. Receives the
    already-mutated ``completed_phases`` / ``phase_outputs`` from the caller and
    only SERIALIZES them — it does NOT append or mutate. The append
    (``completed_phases.append(phase_id)``) and assignment
    (``phase_outputs[phase_id] = ...``) remain in the caller, BEFORE this call.
    """
    db.update_pipeline_run(
        run_id,
        current_phase=phase_id,
        completed_phases=json.dumps(completed_phases),
        phase_outputs=json.dumps(phase_outputs, default=str),
    )


def _write_summary(
    output_dir: Path,
    template: Any,
    result: Dict[str, Any],
    mode: str,
    run_id: str,
) -> None:
    """Write _final_output.json, _final_output.md, and _summary.md."""
    completed_phases = list(result.get("phase_outputs", {}).keys())
    run_date = now_utc().strftime("%Y-%m-%d %H:%M:%S")

    # _final_output.json
    (output_dir / "_final_output.json").write_text(
        json.dumps(result.get("final_output", {}), indent=2, default=str)
    )

    # _final_output.md
    final_text = _extract_output_text(result.get("final_output", {}))
    (output_dir / "_final_output.md").write_text(f"# Final Output\n\n{final_text}\n")

    # _summary.md
    total_tokens = 0
    total_cost = 0.0
    summary_lines = [
        f"# Run Summary: {template.name}",
        "",
        f"**Date:** {run_date}",
        f"**Run ID:** {run_id}",
        f"**Template ID:** {template.id}",
        f"**Mode:** {mode}",
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
        total_tokens += tokens
        total_cost += cost_float
        summary_lines.append(f"| {safe_id} | {state} | {tokens} | {cost_str} |")
    summary_lines += [
        "",
        f"**Total Tokens:** {total_tokens}",
        f"**Total Cost:** ${total_cost:.4f}",
        "",
    ]
    (output_dir / "_summary.md").write_text("\n".join(summary_lines))
