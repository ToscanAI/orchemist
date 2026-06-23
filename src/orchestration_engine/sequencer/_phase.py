"""PhaseSequencer — topological-order pipeline executor.

EPIC #942 sub-issue 953c: this class was moved VERBATIM out of the
``sequencer`` package facade (``__init__.py``) into its own one-class
module. No logic changed; the facade re-exports :class:`PhaseSequencer`
so every historical ``from orchestration_engine.sequencer import
PhaseSequencer`` keeps resolving byte-identically.
"""

import logging
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..file_guard import compute_directory_hash, compute_hash
from ..output_parser import extract_and_write, parse_output
from ..review_parser import ReviewOutcome, parse_review_output
from ..schemas import Priority, TaskError, TaskResult, TaskSpec, TaskState, TaskType
from ..templates import PhaseDefinition, PipelineTemplate, TemplateEngine
from ..timestamps import now_utc
from ._consts import _DEFAULT_SUPERVISOR_PROMPT, _TERMINAL_PUNCTUATION
from ._helpers import (
    _extract_phase_text,
    _format_failure_context,
    _load_skill,
    _parse_supervisor_response,
    _resolve_model_tier,
    _resolve_task_type,
    _sanitize_error_for_prompt,
)
from ._proxies import (
    _PhaseOutput,
    _PreviousOutputInlineProxy,
    _PreviousOutputProxy,
    _SafeDict,
)

