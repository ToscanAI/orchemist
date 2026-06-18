"""Template data models — ``PhaseDefinition`` and ``PipelineTemplate`` (verbatim).

Pure, ``TemplateEngine``-independent dataclasses extracted from the original
``templates.py`` module for #1035 wave 1. Bodies are unchanged; the config
dataclasses they reference are imported from :mod:`._config`. See
``templates/__init__.py`` for the facade re-exports.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..adversary_parser import AdversaryConfig
from ..dialogue_phase import DialoguePhaseConfig
from ..git_integration import GitConfig
from ..routing import RoutingConfig
from ._config import (
    AutoMergeConfig,
    BudgetConfig,
    LifecycleHooksConfig,
    OnCompleteConfig,
)


@dataclass
class PhaseDefinition:
    """A single phase in a pipeline template."""

    id: str
    name: str
    description: str = ""
    task_type: str = "content"  # content, research, review, code, translation
    model_tier: str = "sonnet"  # haiku, sonnet, opus
    min_tier: Optional[str] = None  # #987: resolution FLOOR (haiku<sonnet<opus); None = unbounded
    max_tier: Optional[str] = None  # #987: resolution CEILING; None = unbounded
    thinking_level: str = "low"  # off, low, medium, high
    depends_on: List[str] = field(default_factory=list)
    # Concurrent fan-out group (#988) — StateMachineSequencer only.
    parallel_group: List[str] = field(default_factory=list)
    """IDs of INDEPENDENT phases dispatched concurrently (via the proven #102
    _execute_wave_parallel) when the walk reaches THIS (fan-out) phase, joined
    by a barrier before its single onward transition resolves. Members must be
    NON-LOOP (max_iterations==0) and must not themselves declare parallel_group
    (no nesting — #988b deferred). Empty (default) => byte-identical serial walk."""
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

    # Opt-in CI-equivalent acceptance matrix (#985)
    acceptance_matrix: List[Dict[str, str]] = field(default_factory=list)
    # Ordered list of ``{"name": str, "command": str}`` entries for the
    # ``acceptance_run`` phase. Empty list = no matrix = legacy single-pytest
    # behaviour (byte-identical results file). Each entry is run through the
    # command_executor security model (allowlist + denylist + MAX_OUTPUT_BYTES +
    # timeout, ``shell=False``); the aggregate is GREEN iff every entry passes.

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

    # Escalation partner — the reviewed phase names its adversary phase (Issue #702)
    escalation_partner: Optional[str] = None
    """ID of the adversary phase whose verdict drives escalation when THIS phase
    exhausts its max_iterations. When set and that phase's output is present in
    ``self.phase_outputs`` with a REQUEST_CHANGES verdict, the sequencer flags
    ``escalation_required`` on the abort result. ``None`` (default) disables
    escalation detection for this phase."""

    # Per-phase provider targeting (#969)
    provider: Optional[str] = None
    """Provider that runs THIS phase: 'anthropic' | 'openrouter' (KNOWN_PROVIDERS).
    None (default) → first-can_handle selection (run-level provider). Unknown
    value → validation error + build-time rejection."""

    def __post_init__(self) -> None:  # noqa: C901
        # Normalise None values that YAML might produce for optional fields
        if self.depends_on is None:
            self.depends_on = []
        # Concurrent fan-out group (#988) — None→[] so an omitted YAML key parses
        # identically to a serial phase (byte-identical default).
        if self.parallel_group is None:
            self.parallel_group = []
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

    # --- Warm build/seed lifecycle hooks (Issue #986) ---
    lifecycle_hooks: Optional["LifecycleHooksConfig"] = None
    """Parsed ``lifecycle_hooks:`` section, or ``None`` if absent (fully opt-in).
    When ``None``, the warm-cache hook runner is a no-op (byte-identical to today)."""

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

    def __post_init__(self) -> None:  # noqa: C901
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
        # Normalise lifecycle_hooks field: non-LifecycleHooksConfig values → None (#986)
        if self.lifecycle_hooks is not None and not isinstance(
            self.lifecycle_hooks, LifecycleHooksConfig
        ):
            self.lifecycle_hooks = None
        # Normalise extends / excluded_phase_ids (Issue #704)
        if self.extends is not None and not isinstance(self.extends, str):
            self.extends = None
        if self.excluded_phase_ids is None:
            self.excluded_phase_ids = []
