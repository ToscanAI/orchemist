"""Template engine — loads YAML pipeline templates and creates execution plans."""

import difflib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .adversary_parser import AdversaryConfig
from .dialogue_phase import DialogueParticipant, DialoguePhaseConfig
from .git_integration import GitConfig
from .routing import RoutingConfig, RoutingEngine, _parse_routing_config

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
        "enabled",
        "branch_pattern",
        "auto_commit",
        "commit_phases",
        "working_dir",
        "push",
        "merge_gate",
        "create_pr",
        "base_branch",
    }
    unknown = set(raw.keys()) - known_fields
    if unknown:
        logger.warning(f"Template git config has unknown fields (ignored): {sorted(unknown)}")

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
class AutoMergeConfig:
    """Configuration for automatic PR merging after a pipeline completes scoring.

    When ``enabled`` is ``True`` and the scoring result meets or exceeds
    ``min_score``, and (optionally) the review phase returned an APPROVE
    verdict, the daemon will call ``gh pr merge`` automatically.

    This block is **disabled by default** — existing templates that do not
    declare an ``auto_merge:`` section are completely unaffected.
    """

    enabled: bool = False
    """Master switch — set to ``True`` to enable automatic merging."""

    min_score: float = 0.90
    """Minimum scoring threshold (0.0–1.0) required to trigger auto-merge."""

    require_approve: bool = True
    """When ``True``, the review phase must also return an APPROVE verdict."""

    strategy: str = "squash"
    """Merge strategy passed to ``gh pr merge``. One of: squash, merge, rebase."""

    review_phase_id: str = "review"
    """ID of the review phase whose verdict is checked when ``require_approve`` is True."""

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.min_score = float(self.min_score)
        self.require_approve = bool(self.require_approve)
        self.strategy = str(self.strategy).lower()
        if self.strategy not in ("squash", "merge", "rebase"):
            raise ValueError(
                f"AutoMergeConfig.strategy must be squash/merge/rebase, got: {self.strategy!r}"
            )
        # Clamp score to [0.0, 1.0]
        self.min_score = max(0.0, min(1.0, self.min_score))
        if self.review_phase_id is None:
            self.review_phase_id = "review"


@dataclass
class OnCompleteEntry:
    """A single chained pipeline entry within an ``on_complete:`` block.

    When a pipeline run completes, entries in the ``on_complete.success`` or
    ``on_complete.failed`` lists describe which downstream pipeline templates
    to launch and how to map the parent run's input to the child's input.

    Attributes:
        template: Template name or path to launch when the parent pipeline
                  completes with the associated outcome.
        input_map: Mapping of input key → value (or expression) used to
                   construct the child pipeline's input.  Empty dict means
                   forward the parent's input verbatim.
    """

    template: str
    input_map: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.template or not isinstance(self.template, str):
            raise ValueError("OnCompleteEntry.template must be a non-empty string")
        if self.input_map is None:
            self.input_map = {}
        if not isinstance(self.input_map, dict):
            raise TypeError(
                f"OnCompleteEntry.input_map must be a dict, got: {type(self.input_map)}"
            )


@dataclass
class OnCompleteConfig:
    """Configuration for pipeline chaining triggered on run completion.

    Declared as the ``on_complete:`` block in a pipeline template YAML.
    When a pipeline run finishes, the daemon will inspect this config and
    (in a future issue) launch the appropriate child pipelines.

    This block is **absent by default** — existing templates that do not
    declare an ``on_complete:`` section are completely unaffected.

    Attributes:
        success: List of :class:`OnCompleteEntry` to launch when the run
                 completes successfully.
        failed: List of :class:`OnCompleteEntry` to launch when the run
                fails.
        max_chain_depth: Maximum number of chained hops allowed before the
                         engine refuses to launch further children.  Prevents
                         infinite-loop chains.  Default is ``5``.
    """

    success: List[OnCompleteEntry] = field(default_factory=list)
    """Pipelines to launch on successful completion."""

    failed: List[OnCompleteEntry] = field(default_factory=list)
    """Pipelines to launch on failed completion."""

    max_chain_depth: int = 5
    """Maximum allowed chaining depth (default: 5)."""

    def __post_init__(self) -> None:
        if self.success is None:
            self.success = []
        if self.failed is None:
            self.failed = []
        if self.max_chain_depth is None:
            self.max_chain_depth = 5
        self.max_chain_depth = max(1, int(self.max_chain_depth))


