"""Phase sequencer — executes pipeline phases in order, passing outputs forward.

Parallel execution support (Issue #102)
----------------------------------------
Independent phases within the same topological wave may now execute
concurrently via :class:`~concurrent.futures.ThreadPoolExecutor`.  Behaviour
is controlled by three fields on :class:`~.templates.PipelineTemplate`:

* ``parallel``     — enable/disable concurrent wave execution (default: ``True``)
* ``max_parallel`` — cap concurrent phases per wave (default: ``0`` = unlimited)
* ``fail_fast``    — abort remaining phases when one fails (default: ``True``)

All shared state (``phase_outputs``, progress callbacks) is protected by
reentrant locks so wave-level concurrency is safe.
"""

import logging
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .file_guard import compute_directory_hash, compute_hash
from .output_parser import extract_and_write, parse_output
from .review_parser import ReviewOutcome, parse_review_output
from .schemas import Priority, TaskError, TaskResult, TaskSpec, TaskState, TaskType
from .templates import PhaseDefinition, PipelineTemplate, TemplateEngine
from .timestamps import now_utc
from .transitions import _VERDICT_KEYWORDS, PhaseOutcome, determine_outcome, extract_verdict

logger = logging.getLogger(__name__)

# Module-level constant for output-length validation (Issue #351).
# Kept here so it is allocated once, not on every _validate_phase_output call.
_TERMINAL_PUNCTUATION: frozenset = frozenset(".!?:")

# Default supervisor prompt template (Issue #194).
# Placeholders: {rubric}, {phase_output}
_DEFAULT_SUPERVISOR_PROMPT = """\
You are a quality supervisor evaluating the output of a pipeline phase.

## RUBRIC
{rubric}

## OUTPUT
{phase_output}

## Instructions
Review the phase output against the rubric above.

Respond with exactly ONE of the following verdicts on the **first non-blank line**:

- `APPROVE: <brief reason>` — output meets all criteria, pipeline may continue
- `REVISE: <specific feedback>` — output needs improvement; describe exactly what to fix
- `ABORT: <reason>` — output is fundamentally flawed or dangerous; pipeline must stop

Your verdict line must start with APPROVE, REVISE, or ABORT.
"""


