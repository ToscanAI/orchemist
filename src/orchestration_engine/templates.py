"""Template engine — loads YAML pipeline templates and creates execution plans."""

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


@dataclass
class PhaseDefinition:
    """A single phase in a pipeline template."""

    id: str
    name: str
    description: str = ""
    task_type: str = "content"      # content, research, review, code, translation
    model_tier: str = "sonnet"      # haiku, sonnet, opus
    thinking_level: str = "low"     # off, low, medium, high
    depends_on: List[str] = field(default_factory=list)
    timeout_minutes: int = 30
    prompt_template: str = ""       # Python str.format()-style with {input}, {previous_output}
    output_schema: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalise None values that YAML might produce for optional fields
        if self.depends_on is None:
            self.depends_on = []
        if self.output_schema is None:
            self.output_schema = {}
        if self.description is None:
            self.description = ""
        if self.prompt_template is None:
            self.prompt_template = ""


@dataclass
class PipelineTemplate:
    """A complete pipeline template."""

    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    phases: List[PhaseDefinition] = field(default_factory=list)
    config_schema: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phases is None:
            self.phases = []
        if self.config_schema is None:
            self.config_schema = {}
        if self.description is None:
            self.description = ""


class TemplateNotFoundError(FileNotFoundError):
    """Raised when a template name cannot be resolved in any search path."""

    def __init__(self, name: str, searched: List[Path]) -> None:
        self.name = name
        self.searched = searched
        paths_str = ", ".join(str(p) for p in searched)
        super().__init__(
            f"Template '{name}' not found. Searched: [{paths_str}]"
        )


