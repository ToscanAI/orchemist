"""Adaptive retry strategy engine (Issue #3.2.1).

Maps DiagnosisResult → RetryPlan using a configurable FailureClass → RetryStrategy
table.  This is pure business logic with no I/O; callers are responsible for
persisting the resulting :class:`RetryPlan` and launching retry runs.

Typical usage::

    from orchestration_engine.adaptive_retry import AdaptiveRetryEngine
    from orchestration_engine.diagnosis import DiagnosisResult

    engine = AdaptiveRetryEngine()
    plan = engine.plan(diagnosis, original_run_id="run-abc123")
    if plan is None:
        # Non-retryable failure — escalate or abort
        ...
    else:
        # Apply plan.model_override, plan.extra_context, etc. when relaunching
        db.update_pipeline_run(new_run_id, retry_of_run_id=plan.original_run_id,
                               retry_strategy=plan.strategy.value)
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional, Union

from .diagnosis import DiagnosisResult, FailureClass
from .model_registry import bare_id
from .schemas import ModelTier

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RetryStrategy enum
# ---------------------------------------------------------------------------


class RetryStrategy(str, Enum):
    """How to modify a failed pipeline run before retrying.

    Inherits from ``str`` so values can be stored/compared as plain strings,
    consistent with :class:`FailureClass` and :class:`Remediation`.
    """

    ESCALATE_MODEL = "escalate_model"
    """Use a higher model tier (e.g. Haiku → Sonnet → Opus)."""

    ADD_CONTEXT = "add_context"
    """Inject additional context into the failing phase prompt."""

    REPHRASE_PROMPT = "rephrase_prompt"
    """Rewrite the failing phase prompt to reduce ambiguity."""

    RETRY_UNCHANGED = "retry_unchanged"
    """Retry with identical configuration (suitable for transient/flaky failures)."""

    INCREASE_TIMEOUT = "increase_timeout"
    """Multiply the phase timeout budget to allow more processing time."""


# ---------------------------------------------------------------------------
# RetryPlan dataclass
# ---------------------------------------------------------------------------


@dataclass
class RetryPlan:
    """Concrete retry plan derived from a :class:`~orchestration_engine.diagnosis.DiagnosisResult`.

    Attributes:
        strategy:           The :class:`RetryStrategy` to apply on the next run.
        original_run_id:    The run ID of the failed run being retried.
        model_override:     If set, the retry run must use this model identifier
                            (e.g. ``"claude-opus-4-6"``).  ``None`` means keep
                            the original model.
        extra_context:      Additional text to inject into the phase prompt
                            before retrying.  ``None`` means no injection.
        timeout_multiplier: Factor by which to multiply the phase timeout.
                            Default ``1.0`` means no change.
    """

    strategy: RetryStrategy
    original_run_id: Optional[str] = None
    model_override: Optional[str] = None
    extra_context: Optional[str] = None
    timeout_multiplier: float = 1.0
    reason: Optional[str] = None  # Issue #615: human-readable reason for this retry plan

    def to_json(self) -> str:
        """Serialize this plan to a JSON string for DB storage.

        Returns:
            JSON string with ``strategy`` serialised as its string value.
        """
        d = asdict(self)
        d["strategy"] = self.strategy.value
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> "RetryPlan":
        """Deserialize a :class:`RetryPlan` from a JSON string.

        Args:
            raw: JSON string previously produced by :meth:`to_json`.

        Returns:
            Reconstructed :class:`RetryPlan` instance.

        Raises:
            ValueError: If *raw* is not valid JSON or missing required fields.
        """
        d = json.loads(raw)
        d["strategy"] = RetryStrategy(d["strategy"])
        # Filter to known fields for forward compatibility — unknown fields in
        # newer JSON are silently ignored rather than raising TypeError.
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Failure-class → retry-strategy mapping
# ---------------------------------------------------------------------------

#: Default mapping from :class:`FailureClass` to :class:`RetryStrategy`.
#:
#: ``None`` means the failure is **non-retryable** — the caller must escalate
#: or abort rather than launching another run.
DEFAULT_STRATEGY_MAP: Dict[FailureClass, Optional[RetryStrategy]] = {
    FailureClass.QUALITY_GAP: RetryStrategy.ESCALATE_MODEL,
    FailureClass.WRONG_MODEL: RetryStrategy.ESCALATE_MODEL,
    FailureClass.INSUFFICIENT_CONTEXT: RetryStrategy.ADD_CONTEXT,
    FailureClass.BAD_PROMPT: RetryStrategy.REPHRASE_PROMPT,
    FailureClass.FLAKY_TEST: RetryStrategy.RETRY_UNCHANGED,
    FailureClass.INFRA_ISSUE: RetryStrategy.RETRY_UNCHANGED,
    FailureClass.TIMEOUT: RetryStrategy.INCREASE_TIMEOUT,
    FailureClass.BUDGET_EXCEEDED: None,  # Non-retryable
}

#: Model escalation ladder — when strategy is ``ESCALATE_MODEL`` and no
#: explicit ``model_override`` is provided by the caller, we ascend this list
#: to pick the next tier above the current model.
#:
#: Index 0 is the lightest (cheapest) tier; higher indices are more capable.
#: Built from the canonical model_registry (#916): canonical bare ids, order
#: preserved (haiku → sonnet → opus). The top rung is now claude-opus-4-8.
MODEL_ESCALATION_LADDER: list[str] = [
    bare_id(ModelTier.HAIKU),
    bare_id(ModelTier.SONNET),
    bare_id(ModelTier.OPUS),
]

#: Default timeout multiplier applied when strategy is ``INCREASE_TIMEOUT``.
DEFAULT_TIMEOUT_MULTIPLIER: float = 2.0


# ---------------------------------------------------------------------------
# AdaptiveRetryEngine
# ---------------------------------------------------------------------------


class AdaptiveRetryEngine:
    """Translates a :class:`DiagnosisResult` into an actionable :class:`RetryPlan`.

    This class is stateless and dependency-free; all configuration is supplied
    at construction time.  It contains no I/O — callers are responsible for
    persisting the plan and relaunching runs.

    Usage::

        engine = AdaptiveRetryEngine()
        plan = engine.plan(diagnosis, original_run_id="run-abc123")
        if plan is None:
            logger.error("Non-retryable failure for run %s", run_id)
        else:
            # Relaunch with plan.model_override / plan.extra_context / etc.
            ...
    """

    def __init__(
        self,
        db: Optional[Any] = None,
        db_path: Optional[str] = None,
        strategy_map: Optional[Dict[FailureClass, Optional[RetryStrategy]]] = None,
        timeout_multiplier: float = DEFAULT_TIMEOUT_MULTIPLIER,
    ) -> None:
        """Initialise the engine.

        Args:
            db:                 :class:`~orchestration_engine.db.Database` instance.
                                Required only by :meth:`plan_and_execute`; pass
                                ``None`` when using lower-level methods (:meth:`plan`,
                                :meth:`build_retry_input`) without I/O.
            db_path:            Filesystem path to the SQLite DB file.  Passed to
                                the retry subprocess so it connects to the same
                                database.  Required only by :meth:`plan_and_execute`.
            strategy_map:       Custom :class:`FailureClass` → :class:`RetryStrategy`
                                mapping.  Defaults to :data:`DEFAULT_STRATEGY_MAP`.
            timeout_multiplier: Multiplier applied to the phase timeout when
                                strategy is ``INCREASE_TIMEOUT``.  Default ``2.0``.
        """
        self._db = db
        self._db_path = db_path
        self._strategy_map = strategy_map if strategy_map is not None else DEFAULT_STRATEGY_MAP
        self._timeout_multiplier = timeout_multiplier

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        diagnosis: DiagnosisResult,
        original_run_id: str,
        current_model: Optional[str] = None,
    ) -> Optional[RetryPlan]:
        """Derive a :class:`RetryPlan` from a failed-run diagnosis.

        Args:
            diagnosis:        The :class:`DiagnosisResult` produced by the
                              :class:`~orchestration_engine.diagnosis.DiagnosisEngine`.
            original_run_id:  Run ID of the failed run being retried.
            current_model:    Model identifier used by the original run (e.g.
                              ``"claude-haiku-4-5-20251001"``).  Required for
                              meaningful model escalation; ``None`` falls back
                              to the top of the escalation ladder.

        Returns:
            A :class:`RetryPlan` when the failure is retryable, or ``None``
            when the failure is terminal (e.g. :attr:`FailureClass.BUDGET_EXCEEDED`).
        """
        failure_class = diagnosis.failure_class
        strategy = self._strategy_map.get(failure_class)

        if strategy is None:
            _logger.info(
                "Failure class %s is non-retryable for run %s — no retry plan produced.",
                failure_class.value,
                original_run_id,
            )
            return None

        _logger.info(
            "Producing retry plan for run %s: failure_class=%s strategy=%s",
            original_run_id,
            failure_class.value,
            strategy.value,
        )

        model_override: Optional[str] = None
        extra_context: Optional[str] = None
        timeout_multiplier: float = 1.0

        if strategy == RetryStrategy.ESCALATE_MODEL:
            model_override = self._next_model(current_model)

        elif strategy == RetryStrategy.ADD_CONTEXT:
            extra_context = self._build_extra_context(diagnosis)

        elif strategy == RetryStrategy.INCREASE_TIMEOUT:
            timeout_multiplier = self._timeout_multiplier

        return RetryPlan(
            strategy=strategy,
            original_run_id=original_run_id,
            model_override=model_override,
            extra_context=extra_context,
            timeout_multiplier=timeout_multiplier,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _next_model(current_model: Optional[str]) -> str:
        """Return the next model tier above *current_model* in the escalation ladder.

        If *current_model* is not found in the ladder, or is already at the
        top, returns the top-tier model.

        Args:
            current_model: Model identifier of the original run, or ``None``.

        Returns:
            Model identifier string for the escalated tier.
        """
        ladder = MODEL_ESCALATION_LADDER
        if current_model is None or current_model not in ladder:
            return ladder[-1]
        idx = ladder.index(current_model)
        next_idx = min(idx + 1, len(ladder) - 1)
        return ladder[next_idx]

    @staticmethod
    def _build_extra_context(diagnosis: DiagnosisResult) -> str:
        """Build an extra context string to inject when strategy is ADD_CONTEXT.

        Uses the diagnosis explanation (when available) to provide the model
        with insight into why the previous run failed.

        Args:
            diagnosis: The :class:`DiagnosisResult` for the failed run.

        Returns:
            A formatted context string for prompt injection.
        """
        explanation = diagnosis.explanation or (
            "The previous run failed due to insufficient context. "
            "Please request all necessary files and information before proceeding."
        )
        return (
            f"[RETRY CONTEXT] The previous run failed with diagnosis: "
            f"{diagnosis.failure_class.value}. "
            f"Details: {explanation}"
        )

    # ------------------------------------------------------------------
    # Cost estimation helpers (Issue #396, 3.2.3)
    # ------------------------------------------------------------------

    #: Heuristic per-run cost estimates for each model tier.
    #: Used by :meth:`estimate_cost` to decide whether a retry is budget-safe.
    #: Keyed on the canonical bare model ids (#916); values are deliberately the
    #: same ordered heuristic magnitudes (haiku < sonnet < opus) so the unknown/
    #: None path in :meth:`estimate_cost` returns ``max()`` = opus (safe default).
    #: This stays an ordered heuristic dict (NOT a PricingTable delegation)
    #: because the budget-guard semantics depend on a small ordered tier set.
    MODEL_COST_HEURISTIC: Dict[str, float] = {
        bare_id(ModelTier.HAIKU): 0.05,
        bare_id(ModelTier.SONNET): 0.15,
        bare_id(ModelTier.OPUS): 0.50,
    }

    @staticmethod
    def estimate_cost(model_override: Optional[str]) -> float:
        """Return an estimated USD cost for a retry run using *model_override*.

        Uses :attr:`MODEL_COST_HEURISTIC` for known models.  Falls back to the
        most expensive tier when the model is ``None`` or unrecognised — this is
        a *safe* default: it errs on the side of caution so unknown models are
        not accidentally cleared by the budget guard.

        Heuristics:
            * ``"claude-haiku-4-5-20251001"`` → $0.05
            * ``"claude-sonnet-4-6"``         → $0.15
            * ``"claude-opus-4-8"``           → $0.50
            * ``None`` or unknown             → $0.50 (max / safe default)

        Args:
            model_override: Model identifier string, or ``None`` when no
                            escalation is specified.

        Returns:
            Estimated cost in USD as a float.
        """
        heuristics = AdaptiveRetryEngine.MODEL_COST_HEURISTIC
        if model_override is None:
            return max(heuristics.values())
        return heuristics.get(model_override, max(heuristics.values()))

    def count_existing_retries(self, original_run_id: str) -> int:
        """Return the number of retry runs already spawned for *original_run_id*.

        Delegates to :meth:`~orchestration_engine.db.Database.count_retries_for_run`.
        Requires ``self._db`` to be set (pass ``db=`` to :meth:`__init__`).

        Args:
            original_run_id: The run ID of the first-attempt run.

        Returns:
            Integer count of existing retry runs.

        Raises:
            RuntimeError: If ``self._db`` is ``None`` (engine initialised without a DB).
        """
        if self._db is None:
            raise RuntimeError(
                "count_existing_retries() requires db; pass db= to AdaptiveRetryEngine()."
            )
        return self._db.count_retries_for_run(original_run_id)

    def _resolve_root_run_id(self, run_id: str) -> str:
        """Walk the retry_of_run_id chain in the DB until the root is found.

        Iterates via ``db.get_pipeline_run()`` following ``retry_of_run_id``
        links until a row with no parent is reached. Uses a visited set to
        prevent infinite loops on corrupt/circular data.

        Args:
            run_id: Starting run ID (may be a retry or the root itself).

        Returns:
            The root run ID (the first run in the chain with no parent).
        """
        visited: set = set()
        current_id = run_id
        while current_id not in visited:
            visited.add(current_id)
            row = self._db.get_pipeline_run(current_id)
            if row is None:
                break
            parent_id = row.get("retry_of_run_id")
            if not parent_id:
                break
            current_id = parent_id
        return current_id

    def plan_and_execute(  # noqa: C901
        self,
        diagnosis: DiagnosisResult,
        run: Dict[str, Any],
        run_id: str,
        max_retries: Optional[Union[int, float, str]] = None,
    ) -> None:
        """End-to-end retry orchestration: plan → budget check → cap check → spawn.

        Steps:
        1. Resolve *original_run_id* (traces chained retries back to the root).
        2. Derive a :class:`RetryPlan` from *diagnosis* via :meth:`plan`.
        3. Check the retry cap; fail if *max_retries* is reached.
        4. Check remaining budget from ``input_json``; escalate if cost would exceed it.
        5. Build modified input via :meth:`build_retry_input`.
        6. Insert a new ``pipeline_runs`` row in the DB.
        7. Spawn a detached daemon subprocess for the new run.

        Non-retryable failures result in ``status='escalated'`` for human review.
        Budget violations result in ``status='escalated'`` for human review.
        Cap exhaustion results in ``status='failed'`` with "Max retries exceeded".

        Args:
            diagnosis:   :class:`~orchestration_engine.diagnosis.DiagnosisResult`
                         from :class:`~orchestration_engine.diagnosis.DiagnosisEngine`.
            run:         The failed ``pipeline_runs`` DB row dict.
            run_id:      The failed run's ``run_id``.
            max_retries: Hard cap on total retry attempts per original run.
                         ``None`` → safe default of 1. Negative → treated as 0.
                         Float → truncated to int. Non-numeric string → treated as 0.

        Raises:
            RuntimeError: If ``self._db`` or ``self._db_path`` are ``None``.
        """
        # Note: subprocess is imported at module-level (line 28) so it can be
        # patched via `patch("orchestration_engine.adaptive_retry.subprocess.Popen")`.
        # Do NOT re-import here; the module-level import is used.
        import sys  # noqa: PLC0415
        import uuid  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        if self._db is None or self._db_path is None:
            raise RuntimeError(
                "plan_and_execute() requires db and db_path; " "pass them to AdaptiveRetryEngine()."
            )

        # Normalize max_retries: None → 1 (safe default), negative → 0,
        # float → truncate, non-numeric string → 0.
        if max_retries is None:
            max_retries = 1
        else:
            try:
                max_retries = max(0, int(float(max_retries)))
            except (TypeError, ValueError):
                max_retries = 0

        # 1. Resolve original run ID via DB-backed chain walk (handles chained retries).
        try:
            original_run_id: str = self._resolve_root_run_id(run_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Could not resolve root run ID for %s — failing safe. Error: %s",
                run_id,
                exc,
            )
            self._db.update_pipeline_run(
                run_id,
                status="failed",
                error_message="Could not resolve retry chain — failing safe",
            )
            return

        # 2. Derive the current model from input_json for escalation decisions.
        current_model: Optional[str] = None
        try:
            input_data = json.loads(run.get("input_json") or "{}")
            current_model = input_data.get("model_override")
        except Exception:  # noqa: BLE001
            input_data = {}

        plan = self.plan(diagnosis, original_run_id=original_run_id, current_model=current_model)

        if plan is None:
            # Non-retryable failure class (e.g. BUDGET_EXCEEDED).
            _logger.warning(
                "Non-retryable failure for run %s (%s) — escalating to human.",
                run_id,
                diagnosis.failure_class.value,
            )
            self._db.update_pipeline_run(run_id, status="escalated")
            return

        # 3. Check retry cap.
        try:
            existing_count = self.count_existing_retries(original_run_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Could not determine retry count for run %s — failing safe. Error: %s",
                original_run_id,
                exc,
            )
            self._db.update_pipeline_run(
                run_id,
                status="failed",
                error_message="Retry count unavailable — failing safe",
            )
            return

        if existing_count >= max_retries:
            _logger.warning(
                "Retry cap reached for run %s (%d/%d retries) — marking failed.",
                original_run_id,
                existing_count,
                max_retries,
            )
            self._db.update_pipeline_run(
                run_id,
                status="failed",
                error_message=(
                    f"Max retries exceeded ({existing_count}/{max_retries}) "
                    f"for original run {original_run_id}"
                ),
            )
            return

        # 4. Check budget (read from input_json; 0 means no budget guard).
        try:
            budget = float(input_data.get("budget_usd") or input_data.get("cost_limit_usd") or 0)
        except Exception:  # noqa: BLE001
            budget = 0.0

        if budget > 0:
            estimated_cost = self.estimate_cost(plan.model_override or current_model)
            if estimated_cost > budget:
                _logger.warning(
                    "Retry for run %s would cost ~$%.2f but budget is $%.2f — escalating.",
                    run_id,
                    estimated_cost,
                    budget,
                )
                self._db.update_pipeline_run(run_id, status="escalated")
                return

        # 5. Build modified input JSON.
        retry_input = self.build_retry_input(plan, run, diagnosis=diagnosis)
        retry_run_id = f"retry-{uuid.uuid4().hex[:8]}-{run_id[:8]}"

        original_output_dir = run.get("output_dir", "/tmp/orch-out")
        retry_output_dir = str(_Path(original_output_dir).parent / retry_run_id)

        # 5b. (#735 RC-4 dedup, race-free) — atomic check-and-insert.
        # Use a single transaction containing both the duplicate check and the
        # INSERT so two concurrent evaluators cannot both observe "no active
        # retry" and both insert. SQLite serialises write transactions, so the
        # losing transaction will see the winner's row when it re-checks.
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM pipeline_runs "
                    "WHERE retry_of_run_id = ? AND status IN ('pending','running')",
                    (original_run_id,),
                )
                active_count = cursor.fetchone()[0]
                if active_count > 0:
                    _logger.warning(
                        "Retry already in progress for run %s — skipping duplicate "
                        "evaluate_failure (RC-4 dedup)",
                        original_run_id,
                    )
                    return  # CRITICAL: do NOT spawn daemon, do NOT git-clone.
                # 5c. (#735 RC-3) — clone original output_dir when it's a git repo.
                # Done INSIDE the transaction so a clone failure aborts the
                # insert (we never end up with a DB row pointing at a broken dir).
                # Uses os.system to avoid being intercepted by tests that patch
                # `subprocess.Popen` to mock the daemon spawn — the clone is a
                # separate concern from daemon spawning.
                orig_path = _Path(original_output_dir)
                if (orig_path / ".git").exists():
                    import os as _os  # noqa: PLC0415
                    import shlex as _shlex  # noqa: PLC0415

                    cmd = "git clone {} {}".format(
                        _shlex.quote(str(orig_path)),
                        _shlex.quote(retry_output_dir),
                    )
                    rc = _os.system(cmd + " >/dev/null 2>&1")
                    if rc != 0:
                        _logger.error(
                            "RC-3 git clone failed (aborting retry, rc=%d): %s → %s",
                            rc,
                            original_output_dir,
                            retry_output_dir,
                        )
                        raise RuntimeError(
                            f"Retry aborted: git clone of {original_output_dir} "
                            f"failed with rc={rc}"
                        )
                    _logger.info(
                        "RC-3 cloned original output_dir %s → %s (preserves "
                        "remote URL + committed history)",
                        original_output_dir,
                        retry_output_dir,
                    )

                # 6. Insert new pipeline_runs row INSIDE the dedup transaction.
                conn.execute(
                    """
                    INSERT INTO pipeline_runs (
                        run_id, template_path, template_id, input_json, mode,
                        output_dir, status, gateway_url, skip_scoring,
                        parent_run_id, chain_depth,
                        retry_of_run_id, retry_strategy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        retry_run_id,
                        run["template_path"],
                        run.get("template_id", ""),
                        json.dumps(retry_input),
                        run.get("mode", "openclaw"),
                        retry_output_dir,
                        "pending",
                        run.get("gateway_url"),
                        int(run.get("skip_scoring", 0)),
                        None,
                        0,
                        original_run_id,
                        plan.strategy.value,
                    ),
                )
        except RuntimeError:
            # Clone failure already logged; do NOT spawn daemon.
            return

        # 7. Spawn daemon subprocess (non-blocking, fully detached).
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "orchestration_engine.daemon",
                retry_run_id,
                str(self._db_path),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _logger.info(
            "Retry spawned: retry_run_id=%s original=%s strategy=%s pid=%d",
            retry_run_id,
            original_run_id,
            plan.strategy.value,
            proc.pid,
        )

    # ------------------------------------------------------------------
    # Strategy executor methods (Issue #395, 3.2.2)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_retry_unchanged(
        plan: RetryPlan,  # noqa: ARG004
        input_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return a deep copy of *input_json* with no modifications.

        Used when the failure is transient or flaky and the best action is
        to rerun with an identical configuration.

        Args:
            plan:       The :class:`RetryPlan` (unused, kept for interface consistency).
            input_json: The original pipeline input configuration dict.

        Returns:
            Deep copy of *input_json*.
        """
        return copy.deepcopy(input_json)

    @staticmethod
    def _apply_escalate_model(plan: RetryPlan, input_json: Dict[str, Any]) -> Dict[str, Any]:
        """Return *input_json* with the model escalated to ``plan.model_override``.

        Sets the ``model_override`` key so that the daemon integration (#3.2.3)
        can pass it to the pipeline runner when relaunching the retry run.

        Args:
            plan:       The :class:`RetryPlan` carrying the target model identifier
                        in :attr:`~RetryPlan.model_override`.
            input_json: The original pipeline input configuration dict.

        Returns:
            Deep copy of *input_json* with ``model_override`` set.
        """
        result = copy.deepcopy(input_json)
        result["model_override"] = plan.model_override
        return result

    @staticmethod
    def _apply_increase_timeout(plan: RetryPlan, input_json: Dict[str, Any]) -> Dict[str, Any]:
        """Return *input_json* with timeout fields scaled by ``plan.timeout_multiplier``.

        Modifies ``timeout_seconds`` (and ``timeout_override`` when present) by
        multiplying their current value by :attr:`~RetryPlan.timeout_multiplier`.
        When neither key exists the multiplier is stored under
        ``timeout_override`` so the runner can apply it on relaunch.

        Args:
            plan:       The :class:`RetryPlan` carrying the desired multiplier.
            input_json: The original pipeline input configuration dict.

        Returns:
            Deep copy of *input_json* with timeout fields scaled.
        """
        result = copy.deepcopy(input_json)
        multiplier = plan.timeout_multiplier

        if "timeout_seconds" in result and isinstance(result["timeout_seconds"], (int, float)):
            result["timeout_seconds"] = int(result["timeout_seconds"] * multiplier)
        elif "timeout_override" in result and isinstance(result["timeout_override"], (int, float)):
            result["timeout_override"] = int(result["timeout_override"] * multiplier)
        else:
            # No existing timeout key — record multiplier so downstream can apply it
            result["timeout_multiplier"] = multiplier

        return result

    @staticmethod
    def _apply_rephrase_prompt(
        plan: RetryPlan,  # noqa: ARG004
        input_json: Dict[str, Any],
        diagnosis: DiagnosisResult,
    ) -> Dict[str, Any]:
        """Return *input_json* with failure context injected into prompt fields.

        Appends a structured ``[RETRY CONTEXT]`` block to the ``extra_context``
        key (creating it when absent).  Downstream phase prompts that include
        ``{{ extra_context }}`` or ``{{ input.extra_context }}`` will
        automatically receive the retry hint.

        Args:
            plan:       The :class:`RetryPlan` (unused, kept for interface
                        consistency; callers that pre-computed context can pass
                        it via *input_json* directly).
            input_json: The original pipeline input configuration dict.
            diagnosis:  The :class:`DiagnosisResult` for the failed run, used
                        to explain *why* the prompt was problematic.

        Returns:
            Deep copy of *input_json* with ``extra_context`` set/appended.
        """
        result = copy.deepcopy(input_json)

        explanation = diagnosis.explanation or (
            "The previous run failed due to a poorly structured prompt. "
            "Please clarify ambiguous requirements and add concrete examples."
        )
        retry_block = (
            f"\n\n[RETRY CONTEXT] Previous run failed with diagnosis: "
            f"{diagnosis.failure_class.value}. "
            f"Details: {explanation}. "
            f"Please rephrase or clarify your response to address this issue."
        )

        existing = result.get("extra_context") or ""
        result["extra_context"] = (existing + retry_block).strip()
        return result

    # ------------------------------------------------------------------
    # Public dispatcher (Issue #395, 3.2.2)
    # ------------------------------------------------------------------

    def build_retry_input(  # noqa: C901
        self,
        plan: RetryPlan,
        original_run: Dict[str, Any],
        diagnosis: Optional[DiagnosisResult] = None,
    ) -> Dict[str, Any]:
        """Translate a :class:`RetryPlan` into a modified input config dict.

        This is the primary entry point for the daemon integration (#3.2.3).
        It reads the ``input_json`` field from *original_run* (a DB pipeline-run
        row), deep-copies it, applies the appropriate executor for
        ``plan.strategy``, and returns the result ready for
        :func:`~orchestration_engine.pipeline_runner.run_pipeline`.

        Args:
            plan:         The :class:`RetryPlan` produced by :meth:`plan`.
            original_run: DB row dict for the failed run.  Must contain an
                          ``input_json`` key holding either a JSON string or an
                          already-parsed dict.
            diagnosis:    The :class:`DiagnosisResult` for the failed run.
                          Required when ``plan.strategy`` is
                          :attr:`RetryStrategy.REPHRASE_PROMPT` or
                          :attr:`RetryStrategy.ADD_CONTEXT`.  ``None`` is
                          accepted for other strategies.

        Returns:
            Modified input configuration dict ready for the pipeline runner.

        Raises:
            ValueError: If ``original_run`` is missing the ``input_json`` key,
                        or if *diagnosis* is ``None`` when required by the
                        chosen strategy.
        """
        if "input_json" not in original_run:
            raise ValueError(
                "original_run must contain an 'input_json' key; "
                f"got keys: {list(original_run.keys())}"
            )

        raw = original_run["input_json"]
        if isinstance(raw, str):
            input_json: Dict[str, Any] = json.loads(raw)
        else:
            input_json = copy.deepcopy(raw)

        strategy = plan.strategy

        if strategy == RetryStrategy.RETRY_UNCHANGED:
            retry_input = self._apply_retry_unchanged(plan, input_json)

        elif strategy == RetryStrategy.ESCALATE_MODEL:
            retry_input = self._apply_escalate_model(plan, input_json)

        elif strategy == RetryStrategy.INCREASE_TIMEOUT:
            retry_input = self._apply_increase_timeout(plan, input_json)

        elif strategy in (RetryStrategy.REPHRASE_PROMPT, RetryStrategy.ADD_CONTEXT):
            if diagnosis is None:
                raise ValueError(
                    f"strategy={strategy.value!r} requires a DiagnosisResult; "
                    "pass diagnosis= to build_retry_input()"
                )
            retry_input = self._apply_rephrase_prompt(plan, input_json, diagnosis)

        else:
            # Every strategy the planner can emit (via DEFAULT_STRATEGY_MAP) has an
            # explicit branch above. Reaching here means a RetryPlan was constructed
            # with an unsupported/out-of-enum strategy: fail loudly rather than
            # silently degrading to RETRY_UNCHANGED under a different name (#932).
            raise ValueError(f"Unsupported retry strategy: {strategy.value!r}")

        # ── Issue #615: Re-fetch issue body on retry ──────────────────────────
        # If the original run had issue_number (truthy), fetch the current issue
        # body from GitHub so that postmortem enrichments are picked up by the
        # retry.  On any failure (network, auth, empty body), log a warning and
        # preserve the original input unchanged — non-fatal by design.
        issue_number = retry_input.get("issue_number")
        if issue_number:  # falsy check: handles 0, None, "", False
            try:
                repo_url = retry_input.get("repo_url", "") or ""
                cmd = ["gh", "issue", "view", str(issue_number), "--json", "body", "--jq", ".body"]
                # Only pass --repo when repo_url is a GitHub HTTPS URL
                if repo_url.startswith("https://github.com/"):
                    repo_arg = repo_url.replace("https://github.com/", "").rstrip("/")
                    if repo_arg:
                        cmd += ["--repo", repo_arg]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and result.stdout.strip():
                    retry_input["issue_body"] = result.stdout.strip()
                    _logger.info("Re-fetched issue body for #%s on retry run.", issue_number)
                else:
                    _logger.warning(
                        "Warning: could not re-fetch issue #%s — using original input.",
                        issue_number,
                    )
            except Exception:  # noqa: BLE001
                _logger.warning(
                    "Warning: could not re-fetch issue #%s — using original input.",
                    issue_number,
                )

        return retry_input