class PhaseSequencer:
    """Executes a pipeline template phase by phase.

    Supports both sequential and parallel (concurrent) execution within each
    topological wave.  Parallel mode is selected when
    ``template.parallel is True`` (the default) and a wave contains more than
    one phase.

    Thread-safety guarantees
    ------------------------
    * ``self.phase_outputs`` is protected by ``self._phase_outputs_lock``
      (a :class:`threading.RLock`).  Worker threads acquire this lock before
      writing a phase result and before reading outputs to build downstream
      prompts.
    * ``on_phase_start`` and ``on_phase_complete`` callbacks are invoked
      under ``self._callback_lock`` (a :class:`threading.RLock`) so that
      concurrent invocations from worker threads cannot interleave.
    """

    def __init__(
        self,
        template: PipelineTemplate,
        runner,
        config: dict = None,
        on_phase_complete=None,
        on_phase_start=None,
        on_pipeline_start=None,
        on_pipeline_complete=None,
        output_dir=None,
        run_id: Optional[str] = None,
        db=None,
    ) -> None:
        """Initialise the sequencer.

        Args:
            template:               The pipeline template to execute.
            runner:                 A TaskRunner instance (must have ``.queue`` and
                                    ``.executors``).
            config:                 Optional pipeline-level configuration dict (passed to
                                    templates).
            on_phase_complete:      Optional callable(phase_id: str, result: dict) → None.
                                    Called after each phase completes (success or failure).
            on_phase_start:         Optional callable(phase_id: str, phase, wave_index: int)
                                    → None.  Called just before a phase starts executing.
            on_pipeline_start:      Optional callable(pipeline_context: dict) → None.
                                    Called once before the first phase executes.  The
                                    ``pipeline_context`` dict may be mutated by the hook to
                                    inject values (e.g. ``branch_name``, ``base_branch``,
                                    ``git_diff``) that are then available to all phase
                                    prompt templates via ``{context.key}``.
            on_pipeline_complete:   Optional callable(pipeline_context: dict,
                                    result: dict | None) → None.
                                    Called after the last phase (or when the pipeline
                                    aborts).  ``result`` is ``None`` on abort/exception.
            run_id:                 Optional pipeline run identifier.  When set (together
                                    with ``db``), review phase outcomes are automatically
                                    persisted to the ``review_outcomes`` table after each
                                    review phase completes.
            db:                     Optional :class:`~.db.Database` instance.  Required for
                                    automatic review outcome recording.  Ignored when
                                    ``run_id`` is ``None``.
        """
        self.template = template
        self.runner = runner
        self.config = config or {}
        self.phase_outputs: Dict[str, Any] = {}
        self.pipeline_context: Dict[str, Any] = {}
        """Mutable context dict available to all phase prompt templates via ``{context.key}``."""
        self.on_phase_complete = on_phase_complete
        self.on_phase_start = on_phase_start
        self.on_pipeline_start = on_pipeline_start
        self.on_pipeline_complete = on_pipeline_complete
        self.output_dir = output_dir
        self.run_id: Optional[str] = run_id  # Issue #4.1.2: review outcome tracking
        self.db = db  # Issue #4.1.2: review outcome tracking

        # Fast phase lookup by ID (Issue #231) — avoids O(n) linear scan per phase
        self._phase_map: Dict[str, PhaseDefinition] = {p.id: p for p in template.phases}

        # Thread-safety locks (Issue #102)
        self._phase_outputs_lock: threading.Lock = threading.Lock()
        """Protects ``phase_outputs`` during concurrent wave execution."""
        self._callback_lock: threading.Lock = threading.Lock()
        """Serialises on_phase_start / on_phase_complete callback invocations."""

        # File-guard hash store (Issue #531)
        # Maps absolute file path → SHA256 hex digest for protected_outputs verification.
        self._protected_hashes: Dict[str, str] = {}

        # Protected-path snapshot store (Issue #706)
        # NOTE: This attribute is retained only for potential future use.
        # The snapshot dict is now passed explicitly through the call chain to
        # eliminate thread-safety races in parallel wave execution.

        # Protect-on-approve state (Issue #718)
        # Tracks the adversary phase id that owns the protect_on_approve protections.
        # The adversary phase itself is exempt from its own hash verification.
        self._protect_on_approve_source_phase: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, initial_input: dict) -> dict:  # noqa: C901
        """Execute the full pipeline.

        Phases within each topological wave are executed concurrently when
        ``template.parallel`` is ``True`` (the default) and the wave contains
        more than one phase.  A single-phase wave always executes sequentially
        regardless of the ``parallel`` flag.

        Args:
            initial_input: Pipeline input dict (e.g. article brief).

        Returns:
            Dict with keys:
            - ``phase_outputs``: mapping of phase_id → result dict
            - ``final_output``:  result dict of the last phase
        """
        engine = TemplateEngine()
        execution_order = engine.get_execution_order(self.template)

        if not execution_order:
            logger.warning("Template has no executable phases (empty or fully cyclic)")
            return {"phase_outputs": {}, "final_output": {}}

        # Call pipeline-start hook (e.g. git branch creation)
        if self.on_pipeline_start is not None:
            try:
                self.on_pipeline_start(self.pipeline_context)
            except Exception as exc:
                logger.error(f"Pipeline {self.template.id}: on_pipeline_start hook failed: {exc}")
                raise

        final_result: dict = {}
        try:
            for wave_index, wave in enumerate(execution_order):
                # Decide whether to run this wave in parallel.
                # A wave of size 1 is always sequential (no overhead, no ambiguity).
                use_parallel = self.template.parallel and len(wave) > 1

                if use_parallel:
                    abort_result = self._execute_wave_parallel(wave, wave_index, initial_input)
                else:
                    abort_result = self._execute_wave_sequential(wave, wave_index, initial_input)

                # Either method returns None on success or a final_result dict on abort
                if abort_result is not None:
                    # Call pipeline-complete hook signalling failure
                    if self.on_pipeline_complete is not None:
                        try:
                            self.on_pipeline_complete(self.pipeline_context, None)
                        except Exception as hook_exc:  # noqa: BLE001
                            logger.warning(
                                f"Pipeline {self.template.id}: "
                                f"on_pipeline_complete hook failed: {hook_exc}"
                            )
                    return abort_result

            # Determine the final output (last phase of the last wave)
            last_phase_id = execution_order[-1][-1]
            final_output = self.phase_outputs.get(last_phase_id, {})
            final_result = {
                "phase_outputs": self.phase_outputs,
                "final_output": final_output,
            }

        except Exception:
            # Call pipeline-complete hook on unexpected exception
            if self.on_pipeline_complete is not None:
                try:
                    self.on_pipeline_complete(self.pipeline_context, None)
                except Exception as hook_exc:  # noqa: BLE001
                    logger.warning(
                        f"Pipeline {self.template.id}: "
                        f"on_pipeline_complete hook (exception path) failed: {hook_exc}"
                    )
            raise

        # Call pipeline-complete hook on success
        if self.on_pipeline_complete is not None:
            try:
                self.on_pipeline_complete(self.pipeline_context, final_result)
            except Exception as hook_exc:  # noqa: BLE001
                logger.warning(
                    f"Pipeline {self.template.id}: on_pipeline_complete hook failed: {hook_exc}"
                )

        return final_result

    # ------------------------------------------------------------------
    # Wave execution strategies
    # ------------------------------------------------------------------

    def _execute_wave_sequential(  # noqa: C901
        self,
        wave: List[str],
        wave_index: int,
        initial_input: dict,
    ) -> Optional[dict]:
        """Execute all phases in *wave* one at a time (original behaviour).

        Args:
            wave:          Ordered list of phase IDs in this wave.
            wave_index:    Zero-based index of this wave in the execution plan.
            initial_input: Pipeline input dict.

        Returns:
            ``None`` on success.  A pipeline-abort result dict when a phase
            fails (mirrors the original abort logic).
        """
        for phase_id in wave:
            phase = self._phase_map.get(phase_id) or self._get_phase(phase_id)

            # Notify caller that phase is about to start
            self._invoke_on_phase_start(phase_id, phase, wave_index)

            # ── Dialogue phase dispatch (Track B / Issue #677) ───────────
            # Phases with ``type: dialogue`` in their YAML carry a parsed
            # DialoguePhaseConfig.  These bypass the normal task-runner path
            # entirely and run a drafter ↔ reviewer loop via ``dialogue_phase``.
            # Gated by the ``dialogue_phase`` feature flag (#840) — when
            # False (default), the phase is SKIPPED with a synthetic result
            # that downstream handlers treat as a clean exit, and the linear
            # wave proceeds to the next phase.
            if getattr(phase, "dialogue_config", None) is not None:
                from . import feature_flags as _ff  # noqa: PLC0415

                if not _ff.is_enabled("dialogue_phase"):
                    # INFO not WARNING: the default-disabled state IS a clean
                    # exit (per CHANGELOG framing). Recurring WARNINGs on a
                    # default-state run train operators to ignore the level.
                    logger.info(
                        "Pipeline %s: dialogue phase '%s' SKIPPED — admin "
                        "feature_flags.dialogue_phase is False. Enable it in "
                        "the admin console (or write `feature_flags.dialogue_phase`"
                        " = true to ~/.orchestration-engine/admin.json) to run "
                        "this phase.",
                        self.template.id,
                        phase_id,
                    )
                    result = {
                        "state": "skipped_by_feature_flag",
                        "result": "",
                        "skipped_reason": "feature_flags.dialogue_phase is False",
                        "cost_usd": 0.0,
                        "tokens_consumed": 0,
                        "execution_time_seconds": 0.0,
                    }
                    with self._phase_outputs_lock:
                        self.phase_outputs[phase_id] = result
                    self._invoke_on_phase_complete(phase_id, result)
                    continue
                phase_input = self._build_phase_input(phase, initial_input)
                result = self._execute_dialogue_phase(phase, phase_input)
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                phase_state = result.get("state", "unknown")
                logger.info(
                    f"Pipeline {self.template.id}: dialogue phase '{phase_id}' "
                    f"completed (state={phase_state})"
                )
                if phase_state != "success":
                    return {
                        "phase_outputs": self.phase_outputs,
                        "final_output": result,
                        "failed_phase": phase_id,
                        "aborted": True,
                    }
                continue

            # Build the prompt for this phase. The shared missing_sink collects
            # any <MISSING:> markers emitted by config/input/previous_output
            # substitution in BOTH the prompt and the command/working_dir.
            _missing_sink: set = set()
            phase_input = self._build_phase_input(phase, initial_input, missing_sink=_missing_sink)
            command_extras = self._build_command_extras(
                phase, initial_input, missing_sink=_missing_sink
            )

            # Reject the phase before dispatch if a genuine config/input/
            # previous_output reference rendered <MISSING:> (#535) — a broken
            # config/template must abort the pipeline rather than send garbage
            # to the executor. The guard precedes submit_task so a placeholder
            # failure never enters the retry loop (a config error won't
            # self-heal).
            placeholder_failure = self._check_for_unresolved_placeholders(phase, _missing_sink)
            if placeholder_failure is not None:
                result = placeholder_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return {
                    "phase_outputs": self.phase_outputs,
                    "final_output": result,
                    "failed_phase": phase_id,
                    "aborted": True,
                }

            # Resolve model tier to a ModelTier enum value (if possible)
            preferred_model = self._resolve_model_tier(phase.model_tier)

            # Create and queue the TaskSpec
            task = TaskSpec(
                type=self._resolve_task_type(phase.task_type),
                payload={
                    "prompt": phase_input,
                    "phase_id": phase.id,
                    "pipeline_id": self.template.id,
                    "model_chain": phase.model_chain or [],  # #347: propagate fallback chain
                    "sandbox_roots": self._sandbox_roots(),  # #794: tool-call sandbox
                    **command_extras,
                },
                priority=Priority.HIGH,
                preferred_model=preferred_model,
                timeout_seconds=phase.timeout_minutes * 60,
            )

            # Snapshot protected_paths before execution (#706)
            # Local dict — no shared instance state — safe for both sequential and parallel paths.
            _path_snapshots = self._snapshot_protected_paths(phase)

            task_id = self.runner.queue.submit_task(task)
            logger.info(
                f"Pipeline {self.template.id}: submitted phase '{phase_id}' " f"(task_id={task_id})"
            )

            # Execute synchronously and store output
            result = self._execute_and_wait(task_id, phase, initial_input=initial_input)

            # Write FILE blocks to disk if phase requests it (#189)
            if phase.write_files:
                self._handle_file_write(phase, result)

            # Folder-guard verification — check if protected_paths were modified (#706)
            path_guard_failure = self._verify_protected_paths(phase, _path_snapshots)
            if path_guard_failure is not None:
                result = path_guard_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return {
                    "phase_outputs": self.phase_outputs,
                    "final_output": result,
                    "failed_phase": phase_id,
                    "aborted": True,
                }

            # Hash verification — check if THIS phase tampered with protected files (#531)
            guard_failure = self._verify_protected_hashes(phase)
            if guard_failure is not None:
                result = guard_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return {
                    "phase_outputs": self.phase_outputs,
                    "final_output": result,
                    "failed_phase": phase_id,
                    "aborted": True,
                }

            # ── Result enrichment from disk (Issue #681) ─────────────────
            self._enrich_result_from_disk(phase_id, result)

            # ── Git handoff: commit phase output (Issue #681 / #674) ─────────
            _git_handoff = getattr(self, "_git_handoff", None)
            _loop_groups = getattr(self, "_loop_groups", None)
            if (
                _git_handoff is not None
                and _git_handoff.is_active()
                and _loop_groups is not None
                and phase_id in _loop_groups
            ):
                phase_text = _extract_phase_text(result)
                if phase_text is not None:
                    _git_handoff.commit_phase_output(phase_id, 0, phase_text)

            with self._phase_outputs_lock:
                self.phase_outputs[phase_id] = result

            # Hash capture — record protected_outputs from THIS phase for future verification (#531)
            if result.get("state") == "success" and getattr(phase, "protected_outputs", []):
                self._store_protected_hashes(phase)

            # Output length validation (#351) — fail phase if output is too short
            validation_failure = self._validate_phase_output(phase, result)
            if validation_failure is not None:
                result = validation_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result

            # Supervisor hook (#194) — sequential path
            if getattr(phase, "supervisor", False) and result.get("state") == "success":
                result, abort_info = self._run_supervisor_for_phase(phase, result, initial_input)
                if abort_info:
                    logger.error(
                        f"Pipeline {self.template.id}: pipeline aborted by supervisor "
                        f"on phase '{phase.id}'"
                    )
                    return abort_info
                # Update phase_outputs with potentially revised result
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result

            # Record review outcome durably (Issue #4.1.2) — sequential path
            self._record_review_outcome(phase, result)

            # Record adversary reward (Issue #546 / #702) — sequential path
            self._record_adversary_outcome(phase, result)

            # Notify caller (e.g. CLI progress display)
            self._invoke_on_phase_complete(phase_id, result)

            phase_state = result.get("state", "unknown")
            logger.info(
                f"Pipeline {self.template.id}: phase '{phase_id}' completed "
                f"(state={phase_state})"
            )

            # Stop pipeline on phase failure — don't feed errors downstream
            if phase_state in ("failed", "permanently_failed"):
                logger.error(
                    f"Pipeline {self.template.id}: phase '{phase_id}' failed, "
                    f"aborting pipeline."
                )
                return {
                    "phase_outputs": self.phase_outputs,
                    "final_output": result,
                    "failed_phase": phase_id,
                    "aborted": True,
                }

        return None  # success

    def _execute_wave_parallel(  # noqa: C901
        self,
        wave: List[str],
        wave_index: int,
        initial_input: dict,
    ) -> Optional[dict]:
        """Execute all phases in *wave* concurrently using a thread pool.

        Respects ``template.max_parallel`` (pool size cap) and
        ``template.fail_fast`` (abort siblings on first failure).

        Thread-safety notes
        -------------------
        * Each worker thread acquires ``_phase_outputs_lock`` before writing
          its result to ``self.phase_outputs`` and before calling
          ``_build_phase_input`` (which reads ``phase_outputs``).
        * Callback invocations (``on_phase_start``, ``on_phase_complete``) are
          serialised via ``_callback_lock`` so listeners that mutate shared
          state (e.g. a progress bar) remain consistent.
        * ``future.cancel()`` is a best-effort hint; Python's GIL ensures that
          already-running futures finish their current bytecode instruction but
          the cancelled flag prevents *new* executions from starting in the
          pool queue.  Completed futures are never cancelled.

        Args:
            wave:          List of phase IDs ready to execute in parallel.
            wave_index:    Zero-based wave index.
            initial_input: Pipeline input dict.

        Returns:
            ``None`` when the wave completes without failures.
            A pipeline-abort dict when fail_fast aborts due to a failed phase,
            or when fail_fast=False and at least one phase failed (all errors
            collected, first failure reported).
        """
        # Determine pool size
        max_workers: int = len(wave)
        if self.template.max_parallel > 0:
            max_workers = min(max_workers, self.template.max_parallel)

        fail_fast = self.template.fail_fast

        # Maps future → phase_id so we can identify which phase finished
        future_to_phase: Dict[Future, str] = {}

        # Shared abort flag: set to True when fail_fast triggers
        abort_event = threading.Event()

        def _run_phase(phase_id: str) -> dict:  # noqa: C901
            """Worker function executed in the thread pool for one phase."""
            if abort_event.is_set():
                # Another phase failed with fail_fast=True — skip execution
                logger.info(
                    f"Pipeline {self.template.id}: phase '{phase_id}' skipped "
                    f"(abort_event set by sibling failure)"
                )
                # Return a synthetic skipped result so the future resolves cleanly
                return {
                    "state": "skipped",
                    "result": {"text": ""},
                    "phase_id": phase_id,
                    "skipped": True,
                }

            phase = self._phase_map.get(phase_id) or self._get_phase(phase_id)

            # Notify caller that phase is about to start
            self._invoke_on_phase_start(phase_id, phase, wave_index)

            # ── Dialogue phase dispatch + gate (Track B + #840) ──────────
            # Mirrors the serial path (sequencer.py:_execute_wave_sequential).
            # A type:dialogue phase in a parallel wave bypasses the task
            # queue entirely — and bypasses the gate if not checked here.
            if getattr(phase, "dialogue_config", None) is not None:
                from . import feature_flags as _ff  # noqa: PLC0415

                if not _ff.is_enabled("dialogue_phase"):
                    logger.info(
                        "Pipeline %s: dialogue phase '%s' SKIPPED (parallel "
                        "wave) — admin feature_flags.dialogue_phase is False.",
                        self.template.id,
                        phase_id,
                    )
                    result = {
                        "state": "skipped_by_feature_flag",
                        "result": "",
                        "skipped_reason": "feature_flags.dialogue_phase is False",
                        "cost_usd": 0.0,
                        "tokens_consumed": 0,
                        "execution_time_seconds": 0.0,
                    }
                    with self._phase_outputs_lock:
                        self.phase_outputs[phase_id] = result
                    self._invoke_on_phase_complete(phase_id, result)
                    return result
                # Dispatch via the canonical dialogue runner (gated above).
                with self._phase_outputs_lock:
                    phase_input = self._build_phase_input(phase, initial_input)
                result = self._execute_dialogue_phase(phase, phase_input)
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return result

            # Build prompt — read phase_outputs under lock to avoid races. The
            # missing_sink (a fresh per-worker local set) collects <MISSING:>
            # markers from config/input/previous_output substitution.
            _missing_sink: set = set()
            with self._phase_outputs_lock:
                phase_input = self._build_phase_input(
                    phase, initial_input, missing_sink=_missing_sink
                )

            # command_extras reads only phase/config (immutable here) — no lock.
            command_extras = self._build_command_extras(
                phase, initial_input, missing_sink=_missing_sink
            )

            # Reject the phase before dispatch if a genuine config/input/
            # previous_output reference rendered <MISSING:> (#535). The marker
            # set and command_extras are local values, so the guard runs OUTSIDE
            # the phase_outputs lock safely. The parallel worker returns the
            # bare phase-result; the pool aggregator keys on permanently_failed
            # and wraps it into the pipeline abort.
            placeholder_failure = self._check_for_unresolved_placeholders(phase, _missing_sink)
            if placeholder_failure is not None:
                result = placeholder_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return result

            preferred_model = self._resolve_model_tier(phase.model_tier)

            task = TaskSpec(
                type=self._resolve_task_type(phase.task_type),
                payload={
                    "prompt": phase_input,
                    "phase_id": phase.id,
                    "pipeline_id": self.template.id,
                    "model_chain": phase.model_chain or [],  # #347: propagate fallback chain
                    "sandbox_roots": self._sandbox_roots(),  # #794: tool-call sandbox
                    **command_extras,
                },
                priority=Priority.HIGH,
                preferred_model=preferred_model,
                timeout_seconds=phase.timeout_minutes * 60,
            )

            # Snapshot protected_paths before execution (#706)
            # Local dict scoped to this worker thread — no shared instance state,
            # so concurrent phases in the same wave cannot clobber each other's snapshots.
            _path_snapshots = self._snapshot_protected_paths(phase)

            task_id = self.runner.queue.submit_task(task)
            logger.info(
                f"Pipeline {self.template.id}: submitted phase '{phase_id}' "
                f"(task_id={task_id}, parallel=True)"
            )

            result = self._execute_and_wait(task_id, phase, initial_input=initial_input)

            # Write FILE blocks to disk if phase requests it (#189)
            if phase.write_files:
                self._handle_file_write(phase, result)

            # Folder-guard verification — check if protected_paths were modified (#706)
            path_guard_failure = self._verify_protected_paths(phase, _path_snapshots)
            if path_guard_failure is not None:
                result = path_guard_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return result

            # Hash verification — check if THIS phase tampered with protected files (#531)
            guard_failure = self._verify_protected_hashes(phase)
            if guard_failure is not None:
                result = guard_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result
                self._invoke_on_phase_complete(phase_id, result)
                return result

            # ── Result enrichment from disk (Issue #681) ─────────────────
            self._enrich_result_from_disk(phase_id, result)

            # Write result under lock — prevents lost updates on shared dict
            with self._phase_outputs_lock:
                self.phase_outputs[phase_id] = result

            # Hash capture — record protected_outputs from THIS phase for future verification (#531)
            if result.get("state") == "success" and getattr(phase, "protected_outputs", []):
                self._store_protected_hashes(phase)

            # Output length validation (#351) — fail phase if output is too short
            validation_failure = self._validate_phase_output(phase, result)
            if validation_failure is not None:
                result = validation_failure
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result

            # Supervisor hook (#194) — parallel path
            if getattr(phase, "supervisor", False) and result.get("state") == "success":
                result, abort_info = self._run_supervisor_for_phase(phase, result, initial_input)
                if abort_info:
                    logger.error(
                        f"Pipeline {self.template.id}: pipeline aborted by supervisor "
                        f"on phase '{phase.id}' (parallel)"
                    )
                    # Return a failed-state result so outer loop triggers pipeline abort
                    failed_result = {
                        "state": TaskState.FAILED.value,
                        "result": {"text": ""},
                        "supervisor_abort": True,
                        "supervisor_reason": abort_info.get("supervisor_reason", ""),
                        "metadata": {"attempt_number": 1, "total_attempts": 1},
                        "confidence": 0.0,
                    }
                    with self._phase_outputs_lock:
                        self.phase_outputs[phase_id] = failed_result
                    return failed_result
                # Update phase_outputs with potentially revised result
                with self._phase_outputs_lock:
                    self.phase_outputs[phase_id] = result

            # Record review outcome durably (Issue #4.1.2) — parallel path
            self._record_review_outcome(phase, result)

            # Record adversary reward (Issue #546 / #702) — parallel path
            self._record_adversary_outcome(phase, result)

            # Notify caller
            self._invoke_on_phase_complete(phase_id, result)

            phase_state = result.get("state", "unknown")
            logger.info(
                f"Pipeline {self.template.id}: phase '{phase_id}' completed "
                f"(state={phase_state}, parallel=True)"
            )

            return result

        # Submit all phases to the pool
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for phase_id in wave:
                fut = executor.submit(_run_phase, phase_id)
                future_to_phase[fut] = phase_id

            # Collect results as they complete
            failed_phases: List[Tuple[str, dict]] = []

            for fut in as_completed(future_to_phase):
                phase_id = future_to_phase[fut]
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    # The worker raised an unhandled exception — treat as failure
                    logger.error(
                        f"Pipeline {self.template.id}: phase '{phase_id}' worker "
                        f"raised unhandled exception: {exc}"
                    )
                    synthetic_result = {
                        "state": TaskState.FAILED.value,
                        "result": {"text": ""},
                        "errors": [{"code": "WORKER_EXCEPTION", "message": str(exc)}],
                        "metadata": {"attempt_number": 1, "total_attempts": 1},
                        "confidence": 0.0,
                    }
                    with self._phase_outputs_lock:
                        self.phase_outputs[phase_id] = synthetic_result
                    self._invoke_on_phase_complete(phase_id, synthetic_result)
                    result = synthetic_result

                # Skip accounting for phases that were cancelled by abort_event
                if result.get("skipped"):
                    continue

                phase_state = result.get("state", "unknown")
                if phase_state in ("failed", "permanently_failed"):
                    failed_phases.append((phase_id, result))
                    if fail_fast:
                        # Signal remaining queued (not yet running) workers to skip.
                        # NOTE: fail_fast is best-effort — phases already executing
                        # (e.g. mid-LLM-call) will run to completion because Python
                        # threads cannot be forcibly interrupted.  Only queued (not
                        # yet started) futures are prevented from running.
                        abort_event.set()
                        # Cancel futures that haven't started yet (best-effort)
                        for other_fut in future_to_phase.keys():
                            if other_fut is not fut and not other_fut.done():
                                other_fut.cancel()
                        logger.warning(
                            f"Pipeline {self.template.id}: phase '{phase_id}' failed "
                            f"(fail_fast=True) — cancelling remaining wave phases."
                        )
                        break  # Stop waiting; pool __exit__ will drain workers

        # After the pool is done, assess outcomes
        if failed_phases:
            first_failed_id, first_result = failed_phases[0]
            all_failed = ", ".join(pid for pid, _ in failed_phases)
            logger.error(
                f"Pipeline {self.template.id}: wave {wave_index} had "
                f"{len(failed_phases)} failed phase(s): [{all_failed}]. "
                f"Aborting pipeline."
            )
            return {
                "phase_outputs": self.phase_outputs,
                "final_output": first_result,
                "failed_phase": first_failed_id,
                "failed_phases": [pid for pid, _ in failed_phases],
                "aborted": True,
            }

        return None  # wave succeeded

    # ------------------------------------------------------------------
    # Thread-safe callback helpers
    # ------------------------------------------------------------------

    def _invoke_on_phase_start(
        self,
        phase_id: str,
        phase: PhaseDefinition,
        wave_index: int,
    ) -> None:
        """Invoke ``on_phase_start`` callback under the callback lock.

        Swallows all exceptions so a misbehaving callback never crashes
        the pipeline.
        """
        if self.on_phase_start is None:
            return
        with self._callback_lock:
            try:
                self.on_phase_start(phase_id, phase, wave_index)
            except Exception:  # noqa: BLE001
                pass  # Never let a callback crash the pipeline

    def _invoke_on_phase_complete(self, phase_id: str, result: dict) -> None:
        """Invoke ``on_phase_complete`` callback under the callback lock.

        Swallows all exceptions so a misbehaving callback never crashes
        the pipeline.
        """
        if self.on_phase_complete is None:
            return
        with self._callback_lock:
            try:
                self.on_phase_complete(phase_id, result)
            except Exception:  # noqa: BLE001
                pass  # Never let a callback crash the pipeline

    def _enrich_result_from_disk(self, phase_id: str, result: dict) -> None:
        """Enrich result["result"]["text"] with disk file content if larger.

        If the agent wrote a larger file to {output_dir}/{phase_id}.md during
        execution, update result["result"]["text"] in-place so all downstream
        consumers (_extract_phase_text, git handoff, iteration history,
        phase summary) automatically get the full content.

        Mutates result in-place. No return value.
        """
        if not self.output_dir:
            return
        disk_path = Path(self.output_dir) / f"{phase_id}.md"
        if not disk_path.exists():
            return
        disk_text = disk_path.read_text()
        if not disk_text:
            return
        chat_text = result.get("result", {}).get("text") or ""
        if len(disk_text) > len(chat_text):
            result.setdefault("result", {})["text"] = disk_text
            logger.info(
                f"Phase '{phase_id}': enriched result text from disk "
                f"({len(disk_text)} bytes > {len(chat_text)} bytes)"
            )

    def _record_adversary_outcome(self, phase: PhaseDefinition, result: dict) -> None:
        """Parse adversary verdict and persist reward when output_dir is available.

        Dispatch (Issue #702/#703): a phase carrying ``adversary_config`` uses the
        generic ``adversary_parser`` path; reward-persist errors there are swallowed
        so a reward-persist failure never crashes the pipeline. The legacy hardcoded
        ``spec_adversary`` dispatch was removed in #703 — a bare ``spec_adversary``
        phase with no ``adversary_config`` now raises a surfacing ``ValueError``
        (it does NOT dispatch to a deleted module). Any other config-less phase is a
        no-op.

        Args:
            phase:  The :class:`~.templates.PhaseDefinition` that just completed.
            result: The phase result dict (as returned by :meth:`_execute_and_wait`).

        Raises:
            ValueError: When ``phase.id == "spec_adversary"`` and
                ``phase.adversary_config is None`` (the removed-shim clear error).
        """
        if phase.adversary_config is not None:
            # ── Generic path (Issue #702) ──────────────────────────────────
            if not self.output_dir:
                logger.warning(
                    "Adversary phase %r completed but output_dir is None "
                    "— skipping reward persist",
                    phase.id,
                )
                return
            try:
                from .adversary_parser import (  # noqa: PLC0415
                    compute_reward,
                    parse_adversary_output,
                    persist_reward,
                )

                raw_text = _extract_phase_text(result)
                verdict = parse_adversary_output(raw_text, phase.adversary_config)
                if phase.adversary_config.reward_enabled:
                    reward = compute_reward(verdict, phase.adversary_config)
                    persist_reward(str(self.output_dir), verdict, reward, phase.adversary_config)
                    logger.info(
                        f"Pipeline {self.template.id}: {phase.id} verdict={verdict.verdict} "
                        f"reward={reward} findings={len(verdict.findings)}"
                    )
                else:
                    logger.info(
                        f"Pipeline {self.template.id}: {phase.id} verdict={verdict.verdict} "
                        f"findings={len(verdict.findings)} (reward_enabled=False — no persist)"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Pipeline {self.template.id}: {phase.id} reward persist failed: {exc}"
                )
            return

        # ── Removed legacy shim (Issue #703) ──────────────────────────────
        # A bare ``spec_adversary`` phase (no ``adversary_config``) used to fall
        # back to the deleted ``spec_adversary`` module. That hardcoded dispatch
        # is gone; surface a clear, actionable error instead of silently doing
        # nothing. This ``raise`` sits at the method's top level, OUTSIDE any
        # try/except, so it propagates to the caller (it is not swallowed).
        if phase.id == "spec_adversary":
            raise ValueError(
                f"Pipeline {self.template.id}: phase 'spec_adversary' has no "
                f"adversary_config. The legacy hardcoded spec_adversary dispatch "
                f"was removed in #703 — add an adversary_config block (valid_categories, "
                f"fallback_category, verdict_scan) to this phase to use the generic "
                f"adversary path."
            )
        return

    def _record_review_outcome(self, phase: PhaseDefinition, result: dict) -> None:
        """Persist a review outcome to the DB when ``run_id`` and ``db`` are set.

        Only acts on phases whose ``task_type`` is ``"review"``.  All errors
        are swallowed so a DB failure never crashes the pipeline.

        The method parses the phase output text through
        :func:`~.review_parser.parse_review_output` to extract the structured
        verdict and issues, then upserts a ``ReviewOutcome`` row via
        ``db.insert_review_outcome``.

        Args:
            phase:  The :class:`~.templates.PhaseDefinition` that just completed.
            result: The phase result dict (as returned by
                    :meth:`_execute_and_wait`).
        """
        # Guard: only record for review-typed phases, and only when run_id + db are available
        if not self.run_id or self.db is None:
            return
        if getattr(phase, "task_type", None) != "review":
            return

        try:
            raw_text = _extract_phase_text(result)
            parsed = parse_review_output(raw_text)

            # Convert ReviewIssue objects to serialisable dicts
            issues_list = [
                {
                    "severity": issue.severity.value,
                    "category": issue.category,
                    "description": issue.description,
                }
                for issue in parsed.issues
            ]

            # Resolve the model used — prefer metadata, fall back to phase definition
            reviewer_model: str = str(
                result.get("metadata", {}).get("model")
                or result.get("model")
                or getattr(phase, "model_tier", "unknown")
                or "unknown"
            )

            outcome = ReviewOutcome(
                review_id=str(uuid.uuid4()),
                run_id=self.run_id,
                phase_id=phase.id,
                reviewer_model=reviewer_model,
                verdict=parsed.verdict,
                issues_found=issues_list,
                fix_verified=False,
                created_at=now_utc().isoformat(),
            )

            self.db.insert_review_outcome(
                {
                    "review_id": outcome.review_id,
                    "run_id": outcome.run_id,
                    "phase_id": outcome.phase_id,
                    "reviewer_model": outcome.reviewer_model,
                    "verdict": outcome.verdict,
                    "issues_found": outcome.issues_found,
                    "fix_verified": outcome.fix_verified,
                    "created_at": outcome.created_at,
                }
            )

            logger.info(
                f"Pipeline {self.template.id}: recorded review outcome "
                f"(review_id={outcome.review_id}, phase={phase.id}, "
                f"verdict={outcome.verdict}, issues={len(issues_list)})"
            )
        except Exception as exc:  # noqa: BLE001
            # Never let a DB failure crash the pipeline
            logger.warning(
                f"Pipeline {self.template.id}: failed to record review outcome "
                f"for phase '{phase.id}': {exc}"
            )

    # ------------------------------------------------------------------
    # File-write integration (#189)
    # ------------------------------------------------------------------

    def _handle_file_write(self, phase: PhaseDefinition, result: dict) -> None:
        """Parse FILE blocks from *result* and write them to ``phase.working_dir``.

        Modifies *result* in-place: sets ``result["metadata"]["files_written"]``
        to the list of relative paths successfully written (empty list on
        dry-run or when no files were produced).

        Dry-run mode
        ------------
        When ``self.config["dry_run"]`` is truthy, files are **parsed but not
        written**.  The paths that *would* have been written are logged at INFO
        level and ``files_written`` is set to ``[]``.

        Safety
        ------
        ``phase.working_dir`` must resolve to a path **inside**
        ``phase.base_dir`` (or inside ``working_dir`` itself when ``base_dir``
        is empty).  If this check fails an ERROR is logged and no files are
        written.

        Args:
            phase:  The phase definition (source of ``write_files``,
                    ``working_dir``, ``base_dir``).
            result: Mutable result dict (TaskResult.model_dump()); updated
                    in-place with ``metadata["files_written"]``.
        """
        # Only write on successful phases; leave metadata untouched on failure
        # so callers can detect the difference between "wrote nothing" and "skipped".
        phase_state = result.get("state", "")
        if phase_state in ("failed", "permanently_failed"):
            logger.debug(
                f"Phase '{phase.id}': write_files skipped — phase state is '{phase_state}'"
            )
            return

        text = _extract_phase_text(result)
        if not text:
            result.setdefault("metadata", {})["files_written"] = []
            return

        # ── Path resolution & safety check ───────────────────────────────────
        working_dir = Path(phase.working_dir).expanduser().resolve()
        if phase.base_dir:
            base_dir = Path(phase.base_dir).expanduser().resolve()
        else:
            # No explicit base_dir → working_dir is its own safety boundary
            base_dir = working_dir

        try:
            working_dir.relative_to(base_dir)
        except ValueError:
            logger.error(
                f"Phase '{phase.id}': working_dir {str(working_dir)!r} resolves "
                f"outside base_dir {str(base_dir)!r} — refusing file write"
            )
            result.setdefault("metadata", {})["files_written"] = []
            return

        # ── Dry-run: parse but don't write ───────────────────────────────────
        dry_run = bool(self.config.get("dry_run", False))
        if dry_run:
            parsed = parse_output(text)
            paths = [fb.path for fb in parsed.files]
            if paths:
                logger.info(
                    f"Phase '{phase.id}': dry-run — would write {len(paths)} "
                    f"file(s) to {str(working_dir)!r}: {paths}"
                )
            else:
                logger.debug(f"Phase '{phase.id}': dry-run — no FILE blocks found in output")
            result.setdefault("metadata", {})["files_written"] = []
            return

        # ── Normal mode: call extract_and_write ───────────────────────────────
        written = extract_and_write(text, working_dir)
        paths = [fb.path for fb in written]
        if paths:
            logger.info(
                f"Phase '{phase.id}': wrote {len(paths)} file(s) to "
                f"{str(working_dir)!r}: {paths}"
            )
        else:
            logger.debug(
                f"Phase '{phase.id}': write_files enabled but no FILE blocks "
                f"found/written in output"
            )
        result.setdefault("metadata", {})["files_written"] = paths

    # ------------------------------------------------------------------
    # Supervisor hook (Issue #194)
    # ------------------------------------------------------------------

    def _run_supervisor_for_phase(
        self,
        phase: PhaseDefinition,
        result: dict,
        initial_input: dict,
    ):
        """Run the supervisor evaluation loop for a completed phase.

        Called after a phase with ``supervisor: True`` completes successfully.
        Builds a supervisor TaskSpec, executes it, parses APPROVE/REVISE/ABORT,
        and handles each outcome:

        * **APPROVE** — returns ``(result, None)`` to continue the pipeline.
        * **REVISE**  — re-runs the phase with the supervisor's feedback injected
          as a failure context, then loops back.  Bounded by
          ``phase.supervisor_max_retries``.
        * **ABORT**   — returns ``(result, abort_dict)`` to fail the pipeline.
        * **Max retries exhausted** — same as ABORT.

        Args:
            phase:         The PhaseDefinition that just completed.
            result:        The phase result dict (TaskResult.model_dump()).
            initial_input: The pipeline's initial input dict.

        Returns:
            ``(final_result, abort_dict)`` where ``abort_dict`` is ``None``
            on APPROVE, or a pipeline-abort dict on ABORT / exhaustion.
        """
        max_retries: int = phase.supervisor_max_retries
        revise_count: int = 0
        current_result: dict = result

        while True:
            phase_output = _extract_phase_text(current_result)
            rubric = phase.supervisor_rubric or "(no rubric provided)"

            # Build supervisor prompt
            if phase.supervisor_prompt:
                supervisor_prompt_text = phase.supervisor_prompt.format(
                    rubric=rubric,
                    phase_output=phase_output,
                )
            else:
                supervisor_prompt_text = _DEFAULT_SUPERVISOR_PROMPT.format(
                    rubric=rubric,
                    phase_output=phase_output,
                )

            supervisor_model = phase.supervisor_model or "opus"
            preferred_model = self._resolve_model_tier(supervisor_model)

            supervisor_task = TaskSpec(
                type=TaskType.CONTENT,
                payload={
                    "prompt": supervisor_prompt_text,
                    "phase_id": f"{phase.id}__supervisor",
                    "pipeline_id": self.template.id,
                    "model_chain": phase.model_chain or [],  # #347: propagate fallback chain
                    "sandbox_roots": self._sandbox_roots(),  # #794: tool-call sandbox
                },
                priority=Priority.HIGH,
                preferred_model=preferred_model,
                timeout_seconds=phase.timeout_minutes * 60,
            )

            sup_task_id = self.runner.queue.submit_task(supervisor_task)
            logger.info(
                f"Pipeline {self.template.id}: running supervisor for phase "
                f"'{phase.id}' (revise_count={revise_count}/{max_retries})"
            )

            # Create a minimal PhaseDefinition for _execute_and_wait bookkeeping
            supervisor_phase = PhaseDefinition(
                id=f"{phase.id}__supervisor",
                name=f"{phase.name} Supervisor",
                prompt_template=supervisor_prompt_text,
                model_tier=supervisor_model,
                retries=0,
            )

            supervisor_result = self._execute_and_wait(sup_task_id, supervisor_phase)
            supervisor_text = _extract_phase_text(supervisor_result)

            verdict, reason = self._parse_supervisor_response(supervisor_text)
            logger.info(
                f"Pipeline {self.template.id}: supervisor verdict for phase "
                f"'{phase.id}': {verdict} — {reason[:200]}"
            )

            if verdict == "APPROVE":
                return current_result, None

            elif verdict == "ABORT":
                logger.error(
                    f"Pipeline {self.template.id}: supervisor ABORT on phase "
                    f"'{phase.id}': {reason}"
                )
                abort_result = {
                    "phase_outputs": self.phase_outputs,
                    "final_output": current_result,
                    "failed_phase": phase.id,
                    "aborted": True,
                    "supervisor_abort": True,
                    "supervisor_reason": reason,
                }
                return current_result, abort_result

            elif verdict == "REVISE":
                if revise_count >= max_retries:
                    logger.error(
                        f"Pipeline {self.template.id}: supervisor max_retries "
                        f"({max_retries}) exhausted for phase '{phase.id}' — aborting."
                    )
                    abort_result = {
                        "phase_outputs": self.phase_outputs,
                        "final_output": current_result,
                        "failed_phase": phase.id,
                        "aborted": True,
                        "supervisor_abort": True,
                        "supervisor_reason": (f"max_retries ({max_retries}) exhausted: {reason}"),
                    }
                    return current_result, abort_result

                revise_count += 1
                logger.info(
                    f"Pipeline {self.template.id}: supervisor REVISE on phase "
                    f"'{phase.id}' (attempt {revise_count}/{max_retries}): {reason}"
                )

                # Re-run phase with supervisor feedback injected as failure context
                feedback_ctx = (
                    f"## Supervisor Feedback\n\n"
                    f"The supervisor reviewed your previous output and requested revisions:\n\n"
                    f"{reason}\n\n"
                    f"Please revise your output accordingly."
                )

                with self._phase_outputs_lock:
                    revised_prompt = self._build_phase_input(
                        phase, initial_input, failure_context=feedback_ctx
                    )

                revised_task = TaskSpec(
                    type=self._resolve_task_type(phase.task_type),
                    payload={
                        "prompt": revised_prompt,
                        "phase_id": phase.id,
                        "pipeline_id": self.template.id,
                        "model_chain": phase.model_chain or [],  # #347: propagate fallback chain
                        "sandbox_roots": self._sandbox_roots(),  # #794: tool-call sandbox
                        **self._build_command_extras(phase, initial_input),
                    },
                    priority=Priority.HIGH,
                    preferred_model=self._resolve_model_tier(phase.model_tier),
                    timeout_seconds=phase.timeout_minutes * 60,
                )

                revised_task_id = self.runner.queue.submit_task(revised_task)
                current_result = self._execute_and_wait(
                    revised_task_id, phase, initial_input=initial_input
                )

                # Store the latest result under lock
                with self._phase_outputs_lock:
                    self.phase_outputs[phase.id] = current_result

                # If the revised phase itself failed, abort
                revised_state = current_result.get("state", "unknown")
                if revised_state in ("failed", "permanently_failed"):
                    return current_result, {
                        "phase_outputs": self.phase_outputs,
                        "final_output": current_result,
                        "failed_phase": phase.id,
                        "aborted": True,
                    }
                # Loop back to evaluate the new output

    @staticmethod
    def _parse_supervisor_response(text: str):
        """Parse supervisor text response for APPROVE / REVISE / ABORT.

        Scans each line and checks its first word (case-insensitive).
        Returns ``("APPROVE" | "REVISE" | "ABORT" | "UNKNOWN", reason_str)``.
        On no match defaults to APPROVE with a warning.
        """
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            for verdict in ("APPROVE", "REVISE", "ABORT"):
                if upper.startswith(verdict):
                    # Extract reason after the keyword (and optional colon/space)
                    remainder = stripped[len(verdict) :].lstrip(":").strip()
                    return verdict, remainder
        logger.warning(
            f"Supervisor response had no APPROVE/REVISE/ABORT verdict; "
            f"defaulting to APPROVE. Response preview: {text[:200]!r}"
        )
        return "APPROVE", "no verdict found — defaulting to APPROVE"

    # ------------------------------------------------------------------
    # Output length validation (Issue #351)
    # ------------------------------------------------------------------

    def _validate_phase_output(
        self,
        phase: "PhaseDefinition",
        result: dict,
    ) -> Optional[dict]:
        """Validate the character length of a successfully completed phase output.

        Only runs when:

        * The result state is ``"success"`` (failures are already handled
          upstream, so we only gate *successful* outputs here).
        * ``phase.min_output_length > 0`` (validation is disabled by default).

        Behaviour:

        * **Length below threshold** — returns a synthetic FAILED result dict
          with a clear diagnostic message (actual length, threshold, and the
          last 50 characters of the output so the operator can see where
          truncation happened).
        * **Possible mid-sentence truncation** — when the output passes the
          length check but its last 50 characters contain no terminal
          punctuation (``'.', '!', '?', ':'``), emits a WARNING log.  The
          pipeline continues normally; this is advisory only.
        * **Validation passes** — returns ``None`` (no intervention).

        Args:
            phase:  The :class:`~.templates.PhaseDefinition` that produced
                    *result*.
            result: The phase result dict (as returned by
                    :meth:`_execute_and_wait`).

        Returns:
            ``None`` when validation passes, or a synthetic FAILED result dict
            when the output is shorter than ``phase.min_output_length``.
        """
        # Only validate successful phases — failures are handled by normal abort path
        if result.get("state") != "success":
            return None

        min_len = getattr(phase, "min_output_length", 0)
        if min_len <= 0:
            return None  # validation disabled

        text = _extract_phase_text(result)
        actual_len = len(text)
        tail = text[-50:] if len(text) >= 50 else text

        if actual_len < min_len:
            error_msg = (
                f"Phase '{phase.id}' output too short: "
                f"got {actual_len} chars, expected at least {min_len}. "
                f"Last 50 chars: {tail!r}"
            )
            logger.error(f"Pipeline {self.template.id}: {error_msg} — treating as failure.")
            # Build a synthetic FAILED result so the pipeline abort path handles it
            # consistently with other phase failures.
            failed_result: dict = {
                "state": TaskState.FAILED.value,
                "result": result.get("result", {"text": text}),
                "errors": [
                    {
                        "code": "OUTPUT_TOO_SHORT",
                        "message": error_msg,
                        "severity": "error",
                    }
                ],
                "metadata": {
                    **result.get("metadata", {}),
                    "validation_failure": "min_output_length",
                    "actual_length": actual_len,
                    "min_output_length": min_len,
                    "tail": tail,
                },
                "confidence": 0.0,
            }
            return failed_result

        # Length passes — check for mid-sentence truncation (advisory warning)
        if tail and not any(ch in _TERMINAL_PUNCTUATION for ch in tail):
            logger.warning(
                f"Pipeline {self.template.id}: phase '{phase.id}' output may be "
                f"truncated mid-sentence — last 50 chars contain no terminal "
                f"punctuation (., !, ?, :). Last 50 chars: {tail!r}"
            )

        return None  # validation passed

    # ------------------------------------------------------------------
    # File-guard hash helpers (Issue #531)
    # ------------------------------------------------------------------

    def _maybe_snapshot_on_approve(
        self,
        phase: "PhaseDefinition",
        next_phase_id: Optional[str],
        is_exhausted: bool = False,
    ) -> None:
        """Snapshot protect_on_approve paths when an adversary phase approves or is exhausted.

        Called immediately after :meth:`_resolve_next_phase` at both call sites
        in the sequencer loop. Does nothing if:
        - ``phase.protect_on_approve`` is empty, OR
        - the transition is neither an APPROVE verdict nor an EXHAUSTED outcome.

        Approval detection: checks whether ``next_phase_id`` matches the target
        declared under the ``approve`` transition key in the phase's effective
        transitions. This avoids re-running verdict extraction and is purely
        structural — if the router picked the approve target, the verdict was approve.

        On APPROVE or EXHAUSTED, iterates ``phase.protect_on_approve``, resolves
        each path, and records its SHA256 hash in ``self._protected_hashes``.
        Subsequent phases are then subject to :meth:`_verify_protected_hashes`
        checks. The adversary phase itself is exempt (checked by the adversary's
        own phase id vs the ``_protect_on_approve_source_phase`` attribute).

        Path resolution:
        - Absolute paths are used as-is.
        - Relative paths are resolved against ``self.output_dir``.
        - Missing files emit a WARNING and are skipped (graceful degradation).

        Args:
            phase:         The adversary phase that just completed.
            next_phase_id: The phase ID resolved by ``_resolve_next_phase``.
            is_exhausted:  ``True`` when the phase was routed via EXHAUSTED outcome
                           (implicit approval).
        """
        poa_paths = getattr(phase, "protect_on_approve", [])
        if not poa_paths:
            return  # fast path — nothing declared

        # Determine whether this is an approve transition.
        # For the exhausted call site, is_exhausted=True is passed explicitly.
        # For the normal call site, check whether next_phase_id matches the
        # approve transition target (structural approval detection).
        if is_exhausted:
            is_approve_transition = True
        else:
            effective: dict = {
                **self.template.default_transitions,
                **phase.transitions,
            }
            approve_target = effective.get("approve")
            is_approve_transition = approve_target is not None and next_phase_id == approve_target

        if not is_approve_transition:
            return  # only snapshot on approve / exhausted

        if not self.output_dir:
            logger.warning(
                f"Phase '{phase.id}': protect_on_approve declared but output_dir is None "
                f"— skipping snapshot"
            )
            return

        logger.info(
            f"Pipeline {self.template.id}: phase '{phase.id}' approved — "
            f"snapshotting {len(poa_paths)} protect_on_approve path(s)"
        )

        # Record which adversary phase owns these protections so the adversary
        # itself is exempt from its own protection during re-invocation.
        if not hasattr(self, "_protect_on_approve_source_phase"):
            self._protect_on_approve_source_phase: Optional[str] = None
        self._protect_on_approve_source_phase = phase.id

        with self._phase_outputs_lock:
            for raw_path in poa_paths:
                if Path(raw_path).is_absolute():
                    abs_path = raw_path
                else:
                    abs_path = str(Path(self.output_dir) / raw_path)
                try:
                    digest = compute_hash(abs_path)
                    self._protected_hashes[abs_path] = digest
                    logger.debug(
                        f"Phase '{phase.id}': protect_on_approve snapshotted "
                        f"'{raw_path}' → sha256:{digest[:16]}…"
                    )
                except FileNotFoundError:
                    logger.warning(
                        f"Phase '{phase.id}': protect_on_approve path '{raw_path}' "
                        f"not found at snapshot time — skipping"
                    )

    def _store_protected_hashes(self, phase: "PhaseDefinition") -> None:
        """Compute and store SHA256 hashes for all files in phase.protected_outputs.

        Called after a successful phase that has a non-empty protected_outputs list.
        Hashes are stored in self._protected_hashes keyed by absolute file path.

        Graceful degradation:
        - If output_dir is None, logs a WARNING and returns without storing.
        - If a file does not exist, logs a WARNING and skips that file.

        Args:
            phase: The phase definition whose protected_outputs to hash.
        """
        if not self.output_dir:
            logger.warning(
                f"Phase '{phase.id}': protected_outputs declared but output_dir is None "
                f"— skipping hash computation"
            )
            return

        with self._phase_outputs_lock:
            for filename in getattr(phase, "protected_outputs", []):
                abs_path = str(Path(self.output_dir) / filename)
                try:
                    digest = compute_hash(abs_path)
                    self._protected_hashes[abs_path] = digest
                    logger.debug(
                        f"Phase '{phase.id}': stored hash for protected output "
                        f"'{filename}' → sha256:{digest[:16]}…"
                    )
                except FileNotFoundError:
                    logger.warning(
                        f"Phase '{phase.id}': protected output '{filename}' not found "
                        f"after phase completion — skipping hash"
                    )

    def _verify_protected_hashes(self, phase: "PhaseDefinition") -> Optional[dict]:
        """Verify all stored protected-file hashes before accepting a phase's output.

        Fast path: if _protected_hashes is empty, returns None immediately
        (zero overhead on pipelines without protected_outputs).

        For each stored path → expected hash pair, re-computes the hash and
        compares. On mismatch or deletion, returns a synthetic FAILED result dict
        with error code "PROTECTED_FILE_MODIFIED" and a human-readable message.

        Adversary exemption (Issue #718): The adversary phase that owns the
        protect_on_approve protections is exempt from its own hash verification.
        This allows the adversary to re-run (e.g. in a review loop) without
        being blocked by its own protection. Non-adversary downstream phases
        remain subject to the protection.

        Args:
            phase: The phase whose output is about to be accepted (used for error context).

        Returns:
            None if all hashes match (or no hashes stored).
            A synthetic FAILED result dict on any hash mismatch or file deletion.
        """
        if not self._protected_hashes:
            return None  # fast path — no protected outputs

        # Adversary exemption: the phase that owns protect_on_approve is not
        # subject to its own protection (Issue #718)
        if (
            self._protect_on_approve_source_phase is not None
            and phase.id == self._protect_on_approve_source_phase
        ):
            return None

        with self._phase_outputs_lock:
            items = list(self._protected_hashes.items())

        for abs_path, expected in items:
            filename = Path(abs_path).name
            try:
                actual = compute_hash(abs_path)
                if actual != expected:
                    msg = (
                        f"Protected file modified: {filename} "
                        f"(expected sha256:{expected}, got sha256:{actual})"
                    )
                    logger.error(
                        f"Pipeline {self.template.id}: file-guard FAILED "
                        f"on phase '{phase.id}': {msg}"
                    )
                    return {
                        "state": TaskState.FAILED.value,
                        "result": {"text": ""},
                        "errors": [
                            {
                                "code": "PROTECTED_FILE_MODIFIED",
                                "message": msg,
                                "severity": "error",
                            }
                        ],
                        "metadata": {
                            "attempt_number": 1,
                            "total_attempts": 1,
                            "file_guard_failure": True,
                        },
                        "confidence": 0.0,
                    }
            except FileNotFoundError:
                msg = (
                    f"Protected file deleted: {filename} "
                    f"(expected sha256:{expected}, file not found)"
                )
                logger.error(
                    f"Pipeline {self.template.id}: file-guard FAILED "
                    f"on phase '{phase.id}': {msg}"
                )
                return {
                    "state": TaskState.FAILED.value,
                    "result": {"text": ""},
                    "errors": [
                        {
                            "code": "PROTECTED_FILE_MODIFIED",
                            "message": msg,
                            "severity": "error",
                        }
                    ],
                    "metadata": {
                        "attempt_number": 1,
                        "total_attempts": 1,
                        "file_guard_failure": True,
                    },
                    "confidence": 0.0,
                }

        return None  # all hashes match

    # ------------------------------------------------------------------
    # Protected-path guard helpers (Issue #706)
    # ------------------------------------------------------------------

    def _resolve_protected_path(self, raw_path: str) -> Optional[str]:
        """Resolve a protected_paths entry to an absolute path.

        Resolution order (mirrors behavioral contract):
        1. If absolute path — use as-is.
        2. config["repo_path"] if present — join relative path against it.
        3. self.working_dir (the sequencer's own working directory) if set.
        4. Otherwise log a WARNING and return None (skip this path).

        output_dir is explicitly NOT used — it is for pipeline artifacts.

        Args:
            raw_path: The protected_paths entry (relative or absolute string).

        Returns:
            Absolute path string, or None if resolution failed.
        """
        p = Path(raw_path)
        if p.is_absolute():
            return str(p)

        # Primary: config["repo_path"]
        repo_path = self.config.get("repo_path")
        if repo_path:
            return str(Path(repo_path) / raw_path)

        # Fallback: sequencer working_dir attribute (may be set by subclasses)
        wd = getattr(self, "working_dir", None)
        if wd:
            return str(Path(wd) / raw_path)

        logger.warning(
            "Phase: protected_paths entry %r could not be resolved — "
            "config['repo_path'] and working_dir are both absent. Skipping.",
            raw_path,
        )
        return None

    def _snapshot_protected_paths(self, phase: "PhaseDefinition") -> Dict[str, str]:
        """Compute and return directory hashes for all phase.protected_paths.

        Returns a **local** dict rather than writing to shared instance state,
        making it safe to call concurrently for different phases in a parallel
        wave (fixes thread-safety race condition).

        Fast path: returns an empty dict immediately when protected_paths is empty.
        Paths that don't exist or can't be resolved are logged as WARNINGs
        and skipped (no FAIL).

        Args:
            phase: The phase about to execute.

        Returns:
            Dict mapping absolute path string → SHA256 hex digest.
        """
        snapshots: Dict[str, str] = {}
        raw_paths = getattr(phase, "protected_paths", None) or []
        if not raw_paths:
            return snapshots  # fast path — zero overhead

        for raw_path in raw_paths:
            abs_path = self._resolve_protected_path(raw_path)
            if abs_path is None:
                continue  # warning already logged
            # compute_directory_hash returns None (not raises) for missing/non-dir paths;
            # it logs a WARNING internally.  We just skip storing the snapshot.
            digest = compute_directory_hash(abs_path)
            if digest is None:
                logger.warning(
                    "Phase '%s': protected_path '%s' could not be hashed at snapshot time — "
                    "skipping (path missing or not a directory).",
                    phase.id,
                    abs_path,
                )
                continue
            snapshots[abs_path] = digest
            logger.debug(
                "Phase '%s': snapshot protected_path '%s' → sha256:%s…",
                phase.id,
                abs_path,
                digest[:16],
            )
        return snapshots

    def _verify_protected_paths(
        self,
        phase: "PhaseDefinition",
        snapshots: Dict[str, str],
    ) -> Optional[dict]:
        """Re-hash all snapshotted protected_paths and compare against pre-execution digests.

        Fast path: returns None immediately when *snapshots* is empty.

        The *snapshots* dict is passed in explicitly rather than read from
        instance state, ensuring this method is thread-safe when called
        concurrently for different phases in a parallel wave.

        On any mismatch, returns a synthetic FAILED result dict with:
        - error_code: "PROTECTED_PATH_MODIFIED"
        - protected_path: the directory that changed
        - expected_hash: sha256 before phase execution
        - actual_hash: sha256 after phase execution

        Args:
            phase: The phase whose output is being verified.
            snapshots: Dict mapping absolute path → pre-execution SHA256 digest,
                as returned by :meth:`_snapshot_protected_paths`.

        Returns:
            None if all hashes match (or no snapshots).
            Synthetic FAILED result dict on first mismatch detected.
        """
        if not snapshots:
            return None  # fast path

        for abs_path, expected in snapshots.items():
            actual = compute_directory_hash(abs_path)
            if actual is None:
                # Path vanished after snapshot — treat as a modification
                actual = "<missing after snapshot>"

            if actual != expected:
                msg = (
                    f"Protected path modified: {abs_path} "
                    f"(expected sha256:{expected[:16]}…, got sha256:{actual[:16] if len(actual) >= 16 else actual}…)"  # noqa: E501
                )
                logger.error(
                    "Pipeline %s: folder-guard FAILED on phase '%s': %s",
                    self.template.id,
                    phase.id,
                    msg,
                )
                return {
                    "state": "failed",
                    "result": {"text": ""},
                    "error_code": "PROTECTED_PATH_MODIFIED",
                    "protected_path": abs_path,
                    "expected_hash": expected,
                    "actual_hash": actual,
                    "errors": [
                        {
                            "code": "PROTECTED_PATH_MODIFIED",
                            "message": msg,
                            "severity": "error",
                        }
                    ],
                    "metadata": {
                        "attempt_number": 1,
                        "total_attempts": 1,
                        "folder_guard_failure": True,
                    },
                    "confidence": 0.0,
                }

        return None  # all paths unchanged

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_phase(self, phase_id: str) -> PhaseDefinition:
        """Retrieve a PhaseDefinition by ID from the template.

        Raises:
            KeyError: If the phase is not found (should not happen after
                      validation, but guard anyway).
        """
        for phase in self.template.phases:
            if phase.id == phase_id:
                return phase
        raise KeyError(f"Phase '{phase_id}' not found in template '{self.template.id}'")

    def _build_phase_input(  # noqa: C901
        self,
        phase: PhaseDefinition,
        initial_input: dict,
        failure_context: str = "",
        iteration_history: str = "",
        missing_sink: Optional[set] = None,
        **extra_format_vars,
    ) -> str:
        """Build the prompt string for a phase.

        Uses Python's ``str.format()`` to interpolate:
        - ``{input}``                     — the initial pipeline input dict
        - ``{input[key]}``                — a specific key from the initial input
        - ``{previous_output}``           — when ``output_dir`` is set: a compact
          summary of prior phase outputs (phase name + word count + file path),
          NOT the full inline content; when ``output_dir`` is ``None``, behaves
          exactly as before (raw dict repr of all phase outputs).
        - ``{previous_output[phase_id]}`` — when ``output_dir`` is set: prepends
          ``"Full output at: {output_dir}/{safe_pid}.md\\n\\n"`` before the
          inline content; without ``output_dir``: raw extracted text only.
        - ``{previous_output_inline}``    — always the full raw dict repr of all
          prior phase outputs, regardless of ``output_dir``; use in templates
          that explicitly need every prior output inline.
        - ``{config}``                    — the pipeline config dict
        - ``{skill_context[name]}``       — content of a loaded skill file
          (from skill_refs)
        - ``{failure_context}``           — markdown-formatted context from the
          previous failed attempt (empty string on the first attempt).
        - ``{output_dir}``                — string representation of the output
          directory, or ``"<NO_OUTPUT_DIR>"`` if unset.
        - ``{phase_summary}``             — brief summary of prior phases
          (phase name + word count + file path) regardless of ``output_dir``.

        Missing keys produce a ``<MISSING:key>`` placeholder (via SafeDict)
        rather than raising ``KeyError``.  Missing ``{previous_output[id]}``
        keys produce ``<MISSING:previous_output[id]>``.

        .. note::
            When called from a parallel worker, **the caller must hold
            ``self._phase_outputs_lock``** before invoking this method so that
            the snapshot of ``self.phase_outputs`` is consistent.

        Args:
            phase:           The phase definition.
            initial_input:   Pipeline input dict.
            failure_context: Markdown-formatted failure context from the previous
                             attempt.  Empty string on the first attempt.  Curly
                             braces are escaped before injection so that error
                             messages containing ``{`` / ``}`` do not confuse
                             ``str.format()``.
        """
        if not phase.prompt_template:
            return ""

        # Wrap dicts in a safe mapping that returns a placeholder for missing keys.
        # `missing_sink` (when provided by the dispatch-level guard) collects the
        # markers that THESE config/input/previous_output substitutions emit, so
        # the guard can reject genuinely-missing references without false-firing
        # on <MISSING:...> text that arrives via inlined context_files /
        # skill_context / {context.*} placeholders (#535).
        safe_input = _SafeDict(initial_input, missing_sink=missing_sink)
        safe_config = _SafeDict(self.config, missing_sink=missing_sink)

        # ── fix/243: smart previous_output proxy ──────────────────────────────
        # When output_dir is set, {previous_output} emits compact file-path
        # summaries instead of dumping all content inline.
        # {previous_output_inline} always gives the full raw content regardless.
        _output_dir_for_proxy = str(self.output_dir) if self.output_dir else None
        previous_output_proxy = _PreviousOutputProxy(
            self.phase_outputs,
            _output_dir_for_proxy,
            self._phase_map,
            missing_sink=missing_sink,
        )
        previous_output_inline_proxy = _PreviousOutputInlineProxy(
            self.phase_outputs, missing_sink=missing_sink
        )

        # Load skill_refs and build skill_context dict
        skill_context: Dict[str, str] = {}
        if phase.skill_refs:
            template_dir = (
                self.template.template_path.parent
                if self.template.template_path is not None
                else None
            )
            for skill_ref in phase.skill_refs:
                try:
                    skill_name, skill_content = self._load_skill(skill_ref, template_dir)
                    skill_context[skill_name] = skill_content
                except Exception as exc:  # noqa: BLE001, PERF203
                    logger.warning(
                        f"Phase '{phase.id}': failed to load skill_ref '{skill_ref}' — {exc}"
                    )
                    skill_context[Path(skill_ref).stem] = f"<SKILL_LOAD_ERROR:{skill_ref}>"

        safe_skills = _SafeDict(skill_context)

        # Load context_files and build file_context dict
        file_context: Dict[str, str] = {}
        if hasattr(phase, "context_files") and phase.context_files:
            for file_path in phase.context_files:
                try:
                    p = Path(file_path).expanduser()
                    if p.exists():
                        content = p.read_text(errors="replace")
                        # Use filename (without extension) as key
                        key = p.stem.replace("-", "_").replace(".", "_")
                        file_context[key] = content
                        logger.debug(
                            f"Phase '{phase.id}': loaded context file '{file_path}' ({len(content)} chars)"  # noqa: E501
                        )
                    else:
                        logger.warning(f"Phase '{phase.id}': context file not found: {file_path}")
                        file_context[p.stem] = f"<FILE_NOT_FOUND:{file_path}>"
                except Exception as exc:  # noqa: BLE001, PERF203
                    logger.warning(
                        f"Phase '{phase.id}': failed to read context file '{file_path}' — {exc}"
                    )
                    file_context[Path(file_path).stem] = f"<FILE_READ_ERROR:{file_path}>"
        safe_files = _SafeDict(file_context)

        # Build per-phase keyword args so templates can use {phase_id.output}
        # in addition to {previous_output[phase_id]}
        # Prefer on-disk output files when available — they contain the full
        # text even if in-memory capture was truncated (#239 follow-up).
        phase_kwargs: Dict[str, _PhaseOutput] = {}
        for pid, pout in self.phase_outputs.items():
            in_memory = _extract_phase_text(pout)
            # Check if an on-disk file exists with more content
            if self.output_dir:
                safe_pid = pid.replace("-", "_")
                disk_path = Path(self.output_dir) / f"{safe_pid}.md"
                if disk_path.exists():
                    try:
                        disk_text = disk_path.read_text()
                        # Strip the "# Phase: ...\n\n" header added by CLI
                        if disk_text.startswith("# Phase:"):
                            disk_text = disk_text.split("\n", 2)[-1].strip()
                        if len(disk_text) > len(in_memory):
                            logger.info(
                                f"Phase '{pid}': using on-disk output "
                                f"({len(disk_text)} chars) over in-memory "
                                f"({len(in_memory)} chars)"
                            )
                            in_memory = disk_text
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"Phase '{pid}': failed to read disk output: {exc}")
            phase_kwargs[pid] = _PhaseOutput(in_memory)

        # Wrap pipeline_context so {context.key} works in prompt templates
        safe_context = _SafeDict(self.pipeline_context)

        # Escape curly braces in failure_context so that error messages
        # containing { or } do not confuse str.format() when the value is
        # injected into the template before interpolation.
        escaped_failure_context = failure_context.replace("{", "{{").replace("}", "}}")

        # ── v2.7: Provide output_dir and phase_summary for file-path handoff ──
        output_dir_str = str(self.output_dir) if self.output_dir else "<NO_OUTPUT_DIR>"

        # Build a brief summary of what prior phases produced (phase name + word count)
        summary_lines = []
        for pid in self.phase_outputs:
            text = _extract_phase_text(self.phase_outputs[pid])
            word_count = len(text.split()) if text else 0
            phase_def = self._phase_map.get(pid)
            phase_name = phase_def.name if phase_def else pid
            summary_lines.append(
                f"- {phase_name} ({pid}): completed, ~{word_count} words → {output_dir_str}/{pid.replace('-', '_')}.md"  # noqa: E501
            )
        phase_summary = (
            "\n".join(summary_lines)
            if summary_lines
            else "This is the first phase — no prior work."
        )

        try:
            prompt = phase.prompt_template.format(
                input=safe_input,
                previous_output=previous_output_proxy,
                previous_output_inline=previous_output_inline_proxy,
                config=safe_config,
                skill_context=safe_skills,
                files=safe_files,
                context=safe_context,
                failure_context=escaped_failure_context,
                output_dir=output_dir_str,
                phase_summary=phase_summary,
                iteration_history=iteration_history,
                **extra_format_vars,
                **phase_kwargs,
            )
        except (KeyError, IndexError, AttributeError) as exc:
            logger.warning(
                f"Phase '{phase.id}': format error in prompt template — {exc}. "
                f"Returning raw template."
            )
            prompt = phase.prompt_template

        return prompt

    def _is_dry_run_mode(self) -> bool:
        """Return True when the pipeline is running in dry-run mode.

        Two independent signals are honoured so all entry points are covered:

        * ``self.config["dry_run"]`` is truthy (some callers set this flag on
          the config dict; also used by ``_handle_file_write``), OR
        * every registered executor is a ``DryRunExecutor`` (the CLI/web/daemon
          dry-run paths construct the runner via ``PipelineRunner.dry_run()``).
          This mirrors the existing dry-run detection idiom at
          :meth:`_resolve_dialogue_executor`.

        Dry-run mode is deliberately forgiving of missing input/config so
        operators can smoke-test pipeline structure (issue #659); the
        unresolved-placeholder guard (#535) therefore does not abort in dry-run.
        """
        try:
            if self.config.get("dry_run"):
                return True
        except AttributeError:
            pass
        try:
            executors = list(getattr(self.runner, "executors", []) or [])
        except Exception:  # noqa: BLE001
            return False
        if not executors:
            return False
        return all("dryrun" in type(ex).__name__.lower() for ex in executors)

    def _check_for_unresolved_placeholders(
        self,
        phase: "PhaseDefinition",
        markers: "set[str]",
    ) -> Optional[dict]:
        """Reject a phase whose config/input/previous_output rendered <MISSING:> (#535).

        When ``_build_phase_input`` / ``_build_command_extras`` render an
        unresolved ``{config[...]}``, ``{input[...]}`` or
        ``{previous_output[...]}`` reference, ``_SafeDict`` /
        ``_PreviousOutputProxy`` emit a ``<MISSING:...>`` token AND record it
        into a per-render ``missing_sink`` set. Dispatching such a phase sends a
        broken prompt (or command/working_dir) to the executor, which silently
        produces garbage. Given the collected ``markers`` set, this guard
        returns a terminal phase-result dict so the caller can abort the
        pipeline; it returns ``None`` when the set is empty.

        Why the sink (not a substring scan of the rendered prompt): templates
        commonly inline file/skill content via ``{files[...]}`` /
        ``{skill_context[...]}``, and that inlined content can legitimately
        contain literal ``<MISSING:...>`` text or ``{config[...]}`` doc examples
        (e.g. the project's own source/docs inlined by the audit pipelines).
        Those never invoke ``_SafeDict.__missing__`` and so are never recorded
        — only genuinely-missing config/input/previous_output references are.
        ``{context.*}`` markers (runtime-injected, legitimately empty in
        dry-run) are likewise NOT recorded, so dry-runs of git pipelines are
        unaffected.

        Both config-derived (``<MISSING:key>``) and previous-output
        (``<MISSING:previous_output[phase]>``) forms are rejected. The error
        names every offending marker plus the phase id so operators can fix
        their config or template. Wording mirrors
        ``preflight.py:_check_missing_placeholders`` for consistent operator
        messaging.

        Args:
            phase:   The phase definition (used for its id).
            markers: The set of ``<MISSING:...>`` markers recorded by the
                     config/input/previous_output substitutions for this phase.

        Returns:
            A ``permanently_failed`` phase-result dict naming the markers when
            the set is non-empty; otherwise ``None``.
        """
        if not markers:
            return None

        ordered_markers = sorted(markers)
        marker_list = ", ".join(ordered_markers)

        # Dry-run mode is intentionally forgiving of missing input/config:
        # phases run against synthetic output so operators can smoke-test
        # pipeline STRUCTURE without supplying real inputs (issue #659). Aborting
        # here would break that — so in dry-run we log the markers and let the
        # phase dispatch with the placeholder text. The guard only enforces an
        # abort for real executions (standalone / openclaw).
        if self._is_dry_run_mode():
            logger.warning(
                "Pipeline %s: phase '%s' rendered %d unresolved placeholder(s) "
                "%s — allowed in dry-run mode (would abort on a real run).",
                self.template.id,
                phase.id,
                len(markers),
                marker_list,
            )
            return None

        message = (
            f"Phase '{phase.id}': unresolved placeholders: {marker_list}. "
            "These <MISSING:...> markers were rendered because the referenced "
            "config keys or upstream phase outputs do not exist. Add the "
            "missing keys to your config (or fix the template) and re-run. "
            "To emit a literal '<MISSING:...>' string, escape the braces with "
            "{{ }} in the template."
        )
        logger.error(
            "Pipeline %s: phase '%s' rejected before dispatch — %d unresolved "
            "placeholder(s): %s",
            self.template.id,
            phase.id,
            len(markers),
            marker_list,
        )
        return {
            "state": "permanently_failed",
            "error_code": "UNRESOLVED_PLACEHOLDERS",
            "errors": [{"code": "UNRESOLVED_PLACEHOLDERS", "message": message}],
            "result": {"text": ""},
            "confidence": 0.0,
        }

    @staticmethod
    def _load_skill(  # noqa: C901
        skill_ref: str, template_dir: Optional[Path] = None
    ) -> Tuple[str, str]:
        """Load a skill file, stripping YAML frontmatter.

        Resolves ``skill_ref`` in this order:
        1. Absolute path (if given) — must be under ``~/.orch/skills/``
        2. Relative to ``template_dir`` (if provided)
        3. ``~/.orch/skills/``

        Path traversal protection: the resolved path must lie within one of the
        permitted directories.  Absolute paths are restricted to ``~/.orch/skills/``
        only; relative paths may also resolve within ``template_dir``.

        Args:
            skill_ref:    Path string from the ``skill_refs`` list.
            template_dir: Directory of the template file (for relative resolution).

        Returns:
            ``(skill_name, skill_content)`` where ``skill_name`` comes from the
            frontmatter ``name:`` field or the filename stem, and
            ``skill_content`` is the body text with frontmatter stripped.

        Raises:
            FileNotFoundError: If the skill file cannot be located.
            ValueError: If the resolved path escapes the allowed directories
                        (path traversal protection).
        """
        skill_path = Path(skill_ref)
        global_skills_dir = (Path.home() / ".orch" / "skills").resolve()

        # Build the set of allowed root directories.
        # Absolute skill_refs are only permitted under the global skills dir.
        # Relative skill_refs may also resolve within template_dir.
        if skill_path.is_absolute():
            allowed_dirs = [global_skills_dir]
        else:
            allowed_dirs = [global_skills_dir]
            if template_dir is not None:
                allowed_dirs.append(template_dir.resolve())

        # Resolve to an existing file
        resolved: Optional[Path] = None
        if skill_path.is_absolute():
            if skill_path.exists():
                resolved = skill_path.resolve()
        else:
            if template_dir is not None:
                candidate = template_dir / skill_path
                if candidate.exists():
                    resolved = candidate.resolve()
            if resolved is None:
                candidate_global = global_skills_dir / skill_path
                if candidate_global.exists():
                    resolved = candidate_global

        if resolved is None:
            raise FileNotFoundError(
                f"Skill file '{skill_ref}' not found "
                f"(template_dir={template_dir}, ~/.orch/skills/)"
            )

        # --- Path traversal protection -----------------------------------
        resolved_real = resolved.resolve()
        if not any(_is_within_dir(resolved_real, d) for d in allowed_dirs):
            raise ValueError(
                f"Skill path '{skill_ref}' resolves to '{resolved_real}', which is "
                f"outside the allowed directories: "
                f"{[str(d) for d in allowed_dirs]}. "
                f"Relative skill_refs must stay within the template directory or "
                f"~/.orch/skills/; absolute paths must be under ~/.orch/skills/."
            )

        raw = resolved_real.read_text(encoding="utf-8")

        # Strip YAML frontmatter: text between --- delimiters at start of file
        frontmatter_data: Dict[str, Any] = {}
        body = raw
        if raw.startswith("---"):
            # Find closing ---
            end_match = re.search(r"\n---[ \t]*(?:\n|$)", raw[3:])
            if end_match:
                fm_text = raw[3 : 3 + end_match.start()]
                body = raw[3 + end_match.end() :]
                try:
                    frontmatter_data = yaml.safe_load(fm_text) or {}
                except Exception:  # noqa: BLE001
                    frontmatter_data = {}

        # Skill name: prefer frontmatter 'name:', else filename stem
        skill_name: str = str(frontmatter_data.get("name", "")).strip() or resolved_real.stem

        return skill_name, body.strip()

    def _execute_and_wait(  # noqa: C901
        self, task_id: str, phase: PhaseDefinition, initial_input: Optional[dict] = None
    ) -> dict:
        """Execute a queued task synchronously and return its result as a dict.

        Retrieves the TaskSpec from the queue, runs it through the runner's
        first available executor, and implements a retry loop controlled by
        ``phase.retries`` and ``phase.retry_delay_seconds``.

        Retry behaviour
        ---------------
        - The phase is attempted up to ``phase.retries + 1`` times in total.
        - If ``phase.retries == 0`` (the default), the phase is attempted
          exactly once — identical to the original behaviour.
        - On each failed attempt (whether the executor returns
          ``state=FAILED`` **or raises an exception**) a WARNING is logged
          that includes the phase ID, the current attempt number (e.g.
          ``1/4``), and the error message.
        - ``time.sleep(phase.retry_delay_seconds)`` is called between failed
          attempts.  No sleep is performed after the final (exhausted) attempt.
        - When all attempts are exhausted an ERROR log is emitted and the
          final failed :class:`~orchestration_engine.schemas.TaskResult` dict
          is returned — the pipeline abort logic in :meth:`execute` handles it
          as it did before.
        - On success or final failure the returned dict always contains
          ``metadata["attempt_number"]`` (1-indexed, which attempt this was)
          and ``metadata["total_attempts"]`` (how many attempts were made in
          total).

        .. note:: **Dual retry mechanisms**

            :class:`~orchestration_engine.schemas.TaskSpec` also carries a
            ``max_retries`` field (default 3) and a ``retry_count`` counter
            used by :func:`~orchestration_engine.schemas.calculate_retry_delay`
            for exponential back-off and by
            :func:`~orchestration_engine.schemas.select_model_tier` for model
            escalation.  Those fields are managed by executor implementations
            and govern retry behaviour *inside* a single ``executor.execute()``
            call.

            ``PhaseDefinition.retries`` is an independent, **phase-level**
            mechanism that re-invokes ``executor.execute()`` from scratch on
            failure.  It does **not** increment ``TaskSpec.retry_count``, so
            model escalation (``select_model_tier``) is not triggered between
            phase-level retries.  Both mechanisms can coexist; the executor's
            internal retries happen transparently within each attempt that
            ``_execute_and_wait()`` counts.

        Args:
            task_id: ID of the task previously submitted to the runner queue.
            phase:   The PhaseDefinition (used for logging, retry config).

        Returns:
            Dict with at least ``state``, ``result``, ``confidence``, and
            ``metadata`` keys.  ``metadata`` always contains
            ``attempt_number`` and ``total_attempts``.
        """
        # Retrieve the TaskSpec we just submitted
        task_spec = self.runner.queue.get_task(task_id)
        if not task_spec:
            raise RuntimeError(f"Phase '{phase.id}': task {task_id} not found in queue")

        # Select the executor: provider-aware branch first (#969), else the
        # historical first-can_handle loop.
        executor = None
        if getattr(phase, "provider", None):
            executor = self._resolve_executor_by_name(phase.provider)
            if executor is None:
                # Defensive (contract #5): the eager from_providers build makes
                # this unreachable for KNOWN_PROVIDERS (the executor is always
                # present) and unknown providers are rejected at validate/build
                # time. If we still get here, name the provider + the registered
                # executors + the credential hint.
                available = [type(e).__name__ for e in (self.runner.executors or [])]
                raise RuntimeError(
                    f"Phase '{phase.id}': no executor for provider "
                    f"'{phase.provider}'. Registered: {available}. Ensure the "
                    f"provider's credential is set (ANTHROPIC_API_KEY / "
                    f"OPENROUTER_API_KEY) so the run builds it."
                )
        else:
            # Backward-compat: first executor whose can_handle is True. Because
            # every concrete can_handle returns True, this is always executors[0]
            # (INV-1 — list order is the backward-compat contract).
            for ex in self.runner.executors:
                if ex.can_handle(task_spec.type):
                    executor = ex
                    break
            if executor is None:
                raise RuntimeError(
                    f"Phase '{phase.id}': no executor available for task type "
                    f"'{task_spec.type.value}'"
                )

        total_attempts: int = phase.retries + 1
        last_result: Optional[TaskResult] = None
        last_error_msg: str = "Phase execution failed"

        # Retry-feedback tracking (#192)
        attempt_history: List[dict] = []
        failure_context: str = ""  # empty on the first attempt

        for attempt in range(1, total_attempts + 1):
            # On retry (attempt > 1) rebuild the prompt with failure_context so
            # the LLM receives a description of what went wrong last time.
            if attempt > 1 and initial_input is not None:
                with self._phase_outputs_lock:
                    updated_prompt = self._build_phase_input(
                        phase, initial_input, failure_context=failure_context
                    )
                task_spec.payload["prompt"] = updated_prompt

            # Execute synchronously (blocking).
            # Wrap in try/except so that transient exceptions (network timeouts,
            # API rate limits, etc.) are also retried rather than propagating
            # immediately.  If every attempt raises, a synthetic FAILED
            # TaskResult is created after the loop so the pipeline abort path
            # handles the failure consistently.
            try:
                result: TaskResult = executor.execute(
                    task_spec,
                    worker_id="sequencer-worker",
                    model_tier=phase.model_tier,
                    thinking_level=phase.thinking_level,
                )
            except Exception as exc:  # noqa: BLE001
                last_error_msg = str(exc)
                logger.warning(
                    f"Phase '{phase.id}': attempt {attempt}/{total_attempts} "
                    f"raised exception — {exc}"
                )
                sanitized = self._sanitize_error_for_prompt(last_error_msg)
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "error": sanitized,
                        "partial_output": "",
                        "tokens_used": 0,
                    }
                )
                if attempt < total_attempts:
                    failure_context = self._format_failure_context(attempt, sanitized, "")
                    logger.debug(
                        f"Phase '{phase.id}': waiting {phase.retry_delay_seconds}s "
                        f"before retry attempt {attempt + 1}/{total_attempts}"
                    )
                    time.sleep(phase.retry_delay_seconds)
                continue

            last_result = result

            if result.state == TaskState.SUCCESS:
                # Persist success in queue
                self.runner.queue.complete_task(task_id, result)
                # Annotate metadata with retry telemetry
                result.metadata["attempt_number"] = attempt
                result.metadata["total_attempts"] = attempt
                result.metadata["retry_history"] = attempt_history
                try:
                    return result.model_dump()
                except AttributeError:
                    return result.dict()  # Pydantic v1 fallback

            # ---- Failure path ----------------------------------------
            last_error_msg = "Phase execution failed"
            if result.errors:
                first = result.errors[0]
                last_error_msg = (
                    first.get("message", last_error_msg)
                    if isinstance(first, dict)
                    else getattr(first, "message", last_error_msg)
                )

            logger.warning(
                f"Phase '{phase.id}': attempt {attempt}/{total_attempts} failed — "
                f"{last_error_msg}"
            )

            # Capture partial output and record attempt history (#192)
            try:
                partial_output = _extract_phase_text(result.model_dump())
            except AttributeError:
                partial_output = _extract_phase_text(result.dict())  # Pydantic v1
            tokens_used = result.metadata.get("tokens_used", 0) if result.metadata else 0
            sanitized_err = self._sanitize_error_for_prompt(last_error_msg)
            attempt_history.append(
                {
                    "attempt": attempt,
                    "error": sanitized_err,
                    "partial_output": partial_output,  # full output in history
                    "tokens_used": tokens_used,
                }
            )

            # Sleep between attempts, but NOT after the final one
            if attempt < total_attempts:
                failure_context = self._format_failure_context(
                    attempt, sanitized_err, partial_output
                )
                logger.debug(
                    f"Phase '{phase.id}': waiting {phase.retry_delay_seconds}s "
                    f"before retry attempt {attempt + 1}/{total_attempts}"
                )
                time.sleep(phase.retry_delay_seconds)

        # ---- All attempts exhausted -----------------------------------
        logger.error(
            f"Phase '{phase.id}': permanently failed after {total_attempts} "
            f"attempt(s). Last error: {last_error_msg}"
        )

        # Persist failure in queue
        self.runner.queue.fail_task(task_id, last_error_msg)

        # Annotate final failed result with retry telemetry.
        # last_result may be None when every attempt raised an exception (as
        # opposed to returning a graceful FAILED TaskResult).  Rather than
        # propagating a RuntimeError — which would bypass the pipeline's abort
        # logic — we synthesise a FAILED TaskResult so the caller sees a
        # consistent failure and triggers the standard abort path.
        # Explicit guard (not assert) survives Python -O.
        if last_result is None:
            last_result = TaskResult(
                task_id=task_spec.id,
                task_type=task_spec.type,
                state=TaskState.FAILED,
                confidence=0.0,
                result={"text": ""},
                errors=[
                    TaskError(
                        code="EXEC_EXCEPTION",
                        message=last_error_msg,
                        severity="error",
                    )
                ],
            )
        last_result.metadata["attempt_number"] = total_attempts
        last_result.metadata["total_attempts"] = total_attempts
        last_result.metadata["retry_history"] = attempt_history

        try:
            return last_result.model_dump()
        except AttributeError:
            return last_result.dict()  # Pydantic v1 fallback

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_error_for_prompt(error: str) -> str:
        """Strip Python tracebacks and ANSI codes from an error string, then truncate.

        Intended to produce a clean, concise error message suitable for
        inclusion in an LLM retry prompt.

        Processing steps:
        1. Strip ANSI escape codes (colour codes, cursor movement, etc.)
        2. Remove Python traceback blocks — keeps only the final exception line
           (e.g. ``ValueError: something``).
        3. Truncate the result to 500 characters, appending ``"..."`` when
           truncation occurs.

        Args:
            error: Raw error string (may contain tracebacks and/or ANSI codes).

        Returns:
            Sanitized string of at most 500 characters.
        """
        # 1. Remove ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[mGKHFJsr]")
        error = ansi_escape.sub("", error)

        # 2. Strip Python traceback blocks.
        #    A traceback starts with "Traceback (most recent call last):" and
        #    the body consists of indented lines ("  File ...", "    code...").
        #    The block ends at the first non-indented line which is the actual
        #    exception type/message (e.g. "ValueError: foo").  We skip the
        #    traceback body and keep only that final exception line.
        lines = error.splitlines()
        in_traceback = False
        filtered: List[str] = []
        for line in lines:
            if line.strip().startswith("Traceback (most recent call last):"):
                in_traceback = True
                continue
            if in_traceback:
                # Traceback body: lines indented with spaces or tabs
                if line.startswith("  ") or line.startswith("\t"):
                    continue
                else:
                    # Non-indented line → the exception class/message line
                    in_traceback = False
                    filtered.append(line)
            else:
                filtered.append(line)

        error = "\n".join(filtered).strip()

        # 3. Truncate to 500 chars
        if len(error) > 500:
            error = error[:497] + "..."

        return error

    @staticmethod
    def _format_failure_context(attempt: int, error: str, partial_output: str) -> str:
        """Return a markdown-formatted failure context block for LLM retry prompts.

        The returned string is injected into the phase prompt on the *next*
        attempt (via the ``{failure_context}`` placeholder) so that the LLM
        can see what went wrong and try a different approach.

        Partial output is truncated to 1 000 characters inside this method;
        pass the full partial output here and store it separately in
        ``retry_history`` if you need the untruncated version.

        Args:
            attempt:        The 1-based attempt number that failed.
            error:          Sanitized error message (output of
                            :meth:`_sanitize_error_for_prompt`).
            partial_output: Any text produced by the failed attempt before it
                            errored.  May be an empty string.

        Returns:
            Markdown string ready for injection into a prompt template.
        """
        display_partial = partial_output[:1000] if partial_output else "(none)"

        return (
            f"## Previous Attempt Failed\n\n"
            f"**Attempt:** {attempt}\n"
            f"**Error:** {error}\n\n"
            f"**Partial Output:**\n"
            f"{display_partial}\n\n"
            f"Please review the above failure and try a different approach."
        )

    def _build_command_extras(
        self,
        phase: "PhaseDefinition",
        initial_input: dict,
        missing_sink: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Build extra payload fields for command task_type phases.

        Interpolates ``phase.command`` with ``{config}`` and ``{input}``
        context (same variables available in prompt templates), so pipeline
        YAML can use ``{config[repo_path]}``, etc. in the command string.

        Returns an empty dict when ``phase.task_type != "command"``, so
        callers may unconditionally merge the result into the payload dict.

        Args:
            phase:         The phase definition.
            initial_input: Pipeline input dict.

        Returns:
            Dict with keys ``command``, ``allowed_commands``, ``working_dir``,
            and ``output_dir`` when task_type is "command"; otherwise ``{}``.
        """
        if phase.task_type not in ("command", "acceptance_run"):
            return {}

        raw_command: str = phase.command or ""

        # Interpolate {config[key]} and {input[key]} placeholders.
        # Use _SafeDict so unknown placeholders are left intact rather than
        # raising KeyError — matches the behaviour of _build_phase_input.
        safe_input = _SafeDict(initial_input, missing_sink=missing_sink)
        safe_config = _SafeDict(self.config, missing_sink=missing_sink)
        output_dir_str = str(self.output_dir) if self.output_dir else ""

        try:
            interpolated_command = raw_command.format(
                config=safe_config,
                input=safe_input,
                output_dir=output_dir_str,
            )
        except (KeyError, IndexError, AttributeError, ValueError) as exc:
            logger.warning(
                "Phase '%s': command interpolation failed (%s); using raw command.",
                phase.id,
                exc,
            )
            interpolated_command = raw_command

        # Interpolate working_dir similarly (may contain {config[repo_path]})
        working_dir_raw: str = getattr(phase, "working_dir", None) or ""
        # "." is the dataclass default — treat it as "not set" (use cwd)
        if working_dir_raw == ".":
            working_dir_raw = ""
        try:
            working_dir = (
                working_dir_raw.format(
                    config=safe_config,
                    input=safe_input,
                    output_dir=output_dir_str,
                )
                if working_dir_raw
                else ""
            )
        except (KeyError, IndexError, AttributeError, ValueError):
            working_dir = working_dir_raw

        # Opt-in acceptance matrix (#985): interpolate each entry's command with
        # the SAME {config}/{input}/{output_dir} context (and the same per-entry
        # try/except fallback) as phase.command, then pass through raw so it
        # lands in task.payload["acceptance_matrix"]. Empty list → [] → the
        # executor takes the byte-identical legacy single-pytest path.
        matrix_out: List[Dict[str, str]] = []
        for entry in getattr(phase, "acceptance_matrix", None) or []:
            entry_command_raw: str = entry.get("command", "") or ""
            try:
                entry_command = entry_command_raw.format(
                    config=safe_config,
                    input=safe_input,
                    output_dir=output_dir_str,
                )
            except (KeyError, IndexError, AttributeError, ValueError) as exc:
                logger.warning(
                    "Phase '%s': acceptance_matrix command interpolation failed "
                    "(%s); using raw command.",
                    phase.id,
                    exc,
                )
                entry_command = entry_command_raw
            matrix_out.append({"name": entry.get("name", ""), "command": entry_command})

        logger.debug(
            "Phase '%s': command_extras resolved — command=%r, working_dir=%r, "
            "acceptance_matrix=%d entries",
            phase.id,
            interpolated_command,
            working_dir or "<inherit>",
            len(matrix_out),
        )

        return {
            "command": interpolated_command,
            "allowed_commands": list(phase.allowed_commands),
            "working_dir": working_dir or None,
            "output_dir": output_dir_str,
            "acceptance_matrix": matrix_out,
        }

    def _sandbox_roots(self) -> Dict[str, Optional[str]]:
        """Build the ``sandbox_roots`` payload dict consumed by tool-calling executors (#794).

        Values are absolute path strings for roots that are set, or Python ``None``
        (not the literal string ``"None"``) for roots that are unset. ``tmp_dir``
        is always populated so executors always have at least one usable root.
        """
        repo_path = self.config.get("repo_path") if isinstance(self.config, dict) else None
        output_dir = str(self.output_dir) if self.output_dir else None
        return {
            "repo_path": str(repo_path) if repo_path else None,
            "output_dir": output_dir,
            "tmp_dir": tempfile.gettempdir(),
        }

    # ------------------------------------------------------------------
    # Dialogue phase dispatch (Track B / Issue #677)
    # ------------------------------------------------------------------

    def _execute_dialogue_phase(
        self,
        phase: "PhaseDefinition",
        phase_input: str,
    ) -> Dict[str, Any]:
        """Run a ``type: dialogue`` phase via :mod:`dialogue_phase`.

        Resolves the drafter / reviewer executors from the runner's executor
        registry (matching by class-name substring — see
        :meth:`_resolve_dialogue_executor`), runs the dialogue loop, and
        returns a sequencer-compatible result dict that downstream phase
        handlers can store in ``phase_outputs``.

        The returned dict mirrors the shape of :class:`TaskResult.model_dump`
        so existing consumers (UI, ``_extract_phase_text``) keep working.

        Args:
            phase: The dialogue phase definition (``phase.dialogue_config`` non-None).
            phase_input: The prompt-templated input string for round 1.

        Returns:
            Dict with ``state``, ``result``, ``rounds``, ``converged``,
            ``cost_usd``, ``tokens_consumed``, ``execution_time_seconds``,
            and metadata.
        """
        from .dialogue_phase import DialogueRunner  # noqa: PLC0415

        start_time = time.time()
        config = phase.dialogue_config

        drafter_exec = self._resolve_dialogue_executor(config.drafter.executor)
        reviewer_exec = self._resolve_dialogue_executor(config.reviewer.executor)

        if drafter_exec is None or reviewer_exec is None:
            missing = (
                f"drafter='{config.drafter.executor}'"
                if drafter_exec is None
                else f"reviewer='{config.reviewer.executor}'"
            )
            err = (
                f"dialogue phase '{phase.id}': could not resolve executor ({missing}). "
                f"Available: {[type(e).__name__ for e in (self.runner.executors or [])]}"
            )
            logger.error(err)
            return {
                "state": "failed",
                "task_id": f"dialogue-{phase.id}",
                "task_type": "review",
                "confidence": 0.0,
                "result": {"output": "", "text": ""},
                "errors": [
                    {"code": "dialogue_executor_unresolved", "message": err, "severity": "error"}
                ],
                "execution_time_seconds": time.time() - start_time,
                "metadata": {"phase_id": phase.id},
            }

        runner = DialogueRunner(
            config=config,
            drafter_executor=drafter_exec,
            reviewer_executor=reviewer_exec,
            output_dir=self.output_dir,
            run_id=self.run_id,
            phase_id=phase.id,
        )
        dialogue_result = runner.run(phase_input)

        duration = time.time() - start_time
        rounds_dump = [
            {
                "round_number": r.round_number,
                "draft_text": r.draft_text,
                "review_text": r.review_text,
                "approved": r.approved,
                "drafter_cost": str(r.drafter_cost),
                "reviewer_cost": str(r.reviewer_cost),
                "cost": str(r.cost),
                "drafter_tokens": r.drafter_tokens,
                "reviewer_tokens": r.reviewer_tokens,
                "drafter_model": r.drafter_model,
                "reviewer_model": r.reviewer_model,
                "drift_similarity": r.drift_similarity,
            }
            for r in dialogue_result.rounds
        ]

        # The sequencer maps a phase's success on "state == 'success'".
        # A dialogue is a success if at least one round completed without a
        # fatal executor error.  An un-converged dialogue (max_rounds hit) is
        # NOT a failure per #677 — phase still produces a final draft.
        is_success = dialogue_result.succeeded and len(dialogue_result.rounds) > 0

        errors: List[Dict[str, Any]] = []
        if dialogue_result.error:
            errors.append(
                {
                    "code": "dialogue_executor_error",
                    "message": dialogue_result.error,
                    "severity": "error",
                }
            )

        return {
            "state": "success" if is_success else "failed",
            "task_id": f"dialogue-{phase.id}",
            "task_type": "review",
            "confidence": 0.85 if dialogue_result.converged else (0.6 if is_success else 0.0),
            "result": {
                "output": dialogue_result.final_draft,
                "text": dialogue_result.final_draft,
                "converged": dialogue_result.converged,
                "rounds_completed": len(dialogue_result.rounds),
                "max_rounds": config.max_rounds,
                "convergence_stall": dialogue_result.convergence_stall,
            },
            "errors": errors,
            "execution_time_seconds": duration,
            "tokens_consumed": dialogue_result.total_tokens,
            "cost_usd": str(dialogue_result.total_cost),
            "metadata": {
                "phase_id": phase.id,
                "dialogue_rounds": rounds_dump,
                "dialogue_history": dialogue_result.history,
                "converged": dialogue_result.converged,
                "convergence_stall": dialogue_result.convergence_stall,
                "drafter_executor": config.drafter.executor,
                "reviewer_executor": config.reviewer.executor,
            },
        }

    def _resolve_executor_by_name(self, name: str) -> Optional[Any]:
        """Resolve a registered executor by provider name (#969).

        Match order (case-insensitive):
        1. exact ``provider_name`` class-attr match (the explicit #969 mechanism);
        2. legacy substring of ``type(ex).__name__`` (the historical dialogue
           behaviour, kept so executors without a ``provider_name`` still route);
        3. all-dry-run fallback — when EVERY registered executor is a
           :class:`~.runner.DryRunExecutor`, return it so dry-run validation /
           mixed-provider dry-run needs no real provider credentials.

        Aliases (``gemini_cli`` → ``gemini``) are normalised before matching.
        Returns ``None`` when nothing matches and the fallback does not apply.
        """
        executors = getattr(self.runner, "executors", None) or []
        if not executors:
            return None

        needle = (name or "").lower().replace("-", "_").replace(" ", "")
        if not needle:
            return None
        # Normalise common aliases (kept identical to the historical table)
        aliases = {
            "gemini_cli": "gemini",
        }
        needle = aliases.get(needle, needle)

        # (1) explicit provider_name class attr.
        for ex in executors:
            prov = getattr(ex, "provider_name", "")
            if prov and prov == needle:
                return ex

        # (2) legacy substring fallback over the class name.
        for ex in executors:
            if needle in type(ex).__name__.lower():
                return ex

        # (3) all-dry-run fallback — when the only available executors are
        # dry-run mocks, return one so dialogue / mixed-provider phases can
        # still execute without real provider credentials.
        dry_runners = [ex for ex in executors if "dryrun" in type(ex).__name__.lower()]
        if len(dry_runners) == len(executors) and dry_runners:
            return dry_runners[0]
        return None

    def _resolve_dialogue_executor(self, name: str) -> Optional[Any]:
        """Find an executor in ``self.runner.executors`` whose name matches ``name``.

        Back-compat shim for dialogue phases (#677) — delegates to the shared
        :meth:`_resolve_executor_by_name` (#969), preserving the substring +
        alias + all-dry-run-fallback behaviour exactly:

        * ``openrouter`` → the OpenRouter executor
        * ``anthropic`` → the Anthropic executor
        * ``gemini_cli`` / ``gemini`` → the Gemini executor
        * ``openclaw`` → the OpenClaw executor
        * ``claudecode`` → the ClaudeCode executor

        Returns ``None`` when no matching executor is registered, EXCEPT in
        dry-run mode (only a :class:`~.runner.DryRunExecutor` is registered),
        where the dry-run executor is returned as a fallback so dialogue
        phases can be validated by the template-validation suite without
        real provider credentials.
        """
        return self._resolve_executor_by_name(name)

    @staticmethod
    def _resolve_task_type(task_type_str: str) -> TaskType:
        """Map a string task type to a TaskType enum, defaulting to CONTENT."""
        try:
            return TaskType(task_type_str.lower())
        except ValueError:
            logger.warning(f"Unknown task_type '{task_type_str}'; defaulting to 'content'")
            return TaskType.CONTENT

    @staticmethod
    def _resolve_model_tier(model_tier_str: str):
        """Map a friendly model tier name to a ModelTier enum value.

        The PhaseDefinition uses short names (haiku, sonnet, opus) while
        the schema uses versioned names (haiku-4-5, sonnet-4, opus-4-6).
        Delegates to the canonical model_registry (#916) — the single
        short↔versioned bridge. Returns None if the tier is not recognised
        (runner will use its default).
        """
        from .model_registry import resolve_tier  # noqa: PLC0415

        resolved = resolve_tier(model_tier_str)
        if resolved is None and model_tier_str:
            logger.debug(f"Unrecognised model_tier '{model_tier_str}'; using runner default")
        return resolved


# Module-level alias for the supervisor response parser (convenience for tests)
_parse_supervisor_response = PhaseSequencer._parse_supervisor_response


def _is_within_dir(path: Path, directory: Path) -> bool:
    """Return True if *path* is the same as, or a descendant of, *directory*.

    Both arguments should already be resolved (absolute, symlink-free) paths.
    """
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _extract_phase_text(phase_output: Any) -> str:
    """Extract clean text from a phase output dict.

    Phase outputs are stored as ``TaskResult.model_dump()`` dicts.  The actual
    text lives at ``result.text`` (or ``result["text"]``).  If the output is
    already a string, return it as-is.
    """
    if isinstance(phase_output, str):
        return phase_output
    if isinstance(phase_output, dict):
        # Primary path: result dict from TaskResult.model_dump()
        result = phase_output.get("result", {})
        if isinstance(result, dict):
            text = result.get("text", "")
            if text:
                return str(text)
        # Fallback: maybe it's a flat dict with 'text' at top level
        text = phase_output.get("text", "")
        if text:
            return str(text)
        # Last resort: stringify but warn
        logger.warning(
            f"Phase output dict has no 'result.text' key; falling back to str(). "
            f"Keys: {list(phase_output.keys())}"
        )
        return str(phase_output)
    return str(phase_output)


# ---------------------------------------------------------------------------
# Issue #651 — Finding analysis helpers for MAX_ITERATIONS_EXCEEDED
# ---------------------------------------------------------------------------

#: Regex matching a tagged finding line: [SEVERITY][category] description
_FINDING_TAG_RE = re.compile(r"^\s*\[([A-Za-z]+)\]\[([^\]]+)\]\s+(.+)$")


def _extract_findings_from_text(text: str) -> list:
    """Extract findings from a round file's text content.

    Returns a list of finding strings.  Tagged lines of the form
    ``[SEVERITY][category] description`` are returned as individual findings.
    If no tagged lines exist, the entire file content (up to 2 000 chars) is
    returned as a single finding — unless the file contains only markdown
    headers or empty lines, in which case an empty list is returned.
    """
    findings = []
    for line in text.splitlines():
        if _FINDING_TAG_RE.match(line):
            findings.append(line.strip())  # noqa: PERF401
    if not findings:
        # No tagged lines — check for substantive untagged content
        content_lines = [
            line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        ]
        if content_lines:
            findings = [text.strip()[:2000]]
    return findings


def _are_findings_similar(a: str, b: str) -> bool:
    """Return *True* if two finding strings are substantially similar.

    Uses word-set Jaccard similarity (intersection / union ≥ 0.50) after
    lowercasing and stripping ``[TAG][TAG]`` prefixes.  Empty word-sets
    are never considered similar.
    """

    def _words(s: str) -> set:
        # Strip [SEVERITY][category] tag prefix before comparison
        s = re.sub(r"^\s*\[[^\]]+\]\[[^\]]+\]\s*", "", s.lower())
        return set(re.findall(r"\w+", s))

    words_a = _words(a)
    words_b = _words(b)
    if not words_a or not words_b:
        return False
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return (intersection / union) >= 0.50


def _analyze_round_findings(output_dir, phase_id: str, max_iterations: int) -> str:  # noqa: C901
    """Analyse iteration-indexed round files for repeated vs new findings.

    Returns an analysis string to append to the error message, or ``""`` when
    analysis is not applicable (non-iterative phase, fewer than 2 files,
    fewer than 2 readable files).

    Side-effects when analysis is produced:
    * Logs the result at ``ERROR`` level.
    * Writes ``finding_analysis.md`` to *output_dir*.
    """
    if max_iterations <= 1:
        return ""  # Non-iterative phase — skip

    if output_dir is None:
        return ""

    output_path = Path(output_dir)
    safe_pid = re.sub(r"[^\w\-]", "_", phase_id)

    # Discover all round files: {safe_pid}_round{N}.md
    round_files = []
    for n in range(1, max_iterations + 2):  # scan up to max+1 to catch edge cases
        rp = output_path / f"{safe_pid}_round{n}.md"
        if rp.exists():
            round_files.append((n, rp))

    if len(round_files) < 2:
        return ""  # Nothing to compare

    # Read each file; skip unreadable files with a warning
    per_round_findings: list = []
    for n, rp in round_files:
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
            findings = _extract_findings_from_text(text)
            per_round_findings.append(findings)
        except Exception as exc:  # noqa: BLE001, PERF203
            logger.warning(
                "Finding analysis: could not read round file '%s': %s — skipping.", rp, exc
            )

    if len(per_round_findings) < 2:
        # After skipping unreadable files, fewer than 2 readable files remain
        return ""

    n_rounds = len(per_round_findings)

    # Check how many rounds contributed at least one finding
    rounds_with_findings = [f for f in per_round_findings if f]
    if len(rounds_with_findings) < 2:
        analysis = (
            f"{n_rounds} round files found but no structured findings could be "
            f"extracted for comparison."
        )
        logger.error("Finding analysis for phase '%s': %s", phase_id, analysis)
        if output_dir:
            try:
                analysis_path = output_path / "finding_analysis.md"
                analysis_path.write_text(
                    f"# Finding Analysis\n\nPhase: `{phase_id}`\n\n{analysis}\n",
                    encoding="utf-8",
                )
            except Exception as _exc:  # noqa: BLE001
                logger.warning("Failed to write finding_analysis.md: %s", _exc)
        return analysis

    # Compare findings across all rounds: find any pair from different rounds
    # that is similar.  Track the finding that matches the most rounds.
    all_findings_flat = [
        (r_idx, finding)
        for r_idx, findings in enumerate(per_round_findings)
        for finding in findings
    ]

    repeated_finding = None
    best_match_count = 0

    for i, (round_i, finding_i) in enumerate(all_findings_flat):
        match_rounds = {round_i}
        for j, (round_j, finding_j) in enumerate(all_findings_flat):
            if i == j or round_j == round_i:
                continue
            if _are_findings_similar(finding_i, finding_j):
                match_rounds.add(round_j)
        if len(match_rounds) >= 2 and len(match_rounds) > best_match_count:
            best_match_count = len(match_rounds)
            repeated_finding = finding_i

    if repeated_finding is not None:
        summary = repeated_finding[:200]
        analysis = (
            f"Repeated finding detected across {best_match_count} rounds: {summary}. "
            f"The loop may be stuck on a hallucinated or unfixable issue."
        )
    else:
        analysis = (
            f"All {n_rounds} rounds raised different issues. The code may need "
            f"manual intervention or the issue should be split."
        )

    logger.error("Finding analysis for phase '%s': %s", phase_id, analysis)
    if output_dir:
        try:
            analysis_path = output_path / "finding_analysis.md"
            analysis_path.write_text(
                f"# Finding Analysis\n\nPhase: `{phase_id}`\n\n{analysis}\n",
                encoding="utf-8",
            )
        except Exception as _exc:  # noqa: BLE001
            logger.warning("Failed to write finding_analysis.md: %s", _exc)

    return analysis


def _wrap_callable_runner(fn):  # noqa: C901
    """Wrap a plain callable as a minimal runner object for testing.

    When *fn* is a callable (not already a runner with `.queue`/`.executors`),
    this creates a lightweight shim so that ``StateMachineSequencer`` can
    accept a bare function as its ``runner`` argument in unit tests.

    The callable signature expected by the shim::

        fn(phase_def, context, **kwargs) -> dict

    The returned dict is used as the phase result directly.
    """

    class _FakeQueue:
        def __init__(self):
            self._store = {}

        def submit_task(self, spec):
            self._store[spec.id] = spec
            return spec.id

        def get_task(self, task_id):
            return self._store.get(task_id)

        def complete_task(self, task_id, result):
            pass

        def fail_task(self, task_id, error):
            pass

    class _FakeExecutor:
        def __init__(self, callable_fn):
            self._fn = callable_fn

        def can_handle(self, task_type):  # noqa: ARG002
            return True

        def execute(self, task_spec, **kwargs):
            # Call the wrapped function and convert the result
            result_dict = self._fn(
                None,  # phase_def (not available here)
                task_spec.payload.get("prompt", ""),
                **kwargs,
            )
            # Wrap the dict in a minimal object that satisfies _execute_and_wait
            return _FakeTaskResult(result_dict)

    class _FakeTaskResult:
        """Minimal TaskResult shim for use in tests."""

        def __init__(self, data: dict):
            self._data = data
            # Map common dict keys to TaskResult attributes
            self.state = type("S", (), {"value": "success"})()
            self.state = _FakeState(data)
            self.confidence = data.get("confidence", 0.8)
            self.metadata = data.get("metadata", {})
            self.errors = data.get("errors", [])
            self.model_used = data.get("model_used")

        def model_dump(self):
            content = self._data.get("content", self._data.get("result", ""))
            verdict = self._data.get("verdict", "")
            # Build text for content-based routing (extract_verdict).
            # If a verdict key is present, prepend it so extract_verdict finds it.
            text_parts = []
            if verdict:
                text_parts.append(verdict)
            if content:
                text_parts.append(content)
            text = "\n".join(text_parts) if text_parts else ""
            return {
                "content": content,
                "verdict": verdict,
                "status": self._data.get("status", "completed"),
                "text": text,
                "result": {"text": text},
                "state": "success",
                **self._data,
            }

        # Pydantic v1 compat
        def dict(self):
            return self.model_dump()

    class _FakeState:
        def __init__(self, data: dict):
            status = data.get("status", "completed")
            # Map status strings to TaskState
            if status in ("completed", "success"):
                self.value = "success"
            else:
                self.value = "failed"

        def __eq__(self, other):
            # Handle TaskState (str enum): its value attr holds the string
            if hasattr(other, "value"):
                return self.value == other.value
            # Handle plain strings
            return self.value == other

        def __hash__(self):
            return hash(self.value)

        def __str__(self):
            return self.value

        def __repr__(self):
            return f"_FakeState({self.value!r})"

    class _FakeRunner:
        def __init__(self, callable_fn):
            self.queue = _FakeQueue()
            self.executors = [_FakeExecutor(callable_fn)]

    return _FakeRunner(fn)


class _SafeDict(dict):
    """A dict subclass that returns a placeholder string for missing keys.

    This prevents ``str.format()`` calls from raising ``KeyError`` when the
    template references a phase output that has not yet been produced (e.g.
    due to template authoring errors).

    When a ``missing_sink`` set is supplied, every emitted ``<MISSING:key>``
    marker is also recorded into it. This lets callers distinguish markers that
    THIS mapping actually generated (a genuine missing config/input reference)
    from ``<MISSING:...>`` substrings that merely appear in inlined content
    (e.g. ``context_files`` / ``skill_context`` text) — the latter never
    invoke ``__missing__`` and so are never recorded (#535).
    """

    def __init__(self, *args, missing_sink: Optional[set] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Stored via object.__setattr__ so __getattr__ below never intercepts it.
        object.__setattr__(self, "_missing_sink", missing_sink)

    def __missing__(self, key: str) -> str:
        logger.warning(f"Template referenced missing key: '{key}' — substituting <MISSING:{key}>")
        sink = object.__getattribute__(self, "_missing_sink")
        if sink is not None:
            sink.add(f"<MISSING:{key}>")
        return f"<MISSING:{key}>"

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            logger.warning(
                f"Template referenced missing attribute: '{key}' — substituting <MISSING:{key}>"
            )
            try:
                sink = object.__getattribute__(self, "_missing_sink")
            except AttributeError:
                sink = None
            if sink is not None:
                sink.add(f"<MISSING:{key}>")
            return f"<MISSING:{key}>"


class _PhaseOutput:
    """Wrapper that allows ``{phase_id.output}`` syntax in prompt templates.

    When a template references ``{requirements.output}``, Python's ``str.format()``
    calls ``getattr(phase_obj, 'output')``.  This class provides that attribute.
    It also has a ``__format__`` method so ``{requirements}`` (without ``.output``)
    returns the output text directly.
    """

    def __init__(self, text: str) -> None:
        self.output = text
        self._text = text

    def __format__(self, format_spec: str) -> str:
        return format(self._text, format_spec)

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return f"_PhaseOutput({self._text[:80]!r}...)"


class _PreviousOutputProxy:
    """Smart proxy for the ``{previous_output}`` template variable (fix/243).

    Behaviour depends on whether *output_dir* is set:

    * **output_dir set** — ``{previous_output}`` expands to a compact,
      file-path-based summary (phase name + word count + ``→ path/phase.md``).
      Full phase content is NOT inlined, saving 30 K+ tokens per run.
      ``{previous_output[phase_id]}`` prepends a ``"Full output at: …"`` note
      before the inline text so models know where to find the full version.

    * **output_dir not set** — ``{previous_output}`` expands to
      ``str(phase_outputs)`` (the old behaviour, full backward compatibility).
      ``{previous_output[phase_id]}`` returns the extracted text with no note.

    Missing phase IDs return ``<MISSING:previous_output[phase_id]>`` so
    template authoring errors produce visible, non-crashing placeholders.
    """

    def __init__(
        self,
        phase_outputs: dict,
        output_dir: Optional[str],
        phase_map: dict,
        missing_sink: Optional[set] = None,
    ) -> None:
        self._phase_outputs = phase_outputs
        self._output_dir = output_dir
        self._phase_map = phase_map
        # Optional set that collects emitted <MISSING:previous_output[...]>
        # markers so the dispatch-level guard can reject genuinely-missing
        # upstream references (#535).
        self._missing_sink = missing_sink

    # ── str.format() calls __format__ for {previous_output} ──────────────────

    def __format__(self, format_spec: str) -> str:
        if self._output_dir:
            return format(self._build_summary(), format_spec)
        return format(str(self._phase_outputs), format_spec)

    def __str__(self) -> str:
        if self._output_dir:
            return self._build_summary()
        return str(self._phase_outputs)

    # ── str.format() calls __getitem__ for {previous_output[phase_id]} ───────

    def __getitem__(self, key: str) -> str:
        if key not in self._phase_outputs:
            if self._missing_sink is not None:
                self._missing_sink.add(f"<MISSING:previous_output[{key}]>")
            return f"<MISSING:previous_output[{key}]>"
        inline = _extract_phase_text(self._phase_outputs[key])
        if self._output_dir:
            safe_pid = key.replace("-", "_")
            return f"Full output at: {self._output_dir}/{safe_pid}.md\n\n{inline}"
        return inline

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_summary(self) -> str:
        """Return compact summary lines, one per prior phase."""
        if not self._phase_outputs:
            return "No prior phases."
        lines: List[str] = []
        for pid, pout in self._phase_outputs.items():
            text = _extract_phase_text(pout)
            word_count = len(text.split()) if text else 0
            phase_def = self._phase_map.get(pid)
            phase_name = phase_def.name if phase_def else pid
            safe_pid = pid.replace("-", "_")
            lines.append(
                f"- {phase_name} ({pid}): completed, ~{word_count} words"
                f" → {self._output_dir}/{safe_pid}.md"
            )
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"_PreviousOutputProxy(output_dir={self._output_dir!r}, phases={list(self._phase_outputs.keys())})"  # noqa: E501


class _PreviousOutputInlineProxy:
    """Proxy for ``{previous_output_inline}`` — always returns full inline content.

    Preserves the pre-fix/243 ``{previous_output}`` behaviour for templates
    that explicitly require every prior phase output dumped inline.  The full
    raw ``phase_outputs`` dict repr is returned as a string, regardless of
    whether *output_dir* is configured.
    """

    def __init__(self, phase_outputs: dict, missing_sink: Optional[set] = None) -> None:
        self._phase_outputs = phase_outputs
        self._missing_sink = missing_sink

    def __format__(self, format_spec: str) -> str:
        return format(str(self._phase_outputs), format_spec)

    def __str__(self) -> str:
        return str(self._phase_outputs)

    def __getitem__(self, key: str) -> str:
        if key not in self._phase_outputs:
            if self._missing_sink is not None:
                self._missing_sink.add(f"<MISSING:previous_output_inline[{key}]>")
            return f"<MISSING:previous_output_inline[{key}]>"
        return _extract_phase_text(self._phase_outputs[key])

    def __repr__(self) -> str:
        return f"_PreviousOutputInlineProxy(phases={list(self._phase_outputs.keys())})"


class StateMachineSequencer(PhaseSequencer):
    """Executes a pipeline using state-machine transitions with loop support.

    Unlike :class:`PhaseSequencer` (which follows a static topological order),
    ``StateMachineSequencer`` routes execution dynamically: after each phase
    completes its outcome is mapped to the next phase via the phase's
    ``transitions`` dict (merged with the template's ``default_transitions``).
    Execution terminates when a phase has no matching transition entry
    (terminal state), when a phase exceeds its iteration limit, or when an
    accidental cycle is detected.

    Loop / iteration support (Issue #235)
    --------------------------------------
    Phases may be revisited up to ``phase.max_iterations`` times (set
    ``max_iterations > 0`` on the phase definition to opt into loop
    behaviour).  This enables patterns like:

    * **Review loops**: ``write_draft → review → revise → review``
    * **Retry chains**: ``run_tests → fix_code → run_tests``
    * **Quality gates**: any phase that repeats until an outcome changes

    When a phase has ``max_iterations > 0``, the execution count is tracked
    and compared against ``effective_max = phase.max_iterations``.  If the
    phase would exceed its limit, execution is aborted with
    ``abort_reason = "MAX_ITERATIONS_EXCEEDED"``.

    When a phase has ``max_iterations == 0`` (the default — "not a loop
    phase"), the legacy one-visit cycle guard applies: revisiting such a
    phase logs a WARNING and stops execution cleanly.

    Iteration history
    -----------------
    Each time a phase is re-executed, its **previous** result is appended to
    ``self.iteration_history[phase_id]`` before the new result overwrites
    ``phase_outputs[phase_id]``.  This provides a full per-phase execution
    history for observability and debugging.  The final ``execute()`` result
    dict exposes both ``iteration_history`` and ``iteration_counts``.

    Entry point
    -----------
    Execution begins with the **first phase** listed in ``template.phases``
    (index 0).  This is the conventional entry point for a transition chain.
    Transitions are followed until the chain terminates.

    Transition resolution
    ---------------------
    For each completed phase the *effective* transitions are computed as::

        effective = {**template.default_transitions, **phase.transitions}

    The outcome value (``"success"``, ``"failed"``, ``"timeout"``,
    ``"skipped"``) is looked up in *effective*.  If a matching key is found
    the value is the ID of the next phase.  If no key matches the phase is
    considered terminal and execution stops.

    All parent hooks (``on_phase_start``, ``on_phase_complete``,
    ``on_pipeline_start``, ``on_pipeline_complete``) behave identically to
    :class:`PhaseSequencer`.  Callbacks fire **once per execution** of a
    phase (not once per unique phase), so a phase that loops three times will
    trigger ``on_phase_start`` and ``on_phase_complete`` three times.

    Observability
    -------------
    The result dict returned by :meth:`execute` includes:

    * ``phase_outputs``:    mapping of phase_id → latest result dict
    * ``final_output``:     result dict of the last executed phase
    * ``iteration_history``: mapping of phase_id → list of prior results
      (empty list for phases that ran only once)
    * ``iteration_counts``:  mapping of phase_id → total execution count

    Examples:
        A template with two phases and a single success transition::

            phases:
              - id: fetch
                transitions:
                  success: process
              - id: process

        ``fetch`` runs first; on success, ``process`` runs next;
        ``process`` has no transitions so execution stops there.

        A review loop where ``review`` may revisit ``revise`` up to 3 times::

            phases:
              - id: write_draft
                transitions:
                  success: review
              - id: review
                max_iterations: 3
                transitions:
                  success: publish
                  failed: revise
              - id: revise
                transitions:
                  success: review   # loops back
              - id: publish
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, *args, **kwargs) -> None:
        """Initialise the sequencer, adding per-execution iteration tracking.

        All arguments are forwarded to :class:`PhaseSequencer.__init__`.
        Two extra instance attributes are added:

        ``iteration_history``
            ``Dict[str, List[dict]]`` — for each phase that executed more than
            once, holds the list of *prior* result dicts (oldest first).  The
            *current* result is always in ``phase_outputs``; history stores
            everything before the most recent run.  Reset at the start of each
            :meth:`execute` call.

        ``iteration_counts``
            ``Dict[str, int]`` — total execution count per phase (including the
            current run).  Reset at the start of each :meth:`execute` call.
        """
        # Extract git_handoff before forwarding to parent (Issue #674)
        _git_handoff = kwargs.pop("git_handoff", None)
        # If runner is a plain callable, wrap it in a minimal runner shim
        # so that unit tests can pass a bare function.
        if (
            "runner" in kwargs
            and callable(kwargs["runner"])
            and not hasattr(kwargs["runner"], "queue")
        ):
            kwargs["runner"] = _wrap_callable_runner(kwargs["runner"])
        elif len(args) >= 2 and callable(args[1]) and not hasattr(args[1], "queue"):
            args = (args[0], _wrap_callable_runner(args[1])) + args[2:]
        super().__init__(*args, **kwargs)
        self.iteration_history: Dict[str, List[dict]] = defaultdict(list)
        """Per-phase list of prior results (oldest first).  Current result is in
        ``phase_outputs``.  Reset on each :meth:`execute` call."""
        self.iteration_counts: Dict[str, int] = defaultdict(int)
        """Total execution count per phase.  Reset on each :meth:`execute` call."""
        self._loop_groups: Dict[str, List[str]] = {}
        """Loop group map built at the start of execute(). Empty until execute() runs.
        Maps each phase_id in a loop cycle to the ordered list of ALL phases in that cycle."""
        self._current_build_iter: int = 1
        """Current iteration being built; set by execute() before _build_phase_input call."""
        self._git_handoff = _git_handoff
        """Optional GitHandoff instance for commit-based phase tracking (Issue #674)."""

    # ------------------------------------------------------------------
    # Loop detection and iteration history helpers
    # ------------------------------------------------------------------

    def _reachable(self, start_id: Optional[str], target_id: str, visited: set) -> bool:
        """BFS reachability check through success transitions only.

        Args:
            start_id:  ID of the phase to start from (may be None).
            target_id: ID of the phase we want to reach.
            visited:   Mutable set of already-visited phase IDs (cycle guard).

        Returns:
            True if ``target_id`` is reachable from ``start_id`` via success
            transitions; False otherwise.
        """
        if start_id is None:
            return False
        if start_id == target_id:
            return True
        if start_id in visited:
            return False
        visited.add(start_id)
        phase = self._phase_map.get(start_id)
        if phase is None:
            return False
        effective = {**self.template.default_transitions, **phase.transitions}
        return self._reachable(effective.get("success"), target_id, visited)

    def _detect_loop_groups(self) -> Dict[str, List[str]]:  # noqa: C901
        """Detect loop groups from the transition graph.

        A loop group is the ordered list of phases that form a cycle via a
        ``request_changes`` backward edge and a ``success`` forward path,
        OR a self-loop where a phase's ``success`` transition points to itself
        (with ``max_iterations > 0``).

        For a cycle like ``A →[success]→ B →[success]→ C →[request_changes]→ A``,
        the loop group is ``["A", "B", "C"]`` (ordered by execution sequence within
        one cycle iteration).

        For a self-loop ``A →[success]→ A`` with ``max_iterations > 0``,
        the loop group is ``["A"]``.

        Returns:
            Dict mapping each phase_id in a loop to its ordered group list.
            Phases not in any loop are absent from the dict.
        """
        groups: Dict[str, List[str]] = {}

        for phase in self.template.phases:
            if phase.id in groups:
                continue  # Already assigned to a group

            effective = {**self.template.default_transitions, **phase.transitions}

            # Detect self-loop via success transition (phase → itself)
            success_target = effective.get("success")
            if success_target == phase.id and phase.max_iterations > 0:
                groups[phase.id] = [phase.id]
                continue

            rc_target = effective.get("request_changes")
            if rc_target is None:
                continue

            # Self-loop via request_changes (phase → itself): single-member group
            if rc_target == phase.id:
                groups[phase.id] = [phase.id]
                continue

            # Verify forward reachability: rc_target →[success*]→ phase.id
            if not self._reachable(rc_target, phase.id, visited=set()):
                continue

            # Walk the success chain from rc_target to phase.id to collect the group
            group: List[str] = []
            cursor: Optional[str] = rc_target
            seen: set = set()
            while cursor is not None and cursor not in seen:
                if cursor == phase.id and len(group) > 0:
                    # Completed the cycle
                    break
                seen.add(cursor)
                group.append(cursor)
                cursor_phase = self._phase_map.get(cursor)
                if cursor_phase is None:
                    break
                cursor_effective = {
                    **self.template.default_transitions,
                    **cursor_phase.transitions,
                }
                cursor = cursor_effective.get("success")

            group.append(phase.id)  # The closer of the cycle (has request_changes)

            # Deduplicate: guard against self-loop producing [A, A]
            seen_pids: set = set()
            deduped_group: List[str] = []
            for pid in group:
                if pid not in seen_pids:
                    seen_pids.add(pid)
                    deduped_group.append(pid)

            # Assign every member to this group
            for pid in deduped_group:
                groups[pid] = deduped_group

        return groups

    def _get_member_history(
        self, member_id: str, current_phase_id: str  # noqa: ARG002
    ) -> List[dict]:
        """Return the full result history for a loop group member.

        For ALL group members (including the current phase), this method returns
        ``iteration_history[member_id]`` combined with ``phase_outputs[member_id]``
        when needed.

        **Key timing detail:** ``iteration_history[phase_id]`` is only appended
        *after* a phase runs (the append happens in ``execute()`` post-result).
        This means when building history for round N, the current phase's round
        N-1 result is in ``phase_outputs[phase_id]``, not yet in
        ``iteration_history[phase_id]``.  The same logic applies equally to all
        group members — we always check ``phase_outputs`` as a supplement.

        The identity check (``is not``) is intentional: the same dict object is
        stored in both ``iteration_history`` and ``phase_outputs``.

        Args:
            member_id:        ID of the loop group member whose history to return.
            current_phase_id: ID of the phase currently being built (unused here,
                              kept for API symmetry and future overrides).
        """
        history = list(self.iteration_history.get(member_id, []))
        if member_id in self.phase_outputs:
            current_output = self.phase_outputs[member_id]
            # Avoid double-counting if it's already the last entry (identity check)
            if not history or history[-1] is not current_output:
                history.append(current_output)
        return history

    # Maximum characters per section in {iteration_history} (BC-14)
    _MAX_SECTION_CHARS: int = 4000

    def _build_iteration_history(self, phase_id: str, current_iter: int) -> str:  # noqa: C901
        """Build the ``{iteration_history}`` string for a phase at the given iteration.

        For phases in a loop group, includes prior outputs from ALL group members
        (not just a single partner), ordered by execution sequence within each round.

        Args:
            phase_id:     ID of the current phase.
            current_iter: Current iteration number (1-based).  At iteration 1 the
                          method returns an empty string (BC-8).

        Returns:
            Formatted history block (BC-13 through BC-16, BC-19), or ``""`` when
            ``current_iter <= 1`` or no prior history exists.
        """
        if current_iter <= 1:
            return ""

        group = self._loop_groups.get(phase_id, [])
        if not group:
            return ""

        # ── Git-based compact history (Issue #674) ──
        if self._git_handoff is not None and self._git_handoff.is_active():
            return self._build_git_iteration_history(phase_id, current_iter, group)

        # ── File-based inline history (existing behavior) ──
        output_dir_str = str(self.output_dir) if self.output_dir else None
        sections: List[str] = []

        for round_num in range(1, current_iter):
            for member_id in group:
                member_history = self._get_member_history(member_id, phase_id)
                if round_num > len(member_history):
                    # This member hasn't run in this round yet — omit section
                    continue

                text = _extract_phase_text(member_history[round_num - 1])
                if text is None:
                    text = ""
                # Strip verdict prefix lines (e.g. REQUEST_CHANGES, APPROVE,
                # ABORT) — these are routing metadata, not content (BC-7.3).
                stripped_lines: List[str] = []
                past_verdict = False
                for line in text.split("\n"):
                    if not past_verdict and line.strip().lower() in _VERDICT_KEYWORDS:
                        continue  # skip verdict-only line at start
                    past_verdict = True
                    stripped_lines.append(line)
                text = "\n".join(stripped_lines)
                if len(text) > self._MAX_SECTION_CHARS:
                    if output_dir_str:
                        safe_mid = re.sub(r"[^\w\-]", "_", member_id)
                        suffix = (
                            f"\n[...truncated, full output at "
                            f"{output_dir_str}/{safe_mid}_round{round_num}.md]"
                        )
                    else:
                        suffix = "\n[...truncated]"
                    text = text[: self._MAX_SECTION_CHARS] + suffix
                sections.append(f"--- Round {round_num}: {member_id} ---\n{text}")

        return "\n\n".join(sections) if sections else ""

    def _build_git_iteration_history(
        self, phase_id: str, current_iter: int, group: List[str]  # noqa: ARG002
    ) -> str:
        """Build compact iteration history using git commit references and diffs."""
        sections: List[str] = []

        for round_num in range(1, current_iter):
            for member_id in group:
                commit_sha = self._git_handoff.get_commit(member_id, round_num)
                if commit_sha is None:
                    continue
                short_sha = commit_sha[:8]
                header = f"--- Round {round_num}: {member_id} (commit {short_sha}) ---"

                diff = self._git_handoff.get_diff_for_member(member_id, round_num)
                if diff:
                    body = f"Changes from round {round_num - 1}:\n```diff\n{diff}\n```"
                else:
                    body = f"[Initial output — see commit {short_sha}]"

                sections.append(f"{header}\n{body}")

        return "\n\n".join(sections) if sections else ""

    # ------------------------------------------------------------------
    # Prompt building — override to inject {iteration_history}
    # ------------------------------------------------------------------

    def _build_phase_input(
        self,
        phase: PhaseDefinition,
        initial_input: dict,
        failure_context: str = "",
        missing_sink: Optional[set] = None,
    ) -> str:
        """Build the prompt string, injecting ``{iteration_history}`` for loop phases.

        This override computes the ``{iteration_history}`` value from
        :meth:`_build_iteration_history` using :attr:`_current_build_iter`,
        then delegates all other formatting to the parent implementation.

        .. note::
            ``StateMachineSequencer`` runs phases sequentially (single-threaded
            execution loop), so :attr:`_current_build_iter`` is always consistent
            when this method is called from :meth:`execute`.  Do NOT call this
            method from a parallel wave worker without setting
            ``_current_build_iter`` under ``_phase_outputs_lock`` first.
        """
        current_iter = getattr(self, "_current_build_iter", 1)
        history_str = self._build_iteration_history(phase.id, current_iter)

        # ── Git handoff variables (Issue #674) ──
        previous_commit = ""
        phase_diff = ""
        if self._git_handoff is not None and self._git_handoff.is_active():
            group = self._loop_groups.get(phase.id, [])
            if group and current_iter > 1:
                for member_id in reversed(group):
                    prev_sha = self._git_handoff.get_commit(member_id, current_iter - 1)
                    if prev_sha:
                        previous_commit = prev_sha[:8]
                        break
                all_diffs = []
                for member_id in group:
                    d = self._git_handoff.get_diff_for_member(member_id, current_iter - 1)
                    if d:
                        all_diffs.append(f"### {member_id}\n```diff\n{d}\n```")
                phase_diff = "\n\n".join(all_diffs)

        return super()._build_phase_input(
            phase,
            initial_input,
            failure_context,
            iteration_history=history_str,
            missing_sink=missing_sink,
            previous_commit=previous_commit,
            phase_diff=phase_diff,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, initial_input: dict = None, *, context: dict = None) -> dict:  # noqa: C901
        """Execute the pipeline following state-machine transitions.

        Starts at the first phase in ``template.phases``, resolves each
        transition after completion, and halts when:

        * A phase has no matching transition for its outcome (terminal state).
        * A loop phase (``max_iterations > 0``) exceeds its iteration limit —
          returns an abort dict with ``abort_reason = "MAX_ITERATIONS_EXCEEDED"``.
        * A non-loop phase (``max_iterations == 0``) would be revisited — logs
          a WARNING and stops (legacy cycle guard).

        Args:
            initial_input: Pipeline input dict (e.g. article brief). Also
                accepted as keyword argument ``context`` for compatibility.

        Note:
            If ``initial_input`` is ``None`` and ``context`` is provided,
            ``context`` is used as the input dict.

        Returns:
            Dict with keys:

            - ``phase_outputs``:     mapping of phase_id → latest result dict
            - ``final_output``:      result dict of the last executed phase
            - ``iteration_history``: mapping of phase_id → list of prior results
              (present only when execution completes normally or via cycle guard;
              also included in MAX_ITERATIONS_EXCEEDED abort dicts)
            - ``iteration_counts``:  mapping of phase_id → total execution count
        """
        # Compatibility: accept ``context`` as alias for ``initial_input``
        if initial_input is None and context is not None:
            initial_input = context
        elif initial_input is None:
            initial_input = {}

        if not self.template.phases:
            logger.warning(
                f"StateMachineSequencer: template '{self.template.id}' has no "
                f"phases — returning empty result."
            )
            return {"phase_outputs": {}, "final_output": {}}

        # ── Reset per-execution tracking (supports re-use of the sequencer) ───
        self.iteration_history = defaultdict(list)
        self.iteration_counts = defaultdict(int)

        # ── Detect loop groups for {iteration_history} variable (Issue #667) ──
        self._loop_groups = self._detect_loop_groups()
        if self._loop_groups:
            unique_groups = {tuple(g) for g in self._loop_groups.values()}
            for group_tuple in unique_groups:
                logger.info(
                    "Pipeline %s: detected loop group: %s → %s",
                    self.template.id,
                    " → ".join(group_tuple),
                    group_tuple[0],
                )

        # ── Git handoff initialization (Issue #674) ──────────────────────────
        if self._git_handoff is not None and self._loop_groups:
            try:
                if not self._git_handoff.initialize():
                    logger.warning("Git handoff initialization failed — falling back to file-based")
                    self._git_handoff = None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Git handoff initialization error: %s — falling back to file-based", exc
                )
                self._git_handoff = None

        # ── Pipeline-start hook (e.g. git branch creation) ────────────────────
        if self.on_pipeline_start is not None:
            try:
                self.on_pipeline_start(self.pipeline_context)
            except Exception as exc:
                logger.error(f"Pipeline {self.template.id}: on_pipeline_start hook failed: {exc}")
                raise

        # Entry point: first phase in template order
        current_phase_id: Optional[str] = self.template.phases[0].id

        # executed_sequence tracks phases in execution order (may contain repeats
        # for loop phases).  Used for final_output determination and logging.
        executed_sequence: List[str] = []

        final_result: dict = {}

        # ── Exhausted-route flag (Issue #615) ─────────────────────────────────
        # Set to True when the sequencer routes via PhaseOutcome.EXHAUSTED.
        # After the while loop, this causes final_result to be stamped with
        # aborted=True so the daemon records the run as failed even when
        # the postmortem phase itself completes successfully.
        self._exhausted_route: bool = False

        # ── Issue #978: global walk-step ceiling (defense-in-depth) ──────────
        # Per-phase EXHAUSTED guard (below) kills the proven review<->fix spin;
        # this ceiling is an absolute backstop against ANY future non-terminating
        # walk. Bound = total legal dispatching visits + a small EXHAUSTED-hop
        # margin; it can never trip a legitimate run.
        _walk_steps = 0
        _walk_step_ceiling = (
            sum(p.max_iterations for p in self.template.phases if p.max_iterations > 0)
            + len(self.template.phases)
            + 8
        )

        try:
            while current_phase_id is not None:
                _walk_steps += 1
                if _walk_steps > _walk_step_ceiling:
                    logger.error(
                        "Pipeline %s: walk-step ceiling (%d) exceeded at phase "
                        "'%s' — aborting a non-terminating state-machine walk.",
                        self.template.id,
                        _walk_step_ceiling,
                        current_phase_id,
                    )
                    last_phase = executed_sequence[-1] if executed_sequence else None
                    self._safe_call_hook(
                        self.on_pipeline_complete,
                        self.pipeline_context,
                        None,
                        pipeline_id=self.template.id,
                    )
                    return {
                        "phase_outputs": self.phase_outputs,
                        "final_output": (
                            self.phase_outputs.get(last_phase, {}) if last_phase else {}
                        ),
                        "iteration_history": dict(self.iteration_history),
                        "iteration_counts": dict(self.iteration_counts),
                        "aborted": True,
                        "abort_reason": "WALK_STEP_LIMIT",
                        "failed_phase": current_phase_id,
                        "error_message": (
                            f"Pipeline aborted: state-machine walk exceeded "
                            f"{_walk_step_ceiling} steps without terminating "
                            f"(last phase '{current_phase_id}')."
                        ),
                    }
                # ── Phase lookup ──────────────────────────────────────────────
                phase = self._phase_map.get(current_phase_id)
                if phase is None:
                    raise KeyError(
                        f"Phase '{current_phase_id}' referenced by transition is "
                        f"not defined in template '{self.template.id}'"
                    )

                # ── Iteration counting and limit enforcement ──────────────────
                self.iteration_counts[current_phase_id] += 1
                current_iter: int = self.iteration_counts[current_phase_id]

                if phase.max_iterations > 0:
                    # Explicit loop phase: enforce the phase-level max_iterations cap.
                    # When the count would exceed the limit, abort the pipeline.
                    if current_iter > phase.max_iterations:
                        # ── Issue #615: Route via EXHAUSTED before aborting ───
                        # Give templates a chance to handle exhaustion gracefully
                        # (e.g. route to a postmortem phase) instead of always aborting.
                        _exhausted_next = self._resolve_next_phase(
                            phase,
                            PhaseOutcome.EXHAUSTED,
                            self.phase_outputs.get(current_phase_id, {}),
                        )
                        # Issue #718: snapshot protect_on_approve on exhausted (implicit approval)
                        self._maybe_snapshot_on_approve(phase, _exhausted_next, is_exhausted=True)
                        # ── Phase 0 hard-gate (#840) ─────────────────────────
                        # When the exhausted phase is the existing_symbols
                        # inventory AND the admin feature_flags.phase0_hard_gate
                        # is True, OVERRIDE the YAML's graceful-degradation
                        # fallback (typically exhausted → spec) and HALT the
                        # pipeline. This is what consumers who care about
                        # sub-check 7d rigour want: an empty/missing inventory
                        # is a BLOCKER, not "fall through and grep ad-hoc".
                        from . import feature_flags as _ff  # noqa: PLC0415

                        if current_phase_id == _ff.PHASE_0_ID and _exhausted_next is not None:
                            if _ff.is_enabled("phase0_hard_gate"):
                                logger.warning(
                                    "Pipeline %s: Phase 0 exhausted AND "
                                    "feature_flags.phase0_hard_gate=True — "
                                    "overriding YAML's exhausted→%s fallback "
                                    "and HALTING (sub-check 7d hard gate).",
                                    self.template.id,
                                    _exhausted_next,
                                )
                                _exhausted_next = None
                        # ── Issue #978: termination guard ─────────────────────
                        # An over-cap phase routing EXHAUSTED must only re-enter a
                        # target that can actually DISPATCH. A target that is itself
                        # a loop phase already at/over its cap would re-exhaust on
                        # entry (the cap check at :3969 fires BEFORE dispatch), so
                        # re-entering it makes ZERO progress -> infinite no-dispatch
                        # spin (the #978 100%-CPU hang). Treat such a target like the
                        # "no transition" case: null it and fall through to the
                        # MAX_ITERATIONS_EXCEEDED abort below.
                        if _exhausted_next is not None:
                            _target_phase = self._phase_map.get(_exhausted_next)
                            if (
                                _target_phase is not None
                                and _target_phase.max_iterations > 0
                                # re-entry increments first (:3963): would it exceed?
                                and self.iteration_counts[_exhausted_next]
                                >= _target_phase.max_iterations
                            ):
                                logger.error(
                                    "Pipeline %s: EXHAUSTED route from '%s' resolves "
                                    "to '%s', which is itself at/over its "
                                    "max_iterations (%d) — re-entry cannot dispatch. "
                                    "Aborting to avoid a non-terminating walk.",
                                    self.template.id,
                                    current_phase_id,
                                    _exhausted_next,
                                    _target_phase.max_iterations,
                                )
                                _exhausted_next = None
                        if _exhausted_next is not None:
                            logger.info(
                                f"Pipeline {self.template.id}: MAX_ITERATIONS_EXCEEDED "
                                f"for phase '{current_phase_id}' — routing via 'exhausted' "
                                f"to phase '{_exhausted_next}'."
                            )
                            self._exhausted_route = True
                            current_phase_id = _exhausted_next
                            continue
                        # No exhausted transition — fall through to abort as before.
                        logger.error(
                            f"Pipeline {self.template.id}: MAX_ITERATIONS_EXCEEDED "
                            f"for phase '{current_phase_id}' "
                            f"(limit={phase.max_iterations}, attempted={current_iter}). "
                            f"Aborting pipeline."
                        )
                        last_phase = executed_sequence[-1] if executed_sequence else None
                        abort_result = {
                            "phase_outputs": self.phase_outputs,
                            "final_output": (
                                self.phase_outputs.get(last_phase, {}) if last_phase else {}
                            ),
                            "iteration_history": dict(self.iteration_history),
                            "iteration_counts": dict(self.iteration_counts),
                            "aborted": True,
                            "abort_reason": "MAX_ITERATIONS_EXCEEDED",
                            "failed_phase": current_phase_id,  # Issue #651: fix 'unknown' in daemon
                            "exceeded_phase": current_phase_id,
                            "error_message": (  # Issue #978: name the exhausted phase
                                f"Pipeline aborted: phase '{current_phase_id}' "
                                f"exhausted max_iterations and its exhausted/failed "
                                f"route resolves only to over-cap phase(s) "
                                f"(non-terminating loop prevented)."
                            ),
                        }
                        # ── Finding analysis (Issue #651) ─────────────────────────────────────
                        _finding_analysis = _analyze_round_findings(
                            self.output_dir, current_phase_id, phase.max_iterations
                        )
                        abort_result["finding_analysis"] = _finding_analysis
                        # ── Escalation detection (Issue #702): the exhausted phase
                        #    names its adversary via escalation_partner.
                        if phase.escalation_partner is not None:
                            partner_id = phase.escalation_partner
                            if partner_id not in self.phase_outputs:
                                logger.warning(
                                    f"Pipeline {self.template.id}: escalation_partner "  # noqa: E501
                                    f"{partner_id!r} for exhausted phase {current_phase_id!r} "  # noqa: E501
                                    f"has no output — skipping escalation detection."  # noqa: E501
                                )
                            elif phase.adversary_config is None:
                                # Issue #703: the legacy spec_adversary escalation
                                # parser was removed. Escalation detection now
                                # REQUIRES adversary_config; without it, skip with a
                                # warning (graceful degradation — a raise here would
                                # be swallowed by the enclosing escalation try/except).
                                logger.warning(
                                    f"Pipeline {self.template.id}: exhausted phase "  # noqa: E501
                                    f"{current_phase_id!r} names escalation_partner "  # noqa: E501
                                    f"{partner_id!r} but has no adversary_config — "  # noqa: E501
                                    f"skipping escalation detection."  # noqa: E501
                                )
                            else:
                                try:
                                    adv_raw = _extract_phase_text(self.phase_outputs[partner_id])
                                    from .adversary_parser import (  # noqa: PLC0415
                                        parse_adversary_output,
                                    )

                                    adv_verdict = parse_adversary_output(
                                        adv_raw, phase.adversary_config
                                    )
                                    if adv_verdict.verdict == "REQUEST_CHANGES":
                                        abort_result["escalation_required"] = True
                                        abort_result["escalation_reason"] = (
                                            f"{partner_id}_loop_exhausted"
                                        )
                                        abort_result["adversary_findings"] = [
                                            {"category": f.category, "description": f.description}
                                            for f in adv_verdict.findings
                                        ]
                                        logger.error(
                                            f"Pipeline {self.template.id}: {partner_id} loop "  # noqa: E501
                                            f"exhausted after {phase.max_iterations} iterations "  # noqa: E501
                                            f"— human review required. "  # noqa: E501
                                            f"Findings: {len(adv_verdict.findings)}"
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning(
                                        f"Pipeline {self.template.id}: escalation detection failed: {exc}"  # noqa: E501
                                    )
                        self._safe_call_hook(
                            self.on_pipeline_complete,
                            self.pipeline_context,
                            None,
                            pipeline_id=self.template.id,
                        )
                        return abort_result
                else:
                    # Non-loop phase (max_iterations == 0): apply legacy cycle guard.
                    # A second visit to such a phase is always an accidental cycle.
                    if current_iter > 1:
                        logger.warning(
                            f"Pipeline {self.template.id}: cycle detected — phase "
                            f"'{current_phase_id}' would be visited {current_iter} times "
                            f"(chain: {' → '.join(executed_sequence)}). "
                            f"Set max_iterations > 0 on the phase to enable intentional "
                            f"loops. Stopping."
                        )
                        # Undo the increment so iteration_counts reflects actual executions
                        self.iteration_counts[current_phase_id] -= 1
                        break

                executed_sequence.append(current_phase_id)

                # ── on_phase_start callback ───────────────────────────────────
                # step_index = position in executed_sequence (0-based, repeats for loops)
                self._invoke_on_phase_start(current_phase_id, phase, len(executed_sequence) - 1)

                # ── Dialogue phase dispatch + gate (Track B + #840) ──────────
                # State-machine path: a type:dialogue phase routed via
                # transitions hits this dispatch site. The gate must be checked
                # here so the flag applies regardless of which sequencer the
                # consumer uses (PhaseSequencer linear / parallel paths cover
                # the non-state-machine case in _execute_wave_*).
                if getattr(phase, "dialogue_config", None) is not None:
                    from . import feature_flags as _ff  # noqa: PLC0415

                    if not _ff.is_enabled("dialogue_phase"):
                        logger.info(
                            "Pipeline %s: dialogue phase '%s' SKIPPED "
                            "(state-machine path) — admin "
                            "feature_flags.dialogue_phase is False.",
                            self.template.id,
                            current_phase_id,
                        )
                        result = {
                            "state": "skipped_by_feature_flag",
                            "result": "",
                            "skipped_reason": "feature_flags.dialogue_phase is False",
                            "cost_usd": 0.0,
                            "tokens_consumed": 0,
                            "execution_time_seconds": 0.0,
                        }
                        with self._phase_outputs_lock:
                            self.phase_outputs[current_phase_id] = result
                        self._invoke_on_phase_complete(current_phase_id, result)
                        # Route via SUCCESS transition (skip == clean exit).
                        next_phase_id = self._resolve_next_phase(
                            phase,
                            PhaseOutcome.SUCCESS,
                            result,
                        )
                        if next_phase_id is None:
                            current_phase_id = None
                        else:
                            current_phase_id = next_phase_id
                        continue
                    self._current_build_iter = current_iter
                    phase_input = self._build_phase_input(phase, initial_input)
                    result = self._execute_dialogue_phase(phase, phase_input)
                    with self._phase_outputs_lock:
                        self.phase_outputs[current_phase_id] = result
                    self._invoke_on_phase_complete(current_phase_id, result)
                    phase_state = result.get("state", "unknown")
                    if phase_state != "success":
                        return {
                            "phase_outputs": self.phase_outputs,
                            "final_output": result,
                            "failed_phase": current_phase_id,
                            "aborted": True,
                        }
                    next_phase_id = self._resolve_next_phase(
                        phase,
                        PhaseOutcome.SUCCESS,
                        result,
                    )
                    if next_phase_id is None:
                        current_phase_id = None
                    else:
                        current_phase_id = next_phase_id
                    continue

                # ── Build prompt and submit task ──────────────────────────────
                # Set current iteration so _build_phase_input override can inject
                # the correct {iteration_history} value (Issue #648a).
                self._current_build_iter = current_iter
                _missing_sink: set = set()
                phase_input = self._build_phase_input(
                    phase, initial_input, missing_sink=_missing_sink
                )
                command_extras = self._build_command_extras(
                    phase, initial_input, missing_sink=_missing_sink
                )

                # Reject the phase before dispatch if a genuine config/input/
                # previous_output reference rendered <MISSING:> (#535). Mirrors
                # the folder-guard abort below — append the prior output to
                # iteration_history, stamp the iteration on metadata, invoke the
                # pipeline-complete hook, and return the abort dict with
                # iteration_history / iteration_counts. The guard precedes
                # _execute_and_wait so a placeholder failure never enters the
                # retry loop.
                placeholder_failure = self._check_for_unresolved_placeholders(phase, _missing_sink)
                if placeholder_failure is not None:
                    result = placeholder_failure
                    with self._phase_outputs_lock:
                        if current_phase_id in self.phase_outputs:
                            self.iteration_history[current_phase_id].append(
                                self.phase_outputs[current_phase_id]
                            )
                        result.setdefault("metadata", {})["iteration"] = current_iter
                        self.phase_outputs[current_phase_id] = result
                    self._invoke_on_phase_complete(current_phase_id, result)
                    self._safe_call_hook(
                        self.on_pipeline_complete,
                        self.pipeline_context,
                        None,
                        pipeline_id=self.template.id,
                    )
                    return {
                        "phase_outputs": self.phase_outputs,
                        "final_output": result,
                        "failed_phase": current_phase_id,
                        "aborted": True,
                        "iteration_history": dict(self.iteration_history),
                        "iteration_counts": dict(self.iteration_counts),
                    }

                preferred_model = self._resolve_model_tier(phase.model_tier)

                task = TaskSpec(
                    type=self._resolve_task_type(phase.task_type),
                    payload={
                        "prompt": phase_input,
                        "phase_id": phase.id,
                        "pipeline_id": self.template.id,
                        "model_chain": phase.model_chain or [],  # #347: propagate fallback chain
                        "sandbox_roots": self._sandbox_roots(),  # #794: tool-call sandbox
                        **command_extras,
                    },
                    priority=Priority.HIGH,
                    preferred_model=preferred_model,
                    timeout_seconds=phase.timeout_minutes * 60,
                )

                # ── Snapshot protected_paths before each iteration (#706)
                # Re-snapshot per iteration so each retry baseline reflects
                # current state of the guarded directory (not iteration-1 state).
                # Local dict — no shared instance state.
                _path_snapshots = self._snapshot_protected_paths(phase)

                task_id = self.runner.queue.submit_task(task)
                logger.info(
                    f"Pipeline {self.template.id}: submitted phase "
                    f"'{current_phase_id}' "
                    f"(task_id={task_id}, iteration={current_iter})"
                )

                # ── Execute and wait (with retry logic from parent) ───────────
                result = self._execute_and_wait(task_id, phase, initial_input=initial_input)

                # ── Write FILE blocks if requested (#189) ─────────────────────
                if phase.write_files:
                    self._handle_file_write(phase, result)

                # ── Folder-guard verification — check if protected_paths were modified (#706)
                path_guard_failure = self._verify_protected_paths(phase, _path_snapshots)
                if path_guard_failure is not None:
                    result = path_guard_failure
                    with self._phase_outputs_lock:
                        if current_phase_id in self.phase_outputs:
                            self.iteration_history[current_phase_id].append(
                                self.phase_outputs[current_phase_id]
                            )
                        result.setdefault("metadata", {})["iteration"] = current_iter
                        self.phase_outputs[current_phase_id] = result
                    self._invoke_on_phase_complete(current_phase_id, result)
                    self._safe_call_hook(
                        self.on_pipeline_complete,
                        self.pipeline_context,
                        None,
                        pipeline_id=self.template.id,
                    )
                    return {
                        "phase_outputs": self.phase_outputs,
                        "final_output": result,
                        "failed_phase": current_phase_id,
                        "aborted": True,
                        "iteration_history": dict(self.iteration_history),
                        "iteration_counts": dict(self.iteration_counts),
                    }

                # ── Hash verification — check if THIS phase tampered with protected files (#531)
                guard_failure = self._verify_protected_hashes(phase)
                if guard_failure is not None:
                    result = guard_failure
                    with self._phase_outputs_lock:
                        if current_phase_id in self.phase_outputs:
                            self.iteration_history[current_phase_id].append(
                                self.phase_outputs[current_phase_id]
                            )
                        result.setdefault("metadata", {})["iteration"] = current_iter
                        self.phase_outputs[current_phase_id] = result
                    self._invoke_on_phase_complete(current_phase_id, result)
                    self._safe_call_hook(
                        self.on_pipeline_complete,
                        self.pipeline_context,
                        None,
                        pipeline_id=self.template.id,
                    )
                    return {
                        "phase_outputs": self.phase_outputs,
                        "final_output": result,
                        "failed_phase": current_phase_id,
                        "aborted": True,
                        "iteration_history": dict(self.iteration_history),
                        "iteration_counts": dict(self.iteration_counts),
                    }

                # ── Result enrichment from disk (Issue #681) ─────────────────
                self._enrich_result_from_disk(current_phase_id, result)

                with self._phase_outputs_lock:
                    # Save the previous result to iteration_history before overwriting.
                    # This preserves the full per-phase execution history for
                    # observability (the current result is always in phase_outputs).
                    if current_phase_id in self.phase_outputs:
                        self.iteration_history[current_phase_id].append(
                            self.phase_outputs[current_phase_id]
                        )
                    # Annotate the result with the iteration number so consumers
                    # can identify which run produced each output.
                    result.setdefault("metadata", {})["iteration"] = current_iter
                    self.phase_outputs[current_phase_id] = result

                # ── Git handoff: commit phase output (Issue #674) ─────────────
                if (
                    self._git_handoff is not None
                    and self._git_handoff.is_active()
                    and current_phase_id in self._loop_groups
                ):
                    phase_text = _extract_phase_text(result)
                    if phase_text is not None:
                        self._git_handoff.commit_phase_output(
                            current_phase_id, current_iter, phase_text
                        )

                # ── Hash capture — record protected_outputs from THIS phase for future verification (#531)  # noqa: E501
                if result.get("state") == "success" and getattr(phase, "protected_outputs", []):
                    self._store_protected_hashes(phase)

                # ── Supervisor hook (#194) ────────────────────────────────────
                if getattr(phase, "supervisor", False) and result.get("state") == "success":
                    result, abort_info = self._run_supervisor_for_phase(
                        phase, result, initial_input
                    )
                    if abort_info:
                        logger.error(
                            f"Pipeline {self.template.id}: aborted by supervisor "
                            f"on phase '{phase.id}'"
                        )
                        self._safe_call_hook(
                            self.on_pipeline_complete,
                            self.pipeline_context,
                            None,
                            pipeline_id=self.template.id,
                        )
                        return abort_info
                    with self._phase_outputs_lock:
                        self.phase_outputs[current_phase_id] = result

                # ── Record adversary reward (Issue #546 / #702) ───────────────
                self._record_adversary_outcome(phase, result)

                # ── on_phase_complete callback ────────────────────────────────
                self._invoke_on_phase_complete(current_phase_id, result)

                phase_state = result.get("state", "unknown")
                logger.info(
                    f"Pipeline {self.template.id}: phase '{current_phase_id}' "
                    f"completed (state={phase_state}, iteration={current_iter})"
                )

                # ── Transition resolution ─────────────────────────────────────
                outcome: PhaseOutcome = determine_outcome(result)
                next_phase_id = self._resolve_next_phase(phase, outcome, result)

                # Issue #718: snapshot protect_on_approve on approve verdict
                self._maybe_snapshot_on_approve(phase, next_phase_id)

                if next_phase_id is None:
                    # Terminal state — no outgoing transition for this outcome
                    logger.info(
                        f"Pipeline {self.template.id}: phase '{current_phase_id}' "
                        f"is terminal (no '{outcome.value}' transition). "
                        f"Execution complete."
                    )
                    current_phase_id = None  # exit the loop cleanly
                else:
                    logger.info(
                        f"Pipeline {self.template.id}: "
                        f"'{current_phase_id}' →[{outcome.value}]→ '{next_phase_id}'"
                    )
                    current_phase_id = next_phase_id

            # ── Build final result ────────────────────────────────────────────
            last_phase_id = executed_sequence[-1] if executed_sequence else None
            final_output = self.phase_outputs.get(last_phase_id, {}) if last_phase_id else {}
            final_result = {
                "phase_outputs": self.phase_outputs,
                "final_output": final_output,
                "iteration_history": dict(self.iteration_history),
                "iteration_counts": dict(self.iteration_counts),
            }

        except Exception:
            self._safe_call_hook(
                self.on_pipeline_complete,
                self.pipeline_context,
                None,
                pipeline_id=self.template.id,
            )
            raise

        # ── Git handoff finalize + cleanup (Issue #674) ──────────────────────
        if self._git_handoff is not None:
            pipeline_failed = final_result.get("aborted", False)
            if not pipeline_failed and self._git_handoff.is_active():
                try:
                    target_branch = self._git_handoff.original_branch
                    self._git_handoff.finalize(self.output_dir, target_branch)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Git handoff finalize failed: %s — final files remain in %s",
                        exc,
                        self.output_dir,
                    )
            self._git_handoff.cleanup(preserve=pipeline_failed)

        # ── Issue #615: Stamp aborted=True when routed via exhausted ─────────
        # Even if the postmortem phase completed "successfully", the pipeline
        # run must be recorded as failed.  The daemon treats aborted=True as
        # a failed run, so we inject it here after normal completion.
        if self._exhausted_route:
            final_result["aborted"] = True
            final_result["abort_reason"] = "EXHAUSTED_ROUTE"

        # ── Pipeline-complete hook (success path) ─────────────────────────────
        self._safe_call_hook(
            self.on_pipeline_complete,
            self.pipeline_context,
            final_result,
            pipeline_id=self.template.id,
        )

        return final_result

    # ------------------------------------------------------------------
    # Transition helper
    # ------------------------------------------------------------------

    def _resolve_next_phase(  # noqa: C901
        self,
        phase: PhaseDefinition,
        outcome: PhaseOutcome,
        result: Optional[dict] = None,
    ) -> Optional[str]:
        """Return the next phase ID for *outcome*, or ``None`` if terminal.

        Effective transitions are computed by merging ``template.default_transitions``
        with the phase-level ``phase.transitions`` dict (phase overrides default on
        a per-key basis)::

            effective = {**template.default_transitions, **phase.transitions}

        **Content-based routing (Issue #301):** If any of the verdict keywords
        (``approve``, ``request_changes``, ``abort``) appear as keys in the
        effective transitions dict, the method also calls
        :func:`~.transitions.extract_verdict` on the phase output text.
        If a verdict is found *and* matches a key in ``effective``, it is used
        instead of ``outcome.value``.  This is opt-in — phases that do not list
        verdict keywords in their transitions are unaffected.

        Args:
            phase:   The phase that just completed.
            outcome: The :class:`~.transitions.PhaseOutcome` for the result.
            result:  The raw result dict from the executor (optional).  Used
                     to extract LLM output text for verdict-based routing.

        Returns:
            Phase ID string if a transition is defined for *outcome*
            (or a content verdict), ``None`` if this is a terminal state.
        """
        effective: dict = {
            **self.template.default_transitions,
            **phase.transitions,
        }

        # ── Exhausted fallback to failed (Issue #615) ────────────────────────
        # EXHAUSTED is a sequencer-internal outcome and must never be subject
        # to content-based verdict extraction. Check this BEFORE the
        # content-routing block so that phases with both verdict keys
        # (e.g. spec_adversary with request_changes) and an exhausted
        # transition always route via exhausted, not via the verdict.
        if outcome == PhaseOutcome.EXHAUSTED:
            if "exhausted" in effective:
                return effective["exhausted"]
            return effective.get("failed")

        # ── Content-based routing (opt-in via verdict keys in transitions) ───
        # Only attempt verdict extraction when at least one verdict keyword
        # appears as a transition key — this keeps the common path fast and
        # avoids any text-parsing overhead for non-review phases.
        _verdict_keys = {"approve", "request_changes", "abort"}
        if result is not None and _verdict_keys.intersection(effective):
            # Build the output file path for file-based verdict reading (#678)
            output_file: str | None = None
            if self.output_dir:
                safe_pid = phase.id.replace("-", "_")
                _candidate = Path(self.output_dir) / f"{safe_pid}.md"
                if _candidate.exists():
                    output_file = str(_candidate)

            output_text: str = ""
            raw_result = result.get("result", {})
            if isinstance(raw_result, dict):
                output_text = raw_result.get("text", "") or ""
                # Fallback: OpenClaw executor stores output in partial_output
                if not output_text:
                    output_text = raw_result.get("partial_output", "") or ""
            if not output_text:
                output_text = result.get("text", "") or ""

            verdict = extract_verdict(text=output_text, file_path=output_file)
            if verdict is not None and verdict in effective:
                logger.debug(
                    f"Pipeline {self.template.id}: phase '{phase.id}' "
                    f"content-routed via verdict '{verdict}'"
                )
                return effective[verdict]

            # Verdict extraction failed (or returned a verdict not in transitions).
            # Log a warning so the fallback is observable — silent fallthrough
            # previously caused misleading "SUCCESS: N phases completed" messages.
            if outcome == PhaseOutcome.SUCCESS:
                fallback = effective.get("success")
                logger.warning(
                    f"Pipeline {self.template.id}: phase '{phase.id}' is verdict-routed "
                    f"but verdict extraction returned {verdict!r} — falling through to "
                    f"'success' fallback '{fallback}' (issue #680)"
                )

        return effective.get(outcome.value)

    # ------------------------------------------------------------------
    # Hook helper
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_call_hook(hook, *args, pipeline_id: str = "") -> None:
        """Call *hook* with *args*, logging but swallowing all exceptions.

        Kept as a ``@staticmethod`` on this class so it stays scoped to
        :class:`StateMachineSequencer` rather than polluting the module namespace.
        This mirrors the pattern used throughout :class:`PhaseSequencer` so that
        a misbehaving ``on_pipeline_complete`` callback never crashes the pipeline.
        """
        if hook is None:
            return
        try:
            hook(*args)
        except Exception as hook_exc:  # noqa: BLE001
            logger.warning(f"Pipeline {pipeline_id}: on_pipeline_complete hook failed: {hook_exc}")
