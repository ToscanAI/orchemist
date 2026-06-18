"""Template loading — YAML → :class:`PipelineTemplate` (with composition + field parsing)."""

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..adversary_parser import AdversaryConfig
from ..dialogue_phase import DialoguePhaseConfig
from ..git_integration import GitConfig
from ..routing import RoutingConfig, _parse_routing_config
from ._config import (
    AutoMergeConfig,
    BudgetConfig,
    LifecycleHooksConfig,
    OnCompleteConfig,
    _parse_adversary_config,
    _parse_auto_merge_config,
    _parse_budget_config,
    _parse_dialogue_config,
    _parse_git_config,
    _parse_lifecycle_hooks_config,
    _parse_on_complete_config,
)
from ._models import PhaseDefinition, PipelineTemplate

logger = logging.getLogger(__name__)


class _LoaderMixin:
    """YAML loading and field parsing for :class:`TemplateEngine`."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_template(self, template_path: Path) -> PipelineTemplate:  # noqa: C901
        """Load a pipeline template from a YAML file.

        Supports the new parallel-execution fields (Issue #102):

        * ``parallel``     — bool, default ``true``
        * ``max_parallel`` — int, default ``0`` (unlimited)
        * ``fail_fast``    — bool, default ``true``

        All three fields are optional and backward-compatible: existing
        templates without these fields continue to work unchanged (they
        receive the default values shown above).

        Args:
            template_path: Path to the YAML template file.

        Returns:
            PipelineTemplate instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            KeyError: If required fields (id, name) are missing.
            yaml.YAMLError: If the file is not valid YAML.
        """
        with open(template_path) as fh:
            data = yaml.safe_load(fh)

        if data is None:
            raise ValueError(f"Template file is empty: {template_path}")

        if "id" not in data:
            raise KeyError(f"Template missing required field 'id': {template_path}")
        if "name" not in data:
            raise KeyError(f"Template missing required field 'name': {template_path}")

        # --- Template composition (Issue #704) ---
        # Resolve `extends:` and `exclude_phases:` BEFORE phase parsing so the
        # rest of load_template sees the merged YAML-equivalent dict.
        extends_id_raw = data.get("extends")
        exclude_phases_raw = data.get("exclude_phases") or []
        # Track the source extends id (kept on the merged template as metadata).
        source_extends: Optional[str] = None
        removed_phase_ids: List[str] = []

        if extends_id_raw is not None:
            if not isinstance(extends_id_raw, str) or not extends_id_raw.strip():
                raise ValueError(
                    f"Template {template_path}: 'extends' must be a non-empty string "
                    f"naming a parent template, got: {extends_id_raw!r}"
                )
            if not isinstance(exclude_phases_raw, list):
                raise ValueError(
                    f"Template {template_path}: 'exclude_phases' must be a list of "
                    f"phase IDs, got: {type(exclude_phases_raw).__name__}"
                )
            source_extends = extends_id_raw.strip()
            data, removed_phase_ids = self._merge_extends(
                child_data=data,
                extends_id=source_extends,
                exclude_phases=[str(x) for x in exclude_phases_raw],
                template_path=template_path,
            )
        elif exclude_phases_raw:
            # exclude_phases without extends is meaningless — warn and ignore.
            logger.warning(
                "Template %s: 'exclude_phases' declared without 'extends' — " "ignored.",
                template_path,
            )

        raw_phases = data.get("phases") or []
        phases: List[PhaseDefinition] = []
        for phase_data in raw_phases:
            # Guard against YAML nulls for list/dict fields
            phase_data.setdefault("depends_on", [])
            phase_data.setdefault("output_schema", {})

            # Accept common field aliases (postmortem fix 2026-02-26)
            _PHASE_ALIASES: Dict[str, str] = {  # noqa: N806
                "prompt": "prompt_template",
                "model": "model_tier",
            }
            for alias, canonical in _PHASE_ALIASES.items():
                if alias in phase_data and canonical not in phase_data:
                    logger.info(
                        f"Phase '{phase_data.get('id', '?')}': "
                        f"aliased '{alias}' → '{canonical}'"
                    )
                    phase_data[canonical] = phase_data.pop(alias)
                elif alias in phase_data and canonical in phase_data:
                    # Both present — canonical wins, drop alias
                    logger.warning(
                        f"Phase '{phase_data.get('id', '?')}': "
                        f"both '{alias}' and '{canonical}' present; "
                        f"using '{canonical}', ignoring '{alias}'"
                    )
                    phase_data.pop(alias)

            # Filter to only known PhaseDefinition fields to avoid TypeError
            known_fields = {
                "id",
                "name",
                "description",
                "task_type",
                "model_tier",
                "min_tier",  # #987 per-phase resolution floor
                "max_tier",  # #987 per-phase resolution ceiling
                "thinking_level",
                "depends_on",
                "parallel_group",  # #988 concurrent fan-out group (SM only)
                "timeout_minutes",
                "human_review",
                "prompt_template",
                "output_schema",
                "skill_refs",
                "context_files",
                "retries",
                "retry_delay_seconds",
                "write_files",
                "working_dir",
                "base_dir",
                "transitions",
                "max_iterations",
                # Command execution fields (#190)
                "command",
                "allowed_commands",
                # Opt-in CI-equivalent acceptance matrix (#985)
                "acceptance_matrix",
                # Supervisor hook fields (#194)
                "supervisor",
                "supervisor_prompt",
                "supervisor_model",
                "supervisor_rubric",
                "supervisor_max_retries",
                # Model fallback chain fields (#347)
                "model_chain",
                # Output length validation (#351)
                "min_output_length",
                # Protected outputs for file-guard hash verification (#531)
                "protected_outputs",
                # Generic adversary parser config (#701)
                "adversary_config",
                # Protected paths for directory-level hash guard (#706)
                "protected_paths",
                # Protect-on-approve paths for adversary approval locking (#718)
                "protect_on_approve",
                # Escalation partner — reviewed phase names its adversary (#702)
                "escalation_partner",
                # Per-phase provider targeting (#969)
                "provider",
                # Dialogue phase config (Track B / #677)
                "dialogue_config",
            }

            # Parse dialogue_config FIRST (Track B / #677) — pops dialogue-only
            # YAML fields (``type``, ``drafter``, ``reviewer``, ``max_rounds``,
            # ``convergence_signal``, ``drift_similarity_threshold``) out of
            # phase_data so they don't trigger spurious unknown-field warnings.
            dialogue_cfg: Optional[DialoguePhaseConfig] = _parse_dialogue_config(phase_data)

            # Warn on unknown fields (prevents silent data loss)
            unknown = set(phase_data.keys()) - known_fields
            if unknown:
                logger.warning(
                    f"Phase '{phase_data.get('id', '?')}': " f"unknown fields dropped: {unknown}"
                )

            # Parse adversary_config separately (needs special handling)
            adversary_cfg: Optional[AdversaryConfig] = None
            if "adversary_config" in phase_data:
                adversary_cfg = _parse_adversary_config(phase_data.get("adversary_config"))

            cleaned = {k: v for k, v in phase_data.items() if k in known_fields}
            # Replace raw dict with parsed AdversaryConfig (or None)
            cleaned["adversary_config"] = adversary_cfg
            # Attach parsed DialoguePhaseConfig (or None)
            cleaned["dialogue_config"] = dialogue_cfg
            phases.append(PhaseDefinition(**cleaned))

        # Parse optional git: section
        git_config: Optional[GitConfig] = _parse_git_config(data.get("git"))

        # Parse optional auto_merge: section (Issue #350)
        auto_merge_config: Optional[AutoMergeConfig] = _parse_auto_merge_config(
            data.get("auto_merge")
        )

        # Parse optional on_complete: section (Issue #330.1)
        on_complete_config: Optional[OnCompleteConfig] = _parse_on_complete_config(
            data.get("on_complete")
        )

        # --- Parse parallel-execution control fields (Issue #102) ---
        # Use explicit sentinel check so that `parallel: false` (which is falsy)
        # is correctly distinguished from "field absent" (→ default True).
        raw_parallel = data.get("parallel", None)
        if raw_parallel is None:
            parallel = True
        else:
            parallel = bool(raw_parallel)

        raw_max_parallel = data.get("max_parallel", None)
        if raw_max_parallel is None:
            max_parallel = 0
        else:
            max_parallel = max(0, int(raw_max_parallel))

        raw_fail_fast = data.get("fail_fast", None)
        if raw_fail_fast is None:
            fail_fast = True
        else:
            fail_fast = bool(raw_fail_fast)

        # --- Parse phase transition fields (Issue #231) ---
        raw_default_transitions = data.get("default_transitions", None)
        default_transitions: Dict[str, str] = (
            dict(raw_default_transitions) if isinstance(raw_default_transitions, dict) else {}
        )

        raw_pipeline_max_iterations = data.get("max_iterations", None)
        if raw_pipeline_max_iterations is None:
            pipeline_max_iterations = 10
        else:
            pipeline_max_iterations = max(1, int(raw_pipeline_max_iterations))

        # Parse optional routing_config: section (Issue #331.2)
        routing_config_parsed: Optional[RoutingConfig] = _parse_routing_config(
            data.get("routing_config")
        )

        # Parse optional budget: section (Issue #5.2.2)
        budget_config_parsed: Optional[BudgetConfig] = _parse_budget_config(data.get("budget"))

        # Parse optional lifecycle_hooks: section (Issue #986 — warm build/seed cache)
        lifecycle_hooks_parsed: Optional[LifecycleHooksConfig] = _parse_lifecycle_hooks_config(
            data.get("lifecycle_hooks")
        )

        return PipelineTemplate(
            id=data["id"],
            name=data["name"],
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            use_cases=data.get("use_cases") or [],
            example_input=data.get("example_input") or {},
            tags=data.get("tags") or [],
            category=data.get("category", ""),
            phases=phases,
            config_schema=data.get("config_schema") or {},
            fallback=data.get("fallback") or None,
            template_path=Path(template_path).resolve(),
            git_config=git_config,
            parallel=parallel,
            max_parallel=max_parallel,
            fail_fast=fail_fast,
            default_transitions=default_transitions,
            max_iterations=pipeline_max_iterations,
            scenario=data.get("scenario") or None,  # Issue #172: post-pipeline auto-scoring
            auto_merge=auto_merge_config,  # Issue #350: per-repo auto-merge config
            on_complete=on_complete_config,  # Issue #330.1: pipeline chaining config
            routing_config=routing_config_parsed,  # Issue #331.2: confidence-based routing
            budget=budget_config_parsed,  # Issue #5.2.2: budget enforcement
            lifecycle_hooks=lifecycle_hooks_parsed,  # Issue #986: warm build/seed cache
            extends=source_extends,  # Issue #704: composition metadata
            excluded_phase_ids=list(removed_phase_ids),  # Issue #704: composition metadata
        )