def _parse_on_complete_config(raw: Any) -> Optional["OnCompleteConfig"]:
    """Parse the ``on_complete:`` section of a pipeline YAML into an :class:`OnCompleteConfig`.

    Args:
        raw: The value of ``data.get("on_complete")`` — a dict, ``None``, or
             a non-dict value (treated as absent).

    Returns:
        An :class:`OnCompleteConfig` instance if ``raw`` is a non-empty dict,
        else ``None`` (the feature is disabled when the section is absent).
    """
    if not isinstance(raw, dict):
        return None

    known_fields = {"success", "failed", "max_chain_depth"}
    unknown = set(raw.keys()) - known_fields
    if unknown:
        logger.warning(
            "Template on_complete config has unknown fields (ignored): %s",
            sorted(unknown),
        )

    def _parse_entries(raw_list: Any) -> List[OnCompleteEntry]:
        if not isinstance(raw_list, list):
            return []
        entries = []
        for item in raw_list:
            if not isinstance(item, dict):
                logger.warning("on_complete entry is not a dict (ignored): %r", item)
                continue
            if "template" not in item:
                raise ValueError("Each on_complete entry must have a 'template' key")
            entries.append(
                OnCompleteEntry(
                    template=str(item["template"]),
                    input_map=dict(item.get("input_map") or {}),
                )
            )
        return entries

    return OnCompleteConfig(
        success=_parse_entries(raw.get("success", [])),
        failed=_parse_entries(raw.get("failed", [])),
        max_chain_depth=int(raw.get("max_chain_depth", 5)),
    )


@dataclass
class BudgetConfig:
    """Optional budget enforcement for pipeline runs (Issue #5.2.2).

    When present in a template's ``budget:`` block, the daemon enforces
    cost limits per-run and per-day.  Absent = no enforcement (fully
    backward-compatible).

    Attributes:
        max_cost_per_run: Maximum USD cost per single pipeline run.  The
            daemon aborts the run with ``budget_exceeded`` status if the
            cumulative phase cost exceeds this value.  ``None`` = disabled.
        max_cost_per_day: Maximum USD cost across *all* runs started on the
            current UTC calendar day.  Preflight rejects new launches when
            this cap is reached.  ``None`` = disabled.
        warn_at_percentage: When the per-run cost reaches this percentage of
            ``max_cost_per_run``, a ``budget_warning`` notification is
            dispatched but execution continues.  Default: 80.0.

    Note:
        Per-run enforcement (abort on ``max_cost_per_run``, warn at
        ``warn_at_percentage``) is **not yet implemented** in the daemon.
        Only the daily-cap preflight check (``max_cost_per_day``) is
        active in this release.  The ``budget_exceeded`` status and
        ``budget_warning`` notification dispatch are reserved for a
        future implementation pass.
    """

    max_cost_per_run: Optional[float] = None  # USD — abort if exceeded
    max_cost_per_day: Optional[float] = None  # USD — reject new launches
    warn_at_percentage: float = 80.0  # % of per-run cap to warn at

    def __post_init__(self) -> None:
        if self.max_cost_per_run is not None:
            self.max_cost_per_run = float(self.max_cost_per_run)
            if self.max_cost_per_run < 0:
                raise ValueError(
                    f"BudgetConfig.max_cost_per_run must be >= 0, " f"got {self.max_cost_per_run}"
                )
        if self.max_cost_per_day is not None:
            self.max_cost_per_day = float(self.max_cost_per_day)
            if self.max_cost_per_day < 0:
                raise ValueError(
                    f"BudgetConfig.max_cost_per_day must be >= 0, " f"got {self.max_cost_per_day}"
                )
        self.warn_at_percentage = float(self.warn_at_percentage)
        if not (0.0 <= self.warn_at_percentage <= 100.0):
            raise ValueError(
                f"BudgetConfig.warn_at_percentage must be in [0, 100], "
                f"got {self.warn_at_percentage}"
            )


