"""Template config dataclasses and YAML parse helpers (verbatim from #1035 wave 1).

Pure, ``TemplateEngine``-independent extractions from the original
``templates.py`` module: the ``git:`` / ``auto_merge:`` / ``on_complete:`` /
``budget:`` / ``lifecycle_hooks:`` / ``adversary_config:`` / dialogue config
dataclasses, their ``_parse_*`` helpers, and the ``_is_within_dir`` path helper.
Bodies are unchanged — see ``templates/__init__.py`` for the facade re-exports.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..adversary_parser import AdversaryConfig
from ..dialogue_phase import DialogueParticipant, DialoguePhaseConfig
from ..git_integration import GitConfig

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


@dataclass
class LifecycleHook:
    """One declared lifecycle hook (Issue #986 — warm build/seed cache).

    Attributes:
        command: Shell command run via :class:`~.command_executor.CommandExecutor`
            (``shell=False``, allowlist + dangerous-pattern denylist + timeout +
            ``MAX_OUTPUT_BYTES``). Required, non-empty.
        invalidation: List of path globs (relative to ``config['repo_path']``)
            whose CONTENT is hashed to decide HIT vs MISS. Required, non-empty.
    """

    command: str
    invalidation: List[str] = field(default_factory=list)


@dataclass
class LifecycleHooksConfig:
    """Parsed template-level ``lifecycle_hooks:`` block (Issue #986).

    Absent block ⇒ ``None`` on the template ⇒ the warm-cache runner is a no-op
    (byte-identical to today). ``hooks`` preserves declaration order (dict
    insertion order) so ``build`` runs before ``seed`` when both are declared.

    Attributes:
        hooks: Ordered mapping of hook name → :class:`LifecycleHook`.
        allowed_commands: Shared, fail-closed allowlist applied to ALL hooks.
            Empty/absent ⇒ ``[]`` ⇒ every hook command is blocked (no
            ``DEFAULT_ALLOWED_COMMANDS`` fallback).
        timeout_seconds: Per-hook command timeout. Default 120
            (= ``command_executor.DEFAULT_TIMEOUT_SECONDS``).
    """

    hooks: Dict[str, LifecycleHook] = field(default_factory=dict)
    allowed_commands: List[str] = field(default_factory=list)
    timeout_seconds: int = 120


def _parse_lifecycle_hooks_config(raw: Any) -> Optional["LifecycleHooksConfig"]:
    """Parse the ``lifecycle_hooks:`` section into a :class:`LifecycleHooksConfig`.

    Mirrors :func:`_parse_budget_config`: returns ``None`` when the section is
    absent or not a dict (fully opt-in — absence == byte-identical no-caching
    behaviour). The reserved top-level keys ``allowed_commands`` and
    ``timeout_seconds`` configure ALL hooks; every other key is a hook
    declaration that must be a mapping with a non-empty ``command`` string and a
    non-empty ``invalidation`` list of glob strings (else ``ValueError`` at load,
    consistent with ``_parse_budget_config`` raising on a bad value).

    Args:
        raw: The value of ``data.get("lifecycle_hooks")`` — a dict, ``None``, or a
             non-dict value (treated as absent).

    Returns:
        A :class:`LifecycleHooksConfig` if ``raw`` is a dict, else ``None``.
    """
    if not isinstance(raw, dict):
        return None

    reserved = {"allowed_commands", "timeout_seconds"}
    allowed_commands = list(raw.get("allowed_commands") or [])
    timeout_seconds = int(raw.get("timeout_seconds") or 120)

    hooks: Dict[str, "LifecycleHook"] = {}
    for name, spec in raw.items():
        if name in reserved:
            continue
        if not isinstance(spec, dict):
            raise ValueError(
                f"Template lifecycle_hooks: hook '{name}' must be a mapping with "
                f"'command' and 'invalidation', got {spec!r}"
            )
        known = {"command", "invalidation"}
        unknown = set(spec.keys()) - known
        if unknown:
            logger.warning(
                "Template lifecycle_hooks hook '%s' has unknown fields (ignored): %s",
                name,
                sorted(unknown),
            )
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"Template lifecycle_hooks: hook '{name}' requires a non-empty " f"'command' string"
            )
        invalidation = spec.get("invalidation")
        if (
            not isinstance(invalidation, list)
            or not invalidation
            or not all(isinstance(g, str) and g.strip() for g in invalidation)
        ):
            raise ValueError(
                f"Template lifecycle_hooks: hook '{name}' requires a non-empty "
                f"'invalidation' list of glob strings"
            )
        hooks[name] = LifecycleHook(command=command, invalidation=list(invalidation))

    return LifecycleHooksConfig(
        hooks=hooks,
        allowed_commands=allowed_commands,
        timeout_seconds=timeout_seconds,
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