logger = logging.getLogger(__name__)


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
                from .. import feature_flags as _ff  # noqa: PLC0415

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
            preferred_model = self._resolve_model_tier(
                phase.model_tier, phase.min_tier, phase.max_tier
            )

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
                from .. import feature_flags as _ff  # noqa: PLC0415

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

            preferred_model = self._resolve_model_tier(
                phase.model_tier, phase.min_tier, phase.max_tier
            )

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
                from ..adversary_parser import (  # noqa: PLC0415
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
                    preferred_model=self._resolve_model_tier(
                        phase.model_tier, phase.min_tier, phase.max_tier
                    ),
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

    # Stateless parser relocated to ._helpers (EPIC #942 953b). The
    # ``staticmethod(...)`` wrapper preserves no-self semantics so
    # ``self._parse_supervisor_response(text)`` and
    # ``PhaseSequencer._parse_supervisor_response(text)`` resolve byte-identically.
    _parse_supervisor_response = staticmethod(_parse_supervisor_response)

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

    # Stateless skill loader relocated to ._helpers (EPIC #942 953b). The
    # ``staticmethod(...)`` wrapper preserves no-self semantics so both
    # ``self._load_skill(...)`` and ``PhaseSequencer._load_skill(...)`` resolve
    # byte-identically.
    _load_skill = staticmethod(_load_skill)

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

    # Stateless retry-prompt helpers relocated to ._helpers (EPIC #942 953b).
    # The ``staticmethod(...)`` wrappers preserve no-self semantics so both
    # ``self._x(...)`` and ``PhaseSequencer._x(...)`` resolve byte-identically.
    _sanitize_error_for_prompt = staticmethod(_sanitize_error_for_prompt)
    _format_failure_context = staticmethod(_format_failure_context)

    def _build_hook_failure_result(
        self, phase, current_iter, hook_name, command, exit_code, output
    ) -> dict:
        """Build a terminal abort phase-result dict for a failed lifecycle hook (#986).

        Mirrors the placeholder/folder-guard terminal returns so the caller can
        abort the run identically: ``state == "permanently_failed"`` marks a hard,
        non-retryable stop (a phase must NEVER proceed against a stale/broken
        build).
        """
        msg = (
            f"Lifecycle hook {hook_name!r} failed (exit {exit_code}) before phase "
            f"'{phase.id}': command {command!r}. Refusing to run the phase against a "
            f"stale/broken build. Output:\n{output}"
        )
        logger.error("Pipeline %s: %s", self.template.id, msg)
        return {
            "state": "permanently_failed",
            "result": msg,
            "error": msg,
            "hook_name": hook_name,
            "cost_usd": 0.0,
            "tokens_consumed": 0,
            "execution_time_seconds": 0.0,
            "metadata": {"iteration": current_iter},
        }

    def _run_lifecycle_hooks(self, phase, current_iter):  # noqa: C901
        """Run declared lifecycle hooks on a content-hash MISS (#986).

        Called at the per-phase dispatch seam (before ``submit_task``). For each
        hook declared on the template, compute the content-hash of its
        ``invalidation`` glob-set rooted at ``config['repo_path']``; on a MISS
        (stored hash differs, or first-ever) dispatch the hook command through
        :class:`~.command_executor.CommandExecutor` and store the new hash on
        success; on a HIT skip (the warm artifact is reused).

        Returns ``None`` on success / no-op, or a terminal phase-result abort dict
        when a hook MISS fails (security-block, non-zero exit, timeout) so the
        caller can abort the run — a phase must NEVER proceed against
        stale/broken state, and a failed hook's hash is NOT stored.
        """
        hooks_cfg = getattr(self.template, "lifecycle_hooks", None)
        if hooks_cfg is None or not hooks_cfg.hooks:
            return None  # byte-identical default: no hooks → no-op

        # Hooks shell out to real build/seed commands; in dry-run (structure
        # smoke test) we must NOT run them. Mirror _check_for_unresolved_placeholders.
        if self._is_dry_run_mode():
            return None

        repo_path = self.config.get("repo_path") if isinstance(self.config, dict) else None
        if not repo_path:
            # No consumer repo to root the globs against → cannot compute inputs.
            # A template that declares hooks but supplies no repo_path is
            # misconfigured; the byte-identical no-hooks path is the safe fallback
            # rather than aborting an otherwise-valid run.
            logger.warning(
                "Pipeline %s: lifecycle_hooks declared but config['repo_path'] is "
                "absent — skipping hooks.",
                self.template.id,
            )
            return None

        from ..command_executor import CommandExecutor  # noqa: PLC0415 — import-cycle safety
        from ..file_guard import hash_glob_set  # noqa: PLC0415

        for name, hook in hooks_cfg.hooks.items():
            current_hash = hash_glob_set(repo_path, hook.invalidation)
            if self._warm_cache.get(name) == current_hash:
                logger.debug(
                    "Pipeline %s: lifecycle hook %r HIT (inputs unchanged) — "
                    "reusing warm artifact.",
                    self.template.id,
                    name,
                )
                continue  # HIT — reuse, skip the hook

            logger.info(
                "Pipeline %s: lifecycle hook %r MISS — running %r.",
                self.template.id,
                name,
                hook.command,
            )
            executor = CommandExecutor(default_timeout=hooks_cfg.timeout_seconds)
            spec = TaskSpec(
                type=TaskType.COMMAND,
                payload={
                    "command": hook.command,
                    "allowed_commands": hooks_cfg.allowed_commands or [],
                    "cwd": str(repo_path),
                },
            )
            result = executor.execute(spec)
            if result.state != TaskState.SUCCESS:
                # NEVER proceed against a broken build/seed — abort the run.
                # Do NOT store the new hash (a failed MISS leaves no success residue).
                text = (result.result or {}).get("text", "")
                exit_code = (result.result or {}).get("exit_code", -1)
                return self._build_hook_failure_result(
                    phase, current_iter, name, hook.command, exit_code, text
                )
            # Success — store the new input-hash so subsequent phases HIT.
            self._warm_cache[name] = current_hash

        return None

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
        from ..dialogue_phase import DialogueRunner  # noqa: PLC0415

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

    def _register_dialogue_executors_once(self) -> None:
        """Lazily append dialogue-participant executors (e.g. ``gemini_cli``) that
        the mode factory did not build, to ``self.runner.executors``, at the
        dialogue-dispatch chokepoint (#1051).

        A dialogue phase names its drafter/reviewer under
        ``dialogue_config.{drafter,reviewer}.executor``, not the per-phase
        ``provider:``, so no mode factory ever constructs the GeminiCliExecutor a
        ``gemini_cli`` participant requires. Calling this at the TOP of
        :meth:`_resolve_dialogue_executor` covers CLI + daemon + web + eval +
        programmatic (and the state-machine path via
        ``StateMachineSequencer(PhaseSequencer)``, which inherits this method) in
        ONE seam. Idempotent and dry-run-guarded — safe to call on every resolve.
        """
        runner = getattr(self, "runner", None)
        executors = getattr(runner, "executors", None)
        if runner is None or executors is None:
            return
        # Dry-run guard WITHOUT a mode string (the sequencer has none): if EVERY
        # currently-registered executor is a DryRunExecutor, skip the append so
        # the all-dry-run fallback in :meth:`_resolve_executor_by_name` stays
        # exactly intact. This is the precise inverse of the fallback condition
        # ``len(dry_runners) == len(executors) and dry_runners`` — appending a
        # non-dry-run gemini executor would flip that to False and break the
        # dry-run template-validation suite.
        if executors and all("dryrun" in type(e).__name__.lower() for e in executors):
            return
        # Idempotent: _append_dialogue_executors skips when a "gemini"
        # provider_name is already present, so a GeminiCliExecutor added by a
        # prior resolve / a prior dialogue phase is never duplicated. Lazy import
        # keeps the dependency contained (no import cycle exists either way).
        from ..pipeline_runner import PipelineRunner  # noqa: PLC0415

        PipelineRunner._append_dialogue_executors(executors, getattr(self, "template", None))

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

        Before delegating it runs :meth:`_register_dialogue_executors_once`, the
        lazy/idempotent/dry-run-guarded append (#1051) that ensures a
        ``gemini_cli`` participant has a real :class:`GeminiCliExecutor` to
        resolve to, for EVERY entrypoint that dispatches dialogue through this
        chokepoint.

        Returns ``None`` when no matching executor is registered, EXCEPT in
        dry-run mode (only a :class:`~.runner.DryRunExecutor` is registered),
        where the dry-run executor is returned as a fallback so dialogue
        phases can be validated by the template-validation suite without
        real provider credentials.
        """
        self._register_dialogue_executors_once()
        return self._resolve_executor_by_name(name)

    # Stateless tier/task-type resolvers relocated to ._helpers (EPIC #942 953b).
    # The ``staticmethod(...)`` wrappers preserve no-self semantics so both
    # ``self._x(...)`` and ``PhaseSequencer._x(...)`` resolve byte-identically.
    _resolve_task_type = staticmethod(_resolve_task_type)
    _resolve_model_tier = staticmethod(_resolve_model_tier)