def _parse_budget_config(raw: Any) -> Optional["BudgetConfig"]:
    """Parse the ``budget:`` section of a pipeline YAML into a :class:`BudgetConfig`.

    Args:
        raw: The value of ``data.get("budget")`` — a dict, ``None``, or a
             non-dict value (treated as absent).

    Returns:
        A :class:`BudgetConfig` instance if ``raw`` is a non-empty dict,
        else ``None`` (no budget enforcement when the section is absent).
    """
    if not isinstance(raw, dict):
        return None

    known_fields = {"max_cost_per_run", "max_cost_per_day", "warn_at_percentage"}
    unknown = set(raw.keys()) - known_fields
    if unknown:
        logger.warning(
            "Template budget config has unknown fields (ignored): %s",
            sorted(unknown),
        )

    kwargs: Dict[str, Any] = {}
    for field_name in ("max_cost_per_run", "max_cost_per_day", "warn_at_percentage"):
        if field_name in raw and raw[field_name] is not None:
            try:
                kwargs[field_name] = float(raw[field_name])
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Template budget config: '{field_name}' must be a number, "
                    f"got {raw[field_name]!r}"
                ) from exc

    return BudgetConfig(**kwargs)


def _parse_auto_merge_config(raw: Any) -> Optional[AutoMergeConfig]:
    """Parse the ``auto_merge:`` section of a pipeline YAML into an :class:`AutoMergeConfig`.

    Args:
        raw: The value of ``data.get("auto_merge")`` — a dict, ``None``, or a
             non-dict value (treated as absent).

    Returns:
        An :class:`AutoMergeConfig` instance if ``raw`` is a non-empty dict,
        else ``None`` (the feature is disabled when the section is absent).
    """
    if not isinstance(raw, dict):
        return None

    known_fields = {"enabled", "min_score", "require_approve", "strategy", "review_phase_id"}
    unknown = set(raw.keys()) - known_fields
    if unknown:
        logger.warning(
            "Template auto_merge config has unknown fields (ignored): %s",
            sorted(unknown),
        )

    return AutoMergeConfig(
        enabled=bool(raw.get("enabled", False)),
        min_score=float(raw.get("min_score", 0.90)),
        require_approve=bool(raw.get("require_approve", True)),
        strategy=str(raw.get("strategy", "squash")),
        review_phase_id=str(raw.get("review_phase_id", "review")),
    )


def _parse_adversary_config(raw: Any) -> Optional["AdversaryConfig"]:
    """Parse the ``adversary_config:`` section of a phase YAML into an :class:`AdversaryConfig`.

    Args:
        raw: The value of ``phase_data.get("adversary_config")`` — a dict, ``None``,
             or a non-dict value (treated as absent).

    Returns:
        An :class:`AdversaryConfig` instance if ``raw`` is a non-empty dict,
        else ``None`` (the feature is disabled when the section is absent).

    Raises:
        ValueError: When ``valid_categories`` is empty, ``fallback_category`` is not in
                    ``valid_categories``, or ``verdict_scan`` is not ``"first"`` or ``"last"``.
    """
    if not isinstance(raw, dict):
        return None

    known_fields = {
        "valid_categories",
        "fallback_category",
        "verdict_scan",
        "reward_enabled",
        "reward_filename",
    }
    unknown = set(raw.keys()) - known_fields
    if unknown:
        logger.warning(
            "Phase adversary_config has unknown fields (ignored): %s",
            sorted(unknown),
        )

    # --- valid_categories: required, must be non-empty, deduplicate preserving order ---
    raw_cats = raw.get("valid_categories")
    if not isinstance(raw_cats, list) or len(raw_cats) == 0:
        raise ValueError("adversary_config.valid_categories must be a non-empty list")
    # Deduplicate preserving order (first occurrence wins)
    seen: set = set()
    valid_categories: list = []
    for cat in raw_cats:
        cat_s = str(cat)
        if cat_s not in seen:
            seen.add(cat_s)
            valid_categories.append(cat_s)

    # --- fallback_category: optional, but if set must be in valid_categories ---
    fallback_category: Optional[str] = raw.get("fallback_category", None)
    if fallback_category is not None:
        fallback_category = str(fallback_category)
        if fallback_category not in valid_categories:
            raise ValueError(
                f"adversary_config.fallback_category={fallback_category!r} "
                f"is not in valid_categories={valid_categories!r}"
            )

    # --- verdict_scan: optional, must be "first" or "last" ---
    verdict_scan: str = str(raw.get("verdict_scan", "last"))
    if verdict_scan not in ("first", "last"):
        raise ValueError(
            f"adversary_config.verdict_scan must be 'first' or 'last', " f"got {verdict_scan!r}"
        )

    return AdversaryConfig(
        valid_categories=valid_categories,
        fallback_category=fallback_category,
        verdict_scan=verdict_scan,
        reward_enabled=bool(raw.get("reward_enabled", False)),
        reward_filename=str(raw.get("reward_filename", "adversary_reward.json")),
    )


