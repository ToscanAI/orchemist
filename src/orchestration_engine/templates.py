"""Template engine — loads YAML pipeline templates and creates execution plans."""

import difflib
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .git_integration import GitConfig

logger = logging.getLogger(__name__)


def _is_within_dir(path: Path, directory: Path) -> bool:
    """Return True if *path* is the same as, or a descendant of, *directory*.

    Both arguments should already be resolved (absolute, symlink-free) paths.
    """
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _parse_git_config(raw: Any) -> Optional[GitConfig]:
    """Parse the ``git:`` section of a pipeline YAML into a :class:`GitConfig`.

    Args:
        raw: The value of ``data.get("git")`` — a dict, ``None``, or a
             non-dict value (treated as absent).

    Returns:
        A :class:`GitConfig` instance if ``raw`` is a non-empty dict, else
        ``None`` (preserving full backward compatibility when ``git:`` is
        absent or ``git.enabled`` is ``False``).
    """
    if not isinstance(raw, dict):
        return None

    known_fields = {
        "enabled", "branch_pattern", "auto_commit", "commit_phases",
        "working_dir", "push", "merge_gate", "create_pr", "base_branch",
    }
    unknown = set(raw.keys()) - known_fields
    if unknown:
        logger.warning(
            f"Template git config has unknown fields (ignored): {sorted(unknown)}"
        )

    return GitConfig(
        enabled=bool(raw.get("enabled", False)),
        branch_pattern=str(raw.get("branch_pattern", "feat/{pipeline_id}-{run_id}")),
        auto_commit=bool(raw.get("auto_commit", True)),
        commit_phases=list(raw.get("commit_phases") or []),
        working_dir=str(raw.get("working_dir", ".")),
        push=bool(raw.get("push", True)),
        merge_gate=bool(raw.get("merge_gate", True)),
        create_pr=bool(raw.get("create_pr", False)),
        base_branch=raw.get("base_branch") or None,
    )


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
    human_review: bool = False
    prompt_template: str = ""       # Python str.format()-style with {input}, {previous_output}
    output_schema: Dict[str, Any] = field(default_factory=dict)
    skill_refs: List[str] = field(default_factory=list)  # paths to external skill files
    context_files: List[str] = field(default_factory=list)  # local files to inline into prompt
    retries: int = 0                # number of retry attempts after initial failure (0 = no retry)
    retry_delay_seconds: int = 30   # seconds to wait between retry attempts

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
        if self.skill_refs is None:
            self.skill_refs = []
        if self.context_files is None:
            self.context_files = []
        if self.retries is None:
            self.retries = 0
        if self.retry_delay_seconds is None:
            self.retry_delay_seconds = 30
        # Clamp and coerce to int to guard against negative values or YAML floats.
        # range(1, 0) is empty → last_result stays None → crash; -5 → time.sleep raises
        # ValueError; 1.5 from YAML → range(1, 2.5) raises TypeError.
        self.retries = max(0, int(self.retries))
        self.retry_delay_seconds = max(0, int(self.retry_delay_seconds))


