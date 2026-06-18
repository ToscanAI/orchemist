"""Template composition — recursive ``extends:`` / ``exclude_phases:`` merge (Issue #704)."""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ._discovery import TemplateNotFoundError

logger = logging.getLogger(__name__)


class _CompositionMixin:
    """``extends:`` resolution and phase-level merge for :class:`TemplateEngine`."""

    # ------------------------------------------------------------------
    # Template composition (Issue #704)
    # ------------------------------------------------------------------

    def _merge_extends(  # noqa: C901
        self,
        child_data: Dict[str, Any],
        extends_id: str,
        exclude_phases: List[str],
        template_path: Path,
        _chain: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Resolve ``extends:``, load the parent (recursively), apply
        ``exclude_phases:`` and field-level phase overrides.

        Returns the merged YAML-equivalent dict with ``extends`` and
        ``exclude_phases`` keys stripped, plus the list of phase IDs that
        were actually removed by ``exclude_phases`` (for diagnostics on the
        loaded :class:`PipelineTemplate`).

        Cycle detection: ``_chain`` tracks the templates currently being
        resolved (leaf-child first).  If ``extends_id`` appears in
        ``_chain``, raises :class:`ValueError` with the full chain.

        Args:
            child_data: Raw YAML-loaded dict for the child template.
            extends_id: Parent template ID (the value of the child's
                        ``extends:`` field).
            exclude_phases: Phase IDs to drop from the parent before merge.
            template_path: Path to the child template, used in error messages.
            _chain: Recursion-internal — list of template IDs already being
                    resolved.  Defaults to ``[child_data["id"]]`` so the
                    leaf child is part of cycle detection.

        Returns:
            ``(merged_data, removed_ids)`` — the merged template dict and the
            actual list of phase IDs that were removed via ``exclude_phases``.

        Raises:
            ValueError: On circular extends chains or malformed types.
            TemplateNotFoundError: When the parent template name cannot be
                                    resolved.
        """
        # Initialise chain with the leaf child id so multi-level cycles are caught.
        if _chain is None:
            leaf_id = child_data.get("id") or "<unnamed-child>"
            _chain = [str(leaf_id)]
        else:
            _chain = list(_chain)

        if extends_id in _chain:
            cycle_path = " -> ".join(_chain + [extends_id])
            raise ValueError(f"Circular extends chain detected: {cycle_path}")
        _chain.append(extends_id)

        # Resolve parent path (raises TemplateNotFoundError with context).
        try:
            parent_path = self.resolve_template(extends_id)
        except TemplateNotFoundError as exc:
            raise TemplateNotFoundError(
                f"{extends_id} (referenced by 'extends:' in {template_path})",
                exc.searched,
            ) from exc

        with open(parent_path) as fh:
            parent_data = yaml.safe_load(fh)
        if parent_data is None:
            raise ValueError(
                f"Parent template referenced by 'extends: {extends_id}' is empty: " f"{parent_path}"
            )

        # Recurse on the parent's own extends (multi-level inheritance).
        # Parent's exclude_phases applies to its OWN parent; we don't propagate
        # the child's exclude_phases up the chain.
        if parent_data.get("extends"):
            parent_extends_raw = parent_data["extends"]
            if not isinstance(parent_extends_raw, str) or not parent_extends_raw.strip():
                raise ValueError(
                    f"Template {parent_path}: 'extends' must be a non-empty string, "
                    f"got: {parent_extends_raw!r}"
                )
            parent_exclude_raw = parent_data.get("exclude_phases") or []
            if not isinstance(parent_exclude_raw, list):
                raise ValueError(
                    f"Template {parent_path}: 'exclude_phases' must be a list of "
                    f"phase IDs, got: {type(parent_exclude_raw).__name__}"
                )
            parent_data, _ = self._merge_extends(
                child_data=parent_data,
                extends_id=parent_extends_raw,
                exclude_phases=[str(x) for x in parent_exclude_raw],
                template_path=parent_path,
                _chain=_chain,
            )

        # Build phase index keyed by id, preserving parent order.
        parent_phases_raw = parent_data.get("phases") or []
        if not isinstance(parent_phases_raw, list):
            raise ValueError(
                f"Parent template {parent_path}: 'phases' must be a list, "
                f"got: {type(parent_phases_raw).__name__}"
            )
        # Ordered dict semantics: phase id -> phase dict (preserves first-seen order).
        parent_phase_map: "Dict[str, Dict[str, Any]]" = {}
        parent_order: List[str] = []
        for p in parent_phases_raw:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if isinstance(pid, str) and pid not in parent_phase_map:
                parent_phase_map[pid] = p
                parent_order.append(pid)

        # Apply exclude_phases — dedupe while preserving first-seen order.
        seen_excl: set = set()
        deduped_excl: List[str] = []
        for excl in exclude_phases:
            if excl not in seen_excl:
                seen_excl.add(excl)
                deduped_excl.append(excl)

        removed_ids: List[str] = []
        for excl in deduped_excl:
            if excl in parent_phase_map:
                del parent_phase_map[excl]
                parent_order = [pid for pid in parent_order if pid != excl]
                removed_ids.append(excl)
            else:
                logger.warning(
                    "Template %s: exclude_phases listed phase %r which is not "
                    "present in parent %r (ignored).",
                    template_path,
                    excl,
                    extends_id,
                )

        # Build merged phases list — parent (post-exclude) first, then walk child
        # phases. For each child phase: if id matches a remaining parent phase,
        # field-level merge in place; else append.
        merged_phases: List[Dict[str, Any]] = [dict(parent_phase_map[pid]) for pid in parent_order]
        # Index of merged_phases by id for in-place override lookup.
        merged_index: Dict[str, int] = {
            entry["id"]: i
            for i, entry in enumerate(merged_phases)
            if isinstance(entry.get("id"), str)
        }

        child_phases_raw = child_data.get("phases") or []
        if not isinstance(child_phases_raw, list):
            raise ValueError(
                f"Template {template_path}: 'phases' must be a list, "
                f"got: {type(child_phases_raw).__name__}"
            )
        for cp in child_phases_raw:
            if not isinstance(cp, dict):
                continue
            cpid = cp.get("id")
            if not isinstance(cpid, str):
                # No id → append verbatim, let downstream validation flag it.
                merged_phases.append(cp)
                continue
            if cpid in merged_index:
                # Field-level shallow merge: child fields override parent fields.
                idx = merged_index[cpid]
                merged_phases[idx] = {**merged_phases[idx], **cp}
            else:
                merged_phases.append(cp)
                merged_index[cpid] = len(merged_phases) - 1

        # Top-level merge: child wins for declared keys; parent supplies the rest.
        merged_top: Dict[str, Any] = {**parent_data, **child_data}
        merged_top["phases"] = merged_phases
        # Strip composition keys — they've been resolved.
        merged_top.pop("extends", None)
        merged_top.pop("exclude_phases", None)

        return merged_top, removed_ids