def _parse_dialogue_config(phase_data: Dict[str, Any]) -> Optional[DialoguePhaseConfig]:
    """Parse the dialogue-related fields of a phase YAML into a :class:`DialoguePhaseConfig`.

    The dialogue phase type (Track B / Issue #677) is signalled by
    ``type: dialogue`` on the phase YAML.  When present, this function pulls
    the ``drafter``, ``reviewer``, ``max_rounds``, and ``convergence_signal``
    fields out of *phase_data* (mutating in place by popping them) and returns
    a parsed :class:`DialoguePhaseConfig`.  Returns ``None`` when ``type`` is
    absent or set to anything other than ``"dialogue"`` (case-insensitive).

    The popped fields are removed from *phase_data* so the downstream
    ``known_fields`` filter does not emit a spurious "unknown fields" warning.

    Args:
        phase_data: Mutable dict from the YAML loader; modified in place.

    Returns:
        :class:`DialoguePhaseConfig` instance when ``type: dialogue`` is set,
        else ``None``.

    Raises:
        ValueError: When ``drafter`` or ``reviewer`` is missing/malformed.
    """
    phase_type = phase_data.get("type")
    if not isinstance(phase_type, str) or phase_type.strip().lower() != "dialogue":
        return None

    # Pop the discriminator so it doesn't trip the unknown-fields check
    phase_data.pop("type", None)

    drafter_raw = phase_data.pop("drafter", None)
    reviewer_raw = phase_data.pop("reviewer", None)
    max_rounds = phase_data.pop("max_rounds", None)
    convergence_signal = phase_data.pop("convergence_signal", None)
    drift_threshold = phase_data.pop("drift_similarity_threshold", None)

    if not isinstance(drafter_raw, dict) or not isinstance(reviewer_raw, dict):
        raise ValueError(
            f"Phase '{phase_data.get('id', '?')}': dialogue phase requires "
            "both 'drafter' and 'reviewer' dict fields"
        )

    drafter = DialogueParticipant(**drafter_raw)
    reviewer = DialogueParticipant(**reviewer_raw)

    kwargs: Dict[str, Any] = {"drafter": drafter, "reviewer": reviewer}
    if max_rounds is not None:
        kwargs["max_rounds"] = int(max_rounds)
    if convergence_signal is not None:
        kwargs["convergence_signal"] = str(convergence_signal)
    if drift_threshold is not None:
        kwargs["drift_similarity_threshold"] = float(drift_threshold)

    return DialoguePhaseConfig(**kwargs)