@dataclass
class PipelineTemplate:
    """A complete pipeline template."""

    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    use_cases: List[str] = field(default_factory=list)
    example_input: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    category: str = ""
    phases: List[PhaseDefinition] = field(default_factory=list)
    config_schema: Dict[str, Any] = field(default_factory=dict)
    fallback: Optional[Dict[str, Any]] = None
    template_path: Optional[Path] = field(default=None, repr=False)  # set by load_template
    git_config: Optional[GitConfig] = field(default=None)
    """Parsed ``git:`` section from the template YAML, or ``None`` if absent."""

    def __post_init__(self) -> None:
        if self.phases is None:
            self.phases = []
        if self.config_schema is None:
            self.config_schema = {}
        if self.description is None:
            self.description = ""
        if self.author is None:
            self.author = ""
        if self.use_cases is None:
            self.use_cases = []
        if self.example_input is None:
            self.example_input = {}
        if self.tags is None:
            self.tags = []
        if self.category is None:
            self.category = ""


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

            # Accept common field aliases (postmortem fix 2026-02-26)
            _PHASE_ALIASES: Dict[str, str] = {
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
                "id", "name", "description", "task_type", "model_tier",
                "thinking_level", "depends_on", "timeout_minutes",
                "human_review", "prompt_template", "output_schema",
                "skill_refs",
                "context_files",
                "retries",
                "retry_delay_seconds",
            }

            # Warn on unknown fields (prevents silent data loss)
            unknown = set(phase_data.keys()) - known_fields
            if unknown:
                logger.warning(
                    f"Phase '{phase_data.get('id', '?')}': "
                    f"unknown fields dropped: {unknown}"
                )

            cleaned = {k: v for k, v in phase_data.items() if k in known_fields}
            phases.append(PhaseDefinition(**cleaned))

        # Parse optional git: section
        git_config: Optional[GitConfig] = _parse_git_config(data.get("git"))

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

        # Validate git config if present
        if template.git_config is not None and template.git_config.enabled:
            gc = template.git_config
            for cp in gc.commit_phases:
                if cp not in all_ids:
                    errors.append(
                        f"git.commit_phases references unknown phase '{cp}' "
                        f"(known phases: {sorted(all_ids)})"
                    )

        # Check for empty prompt_template (postmortem fix 2026-02-26)
        for phase in template.phases:
            if not phase.prompt_template or not phase.prompt_template.strip():
                errors.append(
                    f"Phase '{phase.id}' has empty prompt_template — "
                    f"every phase must define a prompt."
                )

        # Check that all skill_ref files exist (with path traversal protection)
        template_dir = (
            template.template_path.parent
            if template.template_path is not None
            else None
        )
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

        return errors

    # ------------------------------------------------------------------
    # Extended / linting validation  (Feature #74)
    # ------------------------------------------------------------------

    #: Known valid model tier names.
    KNOWN_MODEL_TIERS: List[str] = ["haiku", "sonnet", "opus"]

    #: Known valid thinking level values.
    KNOWN_THINKING_LEVELS: List[str] = ["off", "low", "medium", "high"]

    def validate_template_extended(
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

        phase_ids = {p.id for p in template.phases}

        for phase in template.phases:
            # ---- variable reference check ----------------------------
            prompt = phase.prompt_template or ""
            # Match {some_identifier.output} or {some_identifier.something}
            # but NOT built-ins {input}, {previous_output}, {input[key]}
            for match in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\}", prompt):
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
                warnings.append(
                    f"Phase '{phase.id}' has unknown model_tier='{tier}'{hint}"
                )

            # ---- thinking_level check --------------------------------
            level = phase.thinking_level or ""
            if level and level not in self.KNOWN_THINKING_LEVELS:
                suggestion = difflib.get_close_matches(
                    level, self.KNOWN_THINKING_LEVELS, n=1, cutoff=0.4
                )
                hint = f"; did you mean '{suggestion[0]}'?" if suggestion else ""
                warnings.append(
                    f"Phase '{phase.id}' has unknown thinking_level='{level}'{hint}"
                )

        # ---- config_schema check -------------------------------------
        schema = template.config_schema
        if schema:
            if "type" not in schema:
                errors.append(
                    "config_schema is missing required 'type' field"
                )
            elif schema["type"] == "object" and "properties" not in schema:
                warnings.append(
                    "config_schema has type='object' but is missing 'properties'"
                )

        # ---- config_schema defaults validation (#145) -----------------
        if schema and schema.get("type") == "object":
            props = schema.get("properties", {})
            for prop_name, prop_def in props.items():
                if "default" in prop_def and "type" in prop_def:
                    default_val = prop_def["default"]
                    expected_type = prop_def["type"]
                    type_map = {
                        "string": str, "integer": int, "number": (int, float),
                        "boolean": bool, "array": list, "object": dict,
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
        elif not re.match(r'^\d+\.\d+\.\d+$', str(raw_data["version"]).strip()):
            warnings.append(
                f"Field 'version' value {raw_data['version']!r} does not match "
                "semver pattern (expected X.Y.Z, e.g. '1.0.0')"
            )

        # Recommended: use_cases, example_input
        if not raw_data.get("use_cases"):
            warnings.append("Recommended documentation field 'use_cases' is missing or empty")
        if not raw_data.get("example_input"):
            warnings.append("Recommended documentation field 'example_input' is missing or empty")

        return errors, warnings
