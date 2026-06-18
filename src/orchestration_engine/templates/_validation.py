"""Template validation — structural checks, chain-DAG, and extended linting."""

import difflib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..routing import RoutingEngine
from ._config import OnCompleteConfig, OnCompleteEntry, _is_within_dir
from ._models import PipelineTemplate

logger = logging.getLogger(__name__)


class _ValidationMixin:
    """Structural validation, chain-DAG cycle detection, and linting."""

    def validate_template(self, template: PipelineTemplate) -> List[str]:  # noqa: C901
        """Validate a pipeline template for structural errors.

        Checks performed:
        - Required fields present (id, name — already enforced by load_template)
        - No duplicate phase IDs
        - All depends_on references point to existing phase IDs
        - No circular dependencies

        Returns:
            List of human-readable error strings. Empty list means valid.
        """
        errors: List[str] = []
        phase_ids: Dict[str, int] = {}  # id -> first-seen index

        for idx, phase in enumerate(template.phases):
            if phase.id in phase_ids:
                errors.append(
                    f"Duplicate phase ID '{phase.id}' "
                    f"(first at index {phase_ids[phase.id]}, again at index {idx})"
                )
            else:
                phase_ids[phase.id] = idx

        all_ids = set(phase_ids.keys())

        for phase in template.phases:
            for dep in phase.depends_on:
                if dep not in all_ids:
                    errors.append(  # noqa: PERF401
                        f"Phase '{phase.id}' depends on unknown phase '{dep}'"
                    )

        # --- parallel_group validation (#988) ----------------------------
        # A group-bearing phase fans its members out CONCURRENTLY via the #102
        # _execute_wave_parallel core. Members MUST be non-loop (the race in
        # StateMachineSequencer._build_phase_input over _current_build_iter is
        # only safe when max_iterations==0) and MUST NOT themselves declare a
        # parallel_group (nesting is deferred, #988b). All member ids must exist.
        for phase in template.phases:
            for member_id in phase.parallel_group:
                if member_id not in all_ids:
                    errors.append(
                        f"Phase '{phase.id}': parallel_group member '{member_id}' "
                        f"does not exist (known phases: {sorted(all_ids)})"
                    )
                    continue
                member = template.phases[phase_ids[member_id]]
                if member.max_iterations > 0:
                    errors.append(
                        f"Phase '{phase.id}': parallel_group member '{member_id}' "
                        f"is a loop phase (max_iterations={member.max_iterations}); "
                        f"group members must be non-loop (max_iterations==0). "
                        f"Loop members are deferred (#988)."
                    )
                if member.parallel_group:
                    errors.append(
                        f"Phase '{phase.id}': parallel_group member '{member_id}' "
                        f"itself declares a parallel_group; nested groups are not "
                        f"supported (deferred #988b)."
                    )

        # Check for cycles only when there are no missing-dep errors
        # (missing deps can make the cycle detector give false positives)
        dep_errors = [e for e in errors if "depends on unknown" in e]

        # Compute execution order once for reuse (cycle check + Rule 6)
        execution_order: List[List[str]] = []
        if not dep_errors:
            execution_order = self.get_execution_order(template)
            ordered_ids = {pid for wave in execution_order for pid in wave}
            missing_from_order = all_ids - ordered_ids
            if missing_from_order:
                errors.append(
                    f"Cycle detected involving phase(s): " f"{sorted(missing_from_order)}"
                )

        # --- Transition graph validation (Issue #232) -----------------

        # Rule 2: Build effective transitions via per-key merge semantics
        effective_transitions = self._compute_effective_transitions(template)

        # All phase IDs that appear as transition targets anywhere
        all_transition_targets: Set[str] = set()
        for phase_effective in effective_transitions.values():
            all_transition_targets.update(phase_effective.values())

        # Phases that have non-empty effective transitions
        phases_with_transitions: Set[str] = {
            pid for pid, eff in effective_transitions.items() if eff
        }

        # Rule 1: All transition targets must be known phase IDs
        # Issue #704: enrich error message when target was excluded via exclude_phases.
        excluded_ids: Set[str] = set(template.excluded_phase_ids or [])
        for phase in template.phases:
            eff = effective_transitions[phase.id]
            for outcome, target_id in eff.items():
                if target_id not in all_ids:
                    if target_id in excluded_ids:
                        errors.append(
                            f"Phase '{phase.id}': transition target '{target_id}' "
                            f"for outcome '{outcome}' refers to an excluded phase "
                            f"(removed by exclude_phases). Either remove the "
                            f"transition or stop excluding the phase."
                        )
                    else:
                        errors.append(
                            f"Phase '{phase.id}': transition target '{target_id}' "
                            f"for outcome '{outcome}' does not exist "
                            f"(known phases: {sorted(all_ids)})"
                        )

        # Rule 4: max_iterations must be > 0 at pipeline level when any
        # transitions are declared.
        # Note: PipelineTemplate.__post_init__ already clamps max_iterations
        # to at least 1, so this check is a defensive guard for any future
        # change that removes that clamping.
        if phases_with_transitions and template.max_iterations < 1:
            errors.append(
                f"Pipeline '{template.id}' has transition phases but "
                f"max_iterations={template.max_iterations} — "
                f"must be > 0 when transitions are declared."
            )

        # Rule 6: At most one phase per parallel wave may have transitions.
        # Only checked when dep-resolution is clean (same guard as Rule cycle
        # detection above) to avoid misleading wave groupings.
        #
        # Pure state-machine templates (no phase has depends_on) are exempt
        # from this rule: they are designed for StateMachineSequencer where
        # all phases share wave 0 and routing is handled via transitions, not
        # parallel wave execution.  Applying the parallel-wave constraint to
        # such templates would produce false positives (Issue #301).
        is_pure_state_machine = all(not phase.depends_on for phase in template.phases)
        if not dep_errors and execution_order and not is_pure_state_machine:
            for wave_index, wave in enumerate(execution_order):
                transition_phases_in_wave = [pid for pid in wave if pid in phases_with_transitions]
                if len(transition_phases_in_wave) > 1:
                    errors.append(
                        f"Wave {wave_index} contains multiple transition phases "
                        f"{sorted(transition_phases_in_wave)} — at most one phase "
                        f"per parallel wave may have transitions."
                    )

        # Validate git config if present
        if template.git_config is not None and template.git_config.enabled:
            gc = template.git_config
            for cp in gc.commit_phases:
                if cp not in all_ids:
                    errors.append(  # noqa: PERF401
                        f"git.commit_phases references unknown phase '{cp}' "
                        f"(known phases: {sorted(all_ids)})"
                    )

        # Check for empty prompt_template (postmortem fix 2026-02-26)
        # Exception: command and acceptance_run phases use engine dispatch instead of a prompt
        # command (#190), acceptance_run (#532)
        _NO_PROMPT_TASK_TYPES = {"command", "acceptance_run"}  # noqa: N806
        for phase in template.phases:
            if phase.task_type in _NO_PROMPT_TASK_TYPES:
                continue  # these phases are engine-executed, not LLM-prompted
            if not phase.prompt_template or not phase.prompt_template.strip():
                errors.append(
                    f"Phase '{phase.id}' has empty prompt_template — "
                    f"every phase must define a prompt."
                )

        # Light shape check for the opt-in acceptance matrix (#985): each entry
        # must be a dict carrying both ``name`` and ``command``.
        for phase in template.phases:
            for idx, entry in enumerate(phase.acceptance_matrix or []):
                if not isinstance(entry, dict) or not entry.get("name") or not entry.get("command"):
                    errors.append(
                        f"Phase '{phase.id}' acceptance_matrix[{idx}] must be a "
                        f"dict with non-empty 'name' and 'command' keys."
                    )

        # Check that all skill_ref files exist (with path traversal protection)
        template_dir = template.template_path.parent if template.template_path is not None else None
        global_skills_dir = (Path.home() / ".orch" / "skills").resolve()
        for phase in template.phases:
            for skill_ref in phase.skill_refs:
                skill_path = Path(skill_ref)

                # Build allowed directories for this ref (mirrors _load_skill logic).
                # Absolute paths → only global skills dir.
                # Relative paths → global skills dir + template_dir (if set).
                if skill_path.is_absolute():
                    allowed_dirs = [global_skills_dir]
                else:
                    allowed_dirs = [global_skills_dir]
                    if template_dir is not None:
                        allowed_dirs.append(template_dir.resolve())

                # Resolve relative paths against template dir first, then global skills dir
                resolved = None
                if skill_path.is_absolute() and skill_path.exists():
                    resolved = skill_path.resolve()
                elif template_dir is not None:
                    candidate = template_dir / skill_path
                    if candidate.exists():
                        resolved = candidate.resolve()
                if resolved is None:
                    candidate_global = global_skills_dir / skill_path
                    if candidate_global.exists():
                        resolved = candidate_global

                if resolved is None:
                    errors.append(
                        f"Phase '{phase.id}': skill_ref file not found: '{skill_ref}' "
                        f"(looked in template dir and ~/.orch/skills/)"
                    )
                    continue

                # Path traversal protection: reject refs that escape allowed dirs
                resolved_real = resolved.resolve()
                if not any(_is_within_dir(resolved_real, d) for d in allowed_dirs):
                    errors.append(
                        f"Phase '{phase.id}': skill_ref '{skill_ref}' resolves to "
                        f"'{resolved_real}', which is outside the allowed directories "
                        f"({[str(d) for d in allowed_dirs]}). "
                        f"Path traversal is not permitted."
                    )

        # Issue #295: scenario field is optional — auto-scoring is skipped when absent.
        # Scenarios can still be invoked explicitly via `orch scenario run`.

        # Issue #330.1: Validate on_complete block structure
        if template.on_complete is not None:
            oc = template.on_complete
            if not isinstance(oc, OnCompleteConfig):
                errors.append("on_complete must be an OnCompleteConfig instance")
            else:
                for list_name, entry_list in (("success", oc.success), ("failed", oc.failed)):
                    if not isinstance(entry_list, list):
                        errors.append(f"on_complete.{list_name} must be a list")
                        continue
                    for idx, entry in enumerate(entry_list):
                        if not isinstance(entry, OnCompleteEntry):
                            errors.append(
                                f"on_complete.{list_name}[{idx}] must be an OnCompleteEntry"
                            )
                            continue
                        if not entry.template or not isinstance(entry.template, str):
                            errors.append(
                                f"on_complete.{list_name}[{idx}]: template must be a non-empty string"  # noqa: E501
                            )
                        if not isinstance(entry.input_map, dict):
                            errors.append(
                                f"on_complete.{list_name}[{idx}]: input_map must be a dict"
                            )
                if not isinstance(oc.max_chain_depth, int) or oc.max_chain_depth < 1:
                    errors.append(
                        f"on_complete.max_chain_depth must be a positive integer, "
                        f"got: {oc.max_chain_depth!r}"
                    )

                # Issue #330.3: Self-referential on_complete is a static error
                for list_name, entry_list in (("success", oc.success), ("failed", oc.failed)):
                    if not isinstance(entry_list, list):
                        continue
                    for idx, entry in enumerate(entry_list):
                        if not isinstance(entry, OnCompleteEntry):
                            continue
                        if entry.template == template.id:
                            errors.append(
                                f"on_complete.{list_name}[{idx}]: template '{entry.template}' "
                                f"references this template itself (self-referential chain is an error)"  # noqa: E501
                            )

        # Issue #331.2: Validate routing_config threshold integrity
        if template.routing_config is not None:
            routing_errors = RoutingEngine(template.routing_config).validate_thresholds()
            for re_msg in routing_errors:
                errors.append(f"routing_config: {re_msg}")  # noqa: PERF401

        # #987: per-phase tier-band sanity (structural ERROR — inverted band is unsatisfiable).
        # Compare via TIER_ORDER.index (NOT string compare); resolve only when the field is
        # set so an unknown bound (handled as a WARN in validate_template_extended) doesn't
        # crash the comparison and doesn't double-fire here.
        from ..model_registry import TIER_ORDER, resolve_tier  # noqa: PLC0415

        for phase in template.phases:
            lo_t = resolve_tier(phase.min_tier) if phase.min_tier else None
            hi_t = resolve_tier(phase.max_tier) if phase.max_tier else None
            if (
                lo_t is not None
                and hi_t is not None
                and TIER_ORDER.index(lo_t) > TIER_ORDER.index(hi_t)
            ):
                errors.append(
                    f"Phase '{phase.id}' has min_tier='{phase.min_tier}' above "
                    f"max_tier='{phase.max_tier}' (min_tier must be <= max_tier)"
                )

        return errors

    # ------------------------------------------------------------------
    # Chain DAG validation  (Issue #330.3)
    # ------------------------------------------------------------------

    def validate_chain_dag(self, template: "PipelineTemplate") -> List[str]:  # noqa: C901
        """Validate the full chain graph rooted at *template* for cycles.

        Traces all transitive ``on_complete`` references by loading each
        referenced template using the engine's configured search paths.
        Cycles are detected via depth-first search.

        Self-referential entries (template → itself) are reported as a cycle
        even when the entry is the only reference.

        Args:
            template: Entry-point template whose ``on_complete`` graph is traced.

        Returns:
            List of human-readable error strings.  An empty list means the
            DAG is acyclic.  Unresolvable template references are skipped
            with a warning (not treated as a DAG error).
        """
        errors: List[str] = []

        # adjacency: template_id → list of referenced template ids
        graph: Dict[str, List[str]] = {}
        # cache of loaded templates so we don't re-load on revisit
        loaded: Dict[str, "PipelineTemplate"] = {}

        def _get_children(tpl: "PipelineTemplate") -> List[str]:
            """Return the list of template IDs referenced in on_complete."""
            if tpl.on_complete is None or not isinstance(tpl.on_complete, OnCompleteConfig):
                return []
            children: List[str] = []
            for entry_list in (tpl.on_complete.success, tpl.on_complete.failed):
                if not isinstance(entry_list, list):
                    continue
                for entry in entry_list:
                    if isinstance(entry, OnCompleteEntry) and entry.template:
                        children.append(entry.template)  # noqa: PERF401
            return children

        def _load_and_cache(template_id: str) -> Optional["PipelineTemplate"]:
            """Try to resolve and load *template_id*, returning None on failure."""
            if template_id in loaded:
                return loaded[template_id]
            try:
                path = self.resolve_template(template_id)
                tpl = self.load_template(path)
                loaded[tpl.id] = tpl
                return tpl
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "validate_chain_dag: could not load template '%s': %s",
                    template_id,
                    exc,
                )
                return None

        # Seed graph with the entry-point template
        loaded[template.id] = template
        to_explore = [template.id]

        while to_explore:
            tid = to_explore.pop()
            if tid in graph:
                continue
            tpl = loaded.get(tid) or _load_and_cache(tid)
            if tpl is None:
                graph[tid] = []
                continue
            children = _get_children(tpl)
            graph[tid] = children
            for child_id in children:
                if child_id not in graph:
                    to_explore.append(child_id)  # noqa: PERF401

        # DFS cycle detection
        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        cycles_found: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles_found.append(cycle)
            path.pop()
            rec_stack.discard(node)

        for node in sorted(graph):
            if node not in visited:
                dfs(node, [])

        for cycle in cycles_found:
            errors.append(f"Cycle detected in chain DAG: {' → '.join(cycle)}")  # noqa: PERF401

        return errors

    # ------------------------------------------------------------------
    # Extended / linting validation  (Feature #74)
    # ------------------------------------------------------------------

    #: Known valid model tier names.
    KNOWN_MODEL_TIERS: List[str] = ["haiku", "sonnet", "opus"]

    #: Known valid thinking level values.
    KNOWN_THINKING_LEVELS: List[str] = ["off", "low", "medium", "high"]

    #: Known valid per-phase provider names (#969, v1.1). gemini/claudecode/
    #: openclaw are NOT per-phase providers (no run-mode factory / per-phase
    #: credential story); naming them in ``provider:`` is a validation error.
    KNOWN_PROVIDERS: List[str] = ["anthropic", "openrouter"]

    def validate_template_extended(  # noqa: C901
        self,
        template: "PipelineTemplate",
        raw_data: Dict[str, Any],
    ) -> Tuple[List[str], List[str]]:
        """Perform deep linting checks on a loaded template.

        Checks performed:
        - Variable references in ``prompt_template`` fields point to existing
          phase IDs  (``{phase_id.output}`` pattern).
        - ``model_tier`` values are in :attr:`KNOWN_MODEL_TIERS`.
        - ``thinking_level`` values are in :attr:`KNOWN_THINKING_LEVELS`.
        - ``config_schema`` (if present) has at least a ``type`` key; when
          ``type`` is ``"object"`` it should also have ``properties``.

        Args:
            template: Already-loaded :class:`PipelineTemplate`.
            raw_data:  The raw ``dict`` returned by ``yaml.safe_load`` so we
                       can inspect fields that are normalised away by the
                       dataclass constructors.

        Returns:
            ``(errors, warnings)`` — each a list of human-readable strings.
            *errors* indicate definite problems; *warnings* are advisory.
        """
        errors: List[str] = []
        warnings: List[str] = []

        # #987: tier ordering + name resolution for the bound lint checks below.
        from ..model_registry import TIER_ORDER, resolve_tier  # noqa: PLC0415

        phase_ids = {p.id for p in template.phases}

        for phase in template.phases:
            # ---- variable reference check ----------------------------
            prompt = phase.prompt_template or ""
            # Match {some_identifier.output} or {some_identifier.something}
            # but NOT built-ins {input}, {previous_output}, {input[key]}
            for match in re.finditer(
                r"\{([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\}", prompt
            ):
                ref_phase = match.group(1)
                if ref_phase not in phase_ids:
                    warnings.append(
                        f"Phase '{phase.id}' references unknown phase "
                        f"'{ref_phase}' in prompt_template ('{match.group(0)}')"
                    )

            # ---- model_tier check ------------------------------------
            tier = phase.model_tier or ""
            if tier and tier not in self.KNOWN_MODEL_TIERS:
                suggestion = difflib.get_close_matches(
                    tier, self.KNOWN_MODEL_TIERS, n=1, cutoff=0.4
                )
                hint = f"; did you mean '{suggestion[0]}'?" if suggestion else ""
                warnings.append(f"Phase '{phase.id}' has unknown model_tier='{tier}'{hint}")

            # ---- min_tier / max_tier checks (#987) — WARN, mirroring model_tier ----
            for _field_name in ("min_tier", "max_tier"):
                _val = getattr(phase, _field_name, None) or ""
                if _val and _val not in self.KNOWN_MODEL_TIERS:
                    suggestion = difflib.get_close_matches(
                        _val, self.KNOWN_MODEL_TIERS, n=1, cutoff=0.4
                    )
                    hint = f"; did you mean '{suggestion[0]}'?" if suggestion else ""
                    warnings.append(f"Phase '{phase.id}' has unknown {_field_name}='{_val}'{hint}")

            # ---- authored model_tier within [min_tier, max_tier] (#987) — WARN ----
            # The clamp self-heals an out-of-band model_tier at dispatch, so this is a
            # likely authoring mistake worth surfacing, NOT a build-failing error.
            # Compare only when each value resolves to a KNOWN tier.
            _mt = resolve_tier(phase.model_tier) if phase.model_tier else None
            _lo = resolve_tier(phase.min_tier) if phase.min_tier else None
            _hi = resolve_tier(phase.max_tier) if phase.max_tier else None
            if _mt is not None:
                if _lo is not None and TIER_ORDER.index(_mt) < TIER_ORDER.index(_lo):
                    warnings.append(
                        f"Phase '{phase.id}' model_tier='{phase.model_tier}' is below "
                        f"min_tier='{phase.min_tier}' (will be raised to the floor at dispatch)"
                    )
                if _hi is not None and TIER_ORDER.index(_mt) > TIER_ORDER.index(_hi):
                    warnings.append(
                        f"Phase '{phase.id}' model_tier='{phase.model_tier}' is above "
                        f"max_tier='{phase.max_tier}' (will be lowered to the ceiling at dispatch)"
                    )

            # ---- provider check (#969) — ERROR (not warn): unknown blocks ----
            # Deliberate F.2 inversion of model_tier's warn-pinned precedent:
            # an unknown provider lands in ``errors`` (so ``orch validate`` exits
            # non-zero) AND is rejected at build time by from_providers (INV-2).
            prov = phase.provider or ""
            if prov and prov not in self.KNOWN_PROVIDERS:
                suggestion = difflib.get_close_matches(prov, self.KNOWN_PROVIDERS, n=1, cutoff=0.4)
                hint = f"; did you mean '{suggestion[0]}'?" if suggestion else ""
                errors.append(
                    f"Phase '{phase.id}' has unknown provider='{prov}'{hint} "
                    f"(known: anthropic, openrouter — gemini/claudecode/openclaw "
                    f"are not per-phase providers in v1.1)"
                )

            # ---- model_chain check (#347) ----------------------------
            for i, chain_tier in enumerate(phase.model_chain or []):
                if chain_tier and chain_tier not in self.KNOWN_MODEL_TIERS:
                    suggestion = difflib.get_close_matches(
                        chain_tier, self.KNOWN_MODEL_TIERS, n=1, cutoff=0.4
                    )
                    hint = f"; did you mean '{suggestion[0]}'?" if suggestion else ""
                    warnings.append(
                        f"Phase '{phase.id}' has unknown model_chain[{i}]=" f"'{chain_tier}'{hint}"
                    )

            # ---- thinking_level check --------------------------------
            level = phase.thinking_level or ""
            if level and level not in self.KNOWN_THINKING_LEVELS:
                suggestion = difflib.get_close_matches(
                    level, self.KNOWN_THINKING_LEVELS, n=1, cutoff=0.4
                )
                hint = f"; did you mean '{suggestion[0]}'?" if suggestion else ""
                warnings.append(f"Phase '{phase.id}' has unknown thinking_level='{level}'{hint}")

            # ---- deprecated spec_adversary hardcoded-dispatch check (#703) ----
            # A bare ``spec_adversary`` phase (no ``adversary_config``) relied on
            # the legacy hardcoded dispatch removed in #703. Advisory only — this
            # is template-content guidance, NOT dispatch routing: it never selects
            # a parser or computes a reward; it just appends a warning string.
            if phase.id == "spec_adversary" and phase.adversary_config is None:
                warnings.append(
                    "Phase 'spec_adversary' uses deprecated hardcoded dispatch — "
                    "add adversary_config to use the generic path"
                )

        # ---- config_schema check -------------------------------------
        schema = template.config_schema
        if schema:
            if "type" not in schema:
                errors.append("config_schema is missing required 'type' field")
            elif schema["type"] == "object" and "properties" not in schema:
                warnings.append("config_schema has type='object' but is missing 'properties'")

        # ---- config_schema defaults validation (#145) -----------------
        if schema and schema.get("type") == "object":
            props = schema.get("properties", {})
            for prop_name, prop_def in props.items():
                if "default" in prop_def and "type" in prop_def:
                    default_val = prop_def["default"]
                    expected_type = prop_def["type"]
                    type_map = {
                        "string": str,
                        "integer": int,
                        "number": (int, float),
                        "boolean": bool,
                        "array": list,
                        "object": dict,
                    }
                    py_type = type_map.get(expected_type)
                    if py_type and default_val is not None and not isinstance(default_val, py_type):
                        warnings.append(
                            f"config_schema property '{prop_name}' has default "
                            f"{default_val!r} ({type(default_val).__name__}) but "
                            f"declares type '{expected_type}'"
                        )

        # ---- documentation field checks (#78) -----------------------
        # Required: description, author, version
        if not (raw_data.get("description") or "").strip():
            errors.append("Missing required documentation field: 'description'")
        if not (raw_data.get("author") or "").strip():
            errors.append("Missing required documentation field: 'author'")
        if "version" not in raw_data or not (raw_data.get("version") or "").strip():
            errors.append("Missing required documentation field: 'version'")
        elif not re.match(r"^\d+\.\d+\.\d+$", str(raw_data["version"]).strip()):
            warnings.append(
                f"Field 'version' value {raw_data['version']!r} does not match "
                "semver pattern (expected X.Y.Z, e.g. '1.0.0')"
            )

        # Recommended: use_cases, example_input
        if not raw_data.get("use_cases"):
            warnings.append("Recommended documentation field 'use_cases' is missing or empty")
        if not raw_data.get("example_input"):
            warnings.append("Recommended documentation field 'example_input' is missing or empty")

        # --- Transition graph advisory checks (Issue #232) ------------

        effective_transitions_ext = self._compute_effective_transitions(template)

        # Rule 3: Detect cycles in the transition graph (warn, not error —
        # cycles are valid when max_iterations > 0, but should be flagged as
        # advisory so authors can confirm they are intentional).
        transition_cycles = self._detect_transition_cycles(effective_transitions_ext, phase_ids)
        for cycle in transition_cycles:
            warnings.append(  # noqa: PERF401
                f"Transition cycle detected: {' → '.join(cycle)} "
                f"(valid with max_iterations, but verify this is intentional)"
            )

        # Rule 7: Warn when depends_on is non-empty on a phase that is a
        # transition target — dependency ordering and transition routing may
        # conflict, since a transition can jump back to a phase whose
        # depends_on prerequisites have already been satisfied.
        all_transition_targets_ext: Set[str] = set()
        for eff in effective_transitions_ext.values():
            all_transition_targets_ext.update(eff.values())

        for phase in template.phases:
            if phase.id in all_transition_targets_ext and phase.depends_on:
                warnings.append(  # noqa: PERF401
                    f"Phase '{phase.id}' is a transition target but also has "
                    f"depends_on={phase.depends_on} — transition routing and "
                    f"dependency ordering may conflict. Consider removing "
                    f"depends_on from transition target phases."
                )

        return errors, warnings