@dataclass
class PhaseDefinition:
    """A single phase in a pipeline template."""

    id: str
    name: str
    description: str = ""
    task_type: str = "content"  # content, research, review, code, translation
    model_tier: str = "sonnet"  # haiku, sonnet, opus
    thinking_level: str = "low"  # off, low, medium, high
    depends_on: List[str] = field(default_factory=list)
    timeout_minutes: int = 30
    human_review: bool = False
    prompt_template: str = ""  # Python str.format()-style with {input}, {previous_output}
    output_schema: Dict[str, Any] = field(default_factory=dict)
    skill_refs: List[str] = field(default_factory=list)  # paths to external skill files
    context_files: List[str] = field(default_factory=list)  # local files to inline into prompt
    retries: int = 0  # number of retry attempts after initial failure (0 = no retry)
    retry_delay_seconds: int = 30  # seconds to wait between retry attempts
    write_files: bool = False  # parse output FILE blocks and write to working_dir
    working_dir: str = "."  # directory where extracted files are written
    base_dir: str = ""  # safety root; refuse writes outside this dir (empty = working_dir)
    transitions: Dict[str, str] = field(default_factory=dict)  # outcome → phase_id
    max_iterations: int = 0  # 0 = use pipeline default
    # Command execution fields (#190)
    command: Optional[str] = None  # shell command to run (used when task_type=command)
    allowed_commands: List[str] = field(
        default_factory=list
    )  # security allowlist of command prefixes

    # Supervisor hook fields (Issue #194)
    supervisor: bool = False  # enable supervisor evaluation after this phase
    supervisor_prompt: Optional[str] = None  # custom evaluation prompt (uses default if None)
    supervisor_model: Optional[str] = None  # model tier override (defaults to opus)
    supervisor_rubric: Optional[str] = None  # quality criteria / rubric text
    supervisor_max_retries: int = 2  # max REVISE cycles before aborting

    # Model fallback chain fields (Issue #347)
    model_chain: List[str] = field(default_factory=list)
    # Ordered list of model tiers to try on retry exhaustion, e.g. ["sonnet", "opus", "haiku"].
    # Empty list means use the executor's built-in default chain (["sonnet", "opus"]).

    # Output length validation (Issue #351)
    min_output_length: int = 0
    # Minimum character count for successful phase output.
    # 0 = disabled (no validation). When > 0, the sequencer will fail the phase
    # if the output text is shorter than this threshold, catching truncated LLM
    # responses before they propagate to downstream phases.

    # Protected outputs for file-guard hash verification (Issue #531)
    protected_outputs: List[str] = field(default_factory=list)
    # List of filenames (relative to output_dir) to checksum-protect.
    # Hashes are computed after this phase completes and verified before
    # the next consuming phase's output is accepted.

    # Generic adversary parser config (Issue #701)
    adversary_config: Optional[AdversaryConfig] = None
    """Parsed ``adversary_config:`` section from the phase YAML, or ``None`` if absent."""

    # Dialogue phase config (Track B / Issue #677)
    dialogue_config: Optional["DialoguePhaseConfig"] = None
    """Parsed dialogue config when ``type: dialogue`` is set on the phase, else ``None``.

    A phase with ``dialogue_config is not None`` is dispatched by the sequencer to
    :mod:`orchestration_engine.dialogue_phase` instead of the normal task-runner
    path.  The dialogue config encapsulates the drafter / reviewer participant
    configs, ``max_rounds`` and ``convergence_signal``.
    """

    # Protected paths for directory-level hash guard (Issue #706)
    protected_paths: List[str] = field(default_factory=list)
    """List of directory paths (relative or absolute) to guard with a directory hash.
    Hashes are computed before execution and re-verified after _handle_file_write.
    Relative paths are resolved against config['repo_path'] (primary) or
    self.working_dir (fallback). output_dir is never used for resolution.
    """

    # Protect-on-approve paths for adversary phase approval locking (Issue #718)
    protect_on_approve: List[str] = field(default_factory=list)
    """List of file paths to snapshot (hash-protect) when the adversary phase
    returns an APPROVE verdict (or is exhausted — treated as implicit approval).
    Paths are resolved against output_dir (relative) or used as-is (absolute).
    Missing paths at snapshot time emit a WARNING and are skipped gracefully.
    Once snapshotted, paths are verified using the same _verify_protected_hashes
    machinery as protected_outputs. The adversary phase itself is exempt from
    its own protect_on_approve verification; only downstream non-adversary phases
    are subject to protection.
    """

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
        if self.write_files is None:
            self.write_files = False
        if self.working_dir is None:
            self.working_dir = "."
        if self.base_dir is None:
            self.base_dir = ""
        # Clamp and coerce to int to guard against negative values or YAML floats.
        # range(1, 0) is empty → last_result stays None → crash; -5 → time.sleep raises
        # ValueError; 1.5 from YAML → range(1, 2.5) raises TypeError.
        self.retries = max(0, int(self.retries))
        self.retry_delay_seconds = max(0, int(self.retry_delay_seconds))
        # Normalise transition fields
        if self.transitions is None:
            self.transitions = {}
        if self.max_iterations is None:
            self.max_iterations = 0
        self.max_iterations = max(0, int(self.max_iterations))
        # Normalise command execution fields (#190)
        if self.allowed_commands is None:
            self.allowed_commands = []
        # Normalise supervisor hook fields (#194)
        if self.supervisor is None:
            self.supervisor = False
        if self.supervisor_max_retries is None:
            self.supervisor_max_retries = 2
        self.supervisor_max_retries = max(0, int(self.supervisor_max_retries))
        # Normalise model fallback chain field (#347)
        if self.model_chain is None:
            self.model_chain = []
        # Normalise output length validation field (#351)
        if self.min_output_length is None:
            self.min_output_length = 0
        self.min_output_length = max(0, int(self.min_output_length))
        # Normalise protected outputs field (#531)
        if self.protected_outputs is None:
            self.protected_outputs = []
        # Normalise protected paths field (#706)
        if self.protected_paths is None:
            self.protected_paths = []
        # Normalise protect-on-approve field (#718)
        if self.protect_on_approve is None:
            self.protect_on_approve = []