class TemplateEngine:
    """Loads YAML templates and creates execution plans.

    Template search order (first match wins):
    1. Paths from ``ORCH_TEMPLATES_PATH`` env var (colon-separated) — prepended
    2. ``project_dir`` (default: ``./templates/``)
    3. ``user_dir``    (default: ``~/.orch/templates/``)
    4. Bundled package templates (``<package>/../../templates/``)

    Pass ``project_dir`` or ``user_dir`` to the constructor to override the
    defaults — useful in tests.
    """

    _SOURCE_CUSTOM = "custom"
    _SOURCE_PROJECT = "project"
    _SOURCE_USER = "user"
    _SOURCE_BUNDLED = "bundled"

    def __init__(
        self,
        templates_dir: Optional[Path] = None,
        project_dir: Optional[Path] = None,
        user_dir: Optional[Path] = None,
    ) -> None:
        # --- backward-compat: templates_dir sets the project dir -----------
        if templates_dir is not None:
            # Existing callers that pass templates_dir= still work.
            self._project_dir: Path = templates_dir
        else:
            self._project_dir = (
                project_dir if project_dir is not None
                else Path.cwd() / "templates"
            )

        self._user_dir: Path = (
            user_dir if user_dir is not None
            else Path.home() / ".orch" / "templates"
        )

        # Package-bundled templates live two levels up from this file:
        # src/orchestration_engine/ → src/ → repo-root/ → templates/
        self._bundled_dir: Path = Path(__file__).parent.parent.parent / "templates"

        # Keep the old attribute for code that accessed engine.templates_dir
        self.templates_dir = self._project_dir

    # ------------------------------------------------------------------
    # Search-path helpers
    # ------------------------------------------------------------------

    def get_search_paths(self) -> List[Tuple[Path, str]]:
        """Return the ordered list of ``(path, source_label)`` pairs.

        Order:
        1. Paths from ``ORCH_TEMPLATES_PATH`` (labelled "custom")
        2. Project-local   (labelled "project")
        3. User-global     (labelled "user")
        4. Bundled          (labelled "bundled")
        """
        paths: List[Tuple[Path, str]] = []

        # 1. ORCH_TEMPLATES_PATH
        env_raw = os.environ.get("ORCH_TEMPLATES_PATH", "")
        if env_raw:
            for part in env_raw.split(":"):
                part = part.strip()
                if part:
                    paths.append((Path(part), self._SOURCE_CUSTOM))

        # 2. Project-local
        paths.append((self._project_dir, self._SOURCE_PROJECT))

        # 3. User-global
        paths.append((self._user_dir, self._SOURCE_USER))

        # 4. Bundled
        paths.append((self._bundled_dir, self._SOURCE_BUNDLED))

        return paths

    # ------------------------------------------------------------------
    # Name-based resolution
    # ------------------------------------------------------------------

    def resolve_template(self, name: str) -> Path:
        """Resolve a template *name* to an absolute :class:`Path`.

        Searches ``get_search_paths()`` in order.  The *name* is matched
        against ``<stem>.yaml`` and ``<stem>.yml`` files in each directory.

        Args:
            name: Bare template name (e.g. ``"content-pipeline"``).
                  ``.yaml`` / ``.yml`` extensions are stripped before matching.

        Returns:
            Absolute :class:`Path` to the first matching file.

        Raises:
            ValueError: If *name* contains path separators or ``..`` (path
                        traversal attempt).
            TemplateNotFoundError: When no match is found in any directory.
        """
        # Security: reject path traversal attempts before touching the filesystem
        if os.sep in name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(
                f"Template name must not contain path separators or '..': {name!r}"
            )

        # Strip extension so callers can pass "foo.yaml" or just "foo"
        stem = Path(name).stem if name.endswith((".yaml", ".yml")) else name

        searched: List[Path] = []
        for directory, _label in self.get_search_paths():
            if not directory.exists():
                searched.append(directory)
                continue
            for ext in (".yaml", ".yml"):
                candidate = directory / f"{stem}{ext}"
                if candidate.exists():
                    logger.debug("resolve_template(%r) → %s", name, candidate)
                    return candidate.resolve()
            searched.append(directory)

        raise TemplateNotFoundError(name, searched)

    # ------------------------------------------------------------------
    # Template listing
    # ------------------------------------------------------------------

    def list_templates(self) -> List[Dict[str, Any]]:
        """Return all discoverable templates with metadata.

        Scans every directory in ``get_search_paths()``.  Each entry is a
        ``dict`` with the keys:

        * ``name``      — template display name
        * ``id``        — template id
        * ``version``   — template version string
        * ``phases``    — number of phases (int)
        * ``description`` — template description
        * ``source``    — source label (project / user / bundled / custom)
        * ``path``      — absolute path as ``str``

        A template file is included **only once** — the first time it is
        encountered (first-wins rule mirrors ``resolve_template``).  Files in
        later directories with the same *stem* are silently skipped.
        """
        results: List[Dict[str, Any]] = []
        seen_stems: Dict[str, str] = {}  # stem → first source

        for directory, source_label in self.get_search_paths():
            if not directory.exists():
                continue
            for filepath in sorted(directory.glob("*.yaml")) + sorted(
                directory.glob("*.yml")
            ):
                stem = filepath.stem
                if stem in seen_stems:
                    logger.debug(
                        "list_templates: skipping %s (shadowed by %s)",
                        filepath,
                        seen_stems[stem],
                    )
                    continue
                try:
                    template = self.load_template(filepath)
                except Exception as exc:
                    logger.warning("list_templates: skipping %s — %s", filepath, exc)
                    continue

                seen_stems[stem] = source_label
                results.append(
                    {
                        "name": template.name,
                        "id": template.id,
                        "version": template.version,
                        "phases": len(template.phases),
                        "description": template.description,
                        "source": source_label,
                        "path": str(filepath.resolve()),
                    }
                )

        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_template(self, template_path: Path) -> PipelineTemplate:
        """Load a pipeline template from a YAML file.

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

        raw_phases = data.get("phases") or []
        phases: List[PhaseDefinition] = []
        for phase_data in raw_phases:
            # Guard against YAML nulls for list/dict fields
            phase_data.setdefault("depends_on", [])
            phase_data.setdefault("output_schema", {})
            # Filter to only known PhaseDefinition fields to avoid TypeError
            known_fields = {
                "id", "name", "description", "task_type", "model_tier",
                "thinking_level", "depends_on", "timeout_minutes",
                "prompt_template", "output_schema",
            }
            cleaned = {k: v for k, v in phase_data.items() if k in known_fields}
            phases.append(PhaseDefinition(**cleaned))

        return PipelineTemplate(
            id=data["id"],
            name=data["name"],
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            phases=phases,
            config_schema=data.get("config_schema") or {},
        )

    def get_execution_order(self, template: PipelineTemplate) -> List[List[str]]:
        """Compute execution order respecting dependencies.

        Uses Kahn's algorithm (BFS topological sort) to group phases into
        *waves*.  All phases in the same wave are independent and could run
        in parallel; the sequencer executes them serially for MVP.

        Returns:
            List of waves, each wave being a sorted list of phase IDs.
            E.g. [["research"], ["write"], ["fact_check"], ["apply_fixes"], ["final_output"]]

        Raises:
            ValueError: If a cycle is detected (returned as empty list from this
                        method — call validate_template() to get the error message).
        """
        phase_ids = {phase.id for phase in template.phases}

        # in_degree counts unsatisfied dependencies for each phase
        in_degree: Dict[str, int] = {phase.id: 0 for phase in template.phases}
        # dependents[x] = list of phases that must wait for x to finish
        dependents: Dict[str, List[str]] = {phase.id: [] for phase in template.phases}

        for phase in template.phases:
            for dep in phase.depends_on:
                if dep in phase_ids:
                    in_degree[phase.id] += 1
                    dependents[dep].append(phase.id)
                # Unknown deps are silently ignored here; validate_template() catches them

        # Start with phases that have no unsatisfied dependencies
        current_wave = sorted(
            pid for pid, deg in in_degree.items() if deg == 0
        )
        waves: List[List[str]] = []

        while current_wave:
            waves.append(current_wave)
            next_wave: List[str] = []
            for phase_id in current_wave:
                for dep_id in dependents[phase_id]:
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        next_wave.append(dep_id)
            current_wave = sorted(next_wave)

        return waves

    def validate_template(self, template: PipelineTemplate) -> List[str]:
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
                    errors.append(
                        f"Phase '{phase.id}' depends on unknown phase '{dep}'"
                    )

        # Check for cycles only when there are no missing-dep errors
        # (missing deps can make the cycle detector give false positives)
        dep_errors = [e for e in errors if "depends on unknown" in e]
        if not dep_errors:
            ordered_ids = {
                pid
                for wave in self.get_execution_order(template)
                for pid in wave
            }
            missing_from_order = all_ids - ordered_ids
            if missing_from_order:
                errors.append(
                    f"Cycle detected involving phase(s): "
                    f"{sorted(missing_from_order)}"
                )

        return errors