@dataclass
class PipelineTemplate:
    """A complete pipeline template.

    Parallel-execution fields (Issue #102)
    ---------------------------------------
    parallel : bool
        When ``True`` (the default), independent phases within the same
        topological wave execute concurrently using
        :class:`~concurrent.futures.ThreadPoolExecutor`.  Set to ``False``
        for purely sequential execution (the pre-#102 behaviour).

    max_parallel : int
        Maximum number of phases that may run concurrently within a single
        wave.  ``0`` (the default) means unlimited — the executor pool size
        equals the wave size.  Positive values clamp the pool to at most
        ``max_parallel`` workers.

    fail_fast : bool
        When ``True`` (the default), the pipeline aborts as soon as any
        phase in a parallel wave fails — remaining futures are cancelled and
        the error is propagated immediately after the wave completes.  When
        ``False``, all phases in a wave run to completion regardless of
        individual failures; all errors are collected and reported together.
    """

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

    # --- Parallel execution control (Issue #102) ---
    parallel: bool = True
    """Execute independent phases within a wave concurrently (default: True)."""

    max_parallel: int = 0
    """Max concurrent phases per wave; 0 = unlimited (default: 0)."""

    fail_fast: bool = True
    """Abort remaining phases in a wave when one fails (default: True)."""

    # --- Phase transition defaults (Issue #231) ---
    default_transitions: Dict[str, str] = field(default_factory=dict)
    """Default outcome → phase_id transitions applied to all phases that don't override."""

    max_iterations: int = 10
    """Default maximum iterations for state-machine loop phases (must be > 0)."""

    # --- Post-pipeline auto-scoring (Issue #172) ---
    scenario: Optional[str] = None
    """Path to a scenario YAML file for post-pipeline auto-scoring.
    Relative paths are resolved against the template file's parent directory.
    When set, the CLI will automatically run scoring after pipeline completion
    unless --skip-scoring is passed."""

    # --- Per-repo auto-merge config (Issue #350) ---
    auto_merge: Optional[AutoMergeConfig] = None
    """Parsed ``auto_merge:`` section from the template YAML, or ``None`` if absent."""

    # --- Pipeline chaining config (Issue #330.1) ---
    on_complete: Optional[OnCompleteConfig] = None
    """Parsed ``on_complete:`` section from the template YAML, or ``None`` if absent."""

    # --- Confidence-based routing config (Issue #331.2) ---
    routing_config: Optional[RoutingConfig] = None
    """Parsed ``routing_config:`` section from the template YAML, or ``None`` if absent.
    When ``None``, callers should fall back to :data:`~routing.DEFAULT_ROUTING_CONFIG`."""

    # --- Budget enforcement config (Issue #5.2.2) ---
    budget: Optional["BudgetConfig"] = None
    """Parsed ``budget:`` section from the template YAML, or ``None`` if absent.
    When ``None``, no budget enforcement is applied (fully opt-in)."""

    # --- Template composition fields (Issue #704) ---
    extends: Optional[str] = None
    """Source ``extends:`` ID from the child template YAML (metadata only).
    Retained on the merged template for debugging / introspection. After
    :meth:`TemplateEngine.load_template` finishes, the merged result is
    self-contained — this field is informational."""

    excluded_phase_ids: List[str] = field(default_factory=list)
    """List of phase IDs that were removed via ``exclude_phases:`` at load
    time.  Used by :meth:`TemplateEngine.validate_template` to enrich
    transition-target errors when the target was excluded."""

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
        # Normalise parallel execution fields
        if self.parallel is None:
            self.parallel = True
        if self.max_parallel is None:
            self.max_parallel = 0
        if self.fail_fast is None:
            self.fail_fast = True
        self.parallel = bool(self.parallel)
        self.max_parallel = max(0, int(self.max_parallel))
        self.fail_fast = bool(self.fail_fast)
        # Normalise transition fields (Issue #231)
        if self.default_transitions is None:
            self.default_transitions = {}
        if self.max_iterations is None:
            self.max_iterations = 10
        # max_iterations on pipeline must be > 0; clamp to at least 1
        self.max_iterations = max(1, int(self.max_iterations))
        # Normalise scenario field: empty string → None
        if not self.scenario:
            self.scenario = None
        # Normalise on_complete field: non-OnCompleteConfig values → None
        if self.on_complete is not None and not isinstance(self.on_complete, OnCompleteConfig):
            self.on_complete = None
        # Normalise routing_config field: non-RoutingConfig values → None
        if self.routing_config is not None and not isinstance(self.routing_config, RoutingConfig):
            self.routing_config = None
        # Normalise budget field: non-BudgetConfig values → None
        if self.budget is not None and not isinstance(self.budget, BudgetConfig):
            self.budget = None
        # Normalise extends / excluded_phase_ids (Issue #704)
        if self.extends is not None and not isinstance(self.extends, str):
            self.extends = None
        if self.excluded_phase_ids is None:
            self.excluded_phase_ids = []


class TemplateNotFoundError(FileNotFoundError):
    """Raised when a template name cannot be resolved in any search path."""

    def __init__(self, name: str, searched: Optional[List[Path]] = None) -> None:
        self.name = name
        self.searched = searched or []
        paths_str = ", ".join(str(p) for p in self.searched)
        super().__init__(f"Template '{name}' not found. Searched: [{paths_str}]")


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
            self._project_dir = project_dir if project_dir is not None else Path.cwd() / "templates"

        self._user_dir: Path = (
            user_dir if user_dir is not None else Path.home() / ".orch" / "templates"
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
            raise ValueError(f"Template name must not contain path separators or '..': {name!r}")

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

        # --- ID-based fallback: scan YAML files and match by id field -----
        for directory, _label in self.get_search_paths():
            if not directory.exists():
                continue
            for filepath in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
                try:
                    tpl = self.load_template(filepath)
                    if tpl.id == stem:
                        logger.debug(
                            "resolve_template(%r) → %s (matched by id)",
                            name,
                            filepath,
                        )
                        return filepath.resolve()
                except Exception:
                    continue

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
        encountered (first-wins rule mirrors ``resolve_template``).  Deduplication
        is performed by **template id** (not filename stem), so two files with the
        same ``id`` field but different names are correctly treated as the same
        logical template.  Files in later directories with the same id are silently
        skipped (custom > project > user > bundled precedence order).

        Templates with a ``null`` or empty ``id`` field are skipped with a
        ``WARNING``-level log message.  Intra-directory duplicates (same ``id``
        in the same directory) are also silently skipped; alphabetical filename
        ordering determines which entry wins.
        """
        results: List[Dict[str, Any]] = []
        seen_ids: Dict[str, str] = {}  # template id → first source label

        for directory, source_label in self.get_search_paths():
            if not directory.exists():
                continue
            for filepath in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
                try:
                    template = self.load_template(filepath)
                except Exception as exc:
                    logger.warning("list_templates: skipping %s — %s", filepath, exc)
                    continue

                if not template.id:
                    logger.warning(
                        "list_templates: skipping %s — template id is null or empty",
                        filepath,
                    )
                    continue

                if template.id in seen_ids:
                    logger.debug(
                        "list_templates: skipping %s (id %r shadowed by %s)",
                        filepath,
                        template.id,
                        seen_ids[template.id],
                    )
                    continue

                seen_ids[template.id] = source_label
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
    # Template composition (Issue #704)
    # ------------------------------------------------------------------

    def _merge_extends(
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_template(self, template_path: Path) -> PipelineTemplate:
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
                "id",
                "name",
                "description",
                "task_type",
                "model_tier",
                "thinking_level",
                "depends_on",
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
            extends=source_extends,  # Issue #704: composition metadata
            excluded_phase_ids=list(removed_phase_ids),  # Issue #704: composition metadata
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
        current_wave = sorted(pid for pid, deg in in_degree.items() if deg == 0)
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

    # ------------------------------------------------------------------
    # Transition graph helpers (Issue #232)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_effective_transitions(
        template: "PipelineTemplate",
    ) -> Dict[str, Dict[str, str]]:
        """Compute effective transitions per phase using per-key merge semantics.

        Effective = {**template.default_transitions, **phase.transitions}

        Phase-level keys override pipeline-level defaults; absent keys fall
        back to the pipeline default.  This implements Rule 2 (per-key merge,
        not all-or-nothing replacement).

        Args:
            template: Loaded :class:`PipelineTemplate`.

        Returns:
            Mapping of phase_id → effective transitions dict.
        """
        result: Dict[str, Dict[str, str]] = {}
        for phase in template.phases:
            effective = {**template.default_transitions, **phase.transitions}
            result[phase.id] = effective
        return result

    @staticmethod
    def _detect_transition_cycles(
        effective_transitions: Dict[str, Dict[str, str]],
        all_phase_ids: Set[str],
    ) -> List[List[str]]:
        """Detect cycles in the transition graph using recursive DFS.

        Args:
            effective_transitions: Mapping of phase_id → effective transitions
                                   (from :meth:`_compute_effective_transitions`).
            all_phase_ids: Full set of known phase IDs.

        Returns:
            A list of cycles, each expressed as an ordered list of phase IDs
            forming the cycle (the last element loops back to the first).
            Returns an empty list when the transition graph is acyclic.
        """
        # Build adjacency: phase_id → sorted set of reachable phase_ids
        graph: Dict[str, List[str]] = {pid: [] for pid in all_phase_ids}
        for pid, eff in effective_transitions.items():
            for target in eff.values():
                if target in all_phase_ids and target not in graph[pid]:
                    graph[pid].append(target)
        for pid in graph:
            graph[pid].sort()

        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    # Found cycle — extract cycle portion
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)
            path.pop()
            rec_stack.discard(node)

        for phase_id in sorted(all_phase_ids):
            if phase_id not in visited:
                dfs(phase_id, [])

        return cycles

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
                    errors.append(f"Phase '{phase.id}' depends on unknown phase '{dep}'")

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
                    errors.append(
                        f"git.commit_phases references unknown phase '{cp}' "
                        f"(known phases: {sorted(all_ids)})"
                    )

        # Check for empty prompt_template (postmortem fix 2026-02-26)
        # Exception: command and acceptance_run phases use engine dispatch instead of a prompt
        # command (#190), acceptance_run (#532)
        _NO_PROMPT_TASK_TYPES = {"command", "acceptance_run"}
        for phase in template.phases:
            if phase.task_type in _NO_PROMPT_TASK_TYPES:
                continue  # these phases are engine-executed, not LLM-prompted
            if not phase.prompt_template or not phase.prompt_template.strip():
                errors.append(
                    f"Phase '{phase.id}' has empty prompt_template — "
                    f"every phase must define a prompt."
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
                                f"on_complete.{list_name}[{idx}]: template must be a non-empty string"
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
                                f"references this template itself (self-referential chain is an error)"
                            )

        # Issue #331.2: Validate routing_config threshold integrity
        if template.routing_config is not None:
            routing_errors = RoutingEngine(template.routing_config).validate_thresholds()
            for re_msg in routing_errors:
                errors.append(f"routing_config: {re_msg}")

        return errors

    # ------------------------------------------------------------------
    # Chain DAG validation  (Issue #330.3)
    # ------------------------------------------------------------------

    def validate_chain_dag(self, template: "PipelineTemplate") -> List[str]:
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
                        children.append(entry.template)
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
            except Exception as exc:
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
                    to_explore.append(child_id)

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
            errors.append(f"Cycle detected in chain DAG: {' → '.join(cycle)}")

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
            warnings.append(
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
                warnings.append(
                    f"Phase '{phase.id}' is a transition target but also has "
                    f"depends_on={phase.depends_on} — transition routing and "
                    f"dependency ordering may conflict. Consider removing "
                    f"depends_on from transition target phases."
                )

        return errors, warnings


def load_template(template_path: str) -> "PipelineTemplate":
    """Module-level convenience wrapper around TemplateEngine.load_template().

    Allows callers to do::

        from orchestration_engine.templates import load_template
        template = load_template("/path/to/template.yaml")

    instead of instantiating TemplateEngine explicitly.

    Args:
        template_path: Path to the YAML template file (str or Path).

    Returns:
        PipelineTemplate instance.
    """
    return TemplateEngine().load_template(Path(template_path))
