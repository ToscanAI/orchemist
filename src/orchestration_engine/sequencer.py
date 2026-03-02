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
import threading
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .output_parser import extract_and_write, parse_output
from .schemas import Priority, TaskError, TaskResult, TaskSpec, TaskState, TaskType
from .templates import PhaseDefinition, PipelineTemplate, TemplateEngine
from .transitions import PhaseOutcome, determine_outcome, extract_verdict

logger = logging.getLogger(__name__)

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

    def __init__(self, template: PipelineTemplate, runner, config: dict = None,
                 on_phase_complete=None, on_phase_start=None,
                 on_pipeline_start=None, on_pipeline_complete=None,
                 output_dir=None) -> None:
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

        # Fast phase lookup by ID (Issue #231) — avoids O(n) linear scan per phase
        self._phase_map: Dict[str, PhaseDefinition] = {
            p.id: p for p in template.phases
        }

        # Thread-safety locks (Issue #102)
        self._phase_outputs_lock: threading.Lock = threading.Lock()
        """Protects ``phase_outputs`` during concurrent wave execution."""
        self._callback_lock: threading.Lock = threading.Lock()
        """Serialises on_phase_start / on_phase_complete callback invocations."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, initial_input: dict) -> dict:
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
                logger.error(
                    f"Pipeline {self.template.id}: on_pipeline_start hook failed: {exc}"
                )
                raise

        final_result: dict = {}
        try:
            for wave_index, wave in enumerate(execution_order):
                # Decide whether to run this wave in parallel.
                # A wave of size 1 is always sequential (no overhead, no ambiguity).
                use_parallel = (
                    self.template.parallel
                    and len(wave) > 1
                )

                if use_parallel:
                    abort_result = self._execute_wave_parallel(
                        wave, wave_index, initial_input
                    )
                else:
                    abort_result = self._execute_wave_sequential(
                        wave, wave_index, initial_input
                    )

                # Either method returns None on success or a final_result dict on abort
                if abort_result is not None:
                    # Call pipeline-complete hook signalling failure
                    if self.on_pipeline_complete is not None:
                        try:
                            self.on_pipeline_complete(self.pipeline_context, None)
                        except Exception as hook_exc:
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
                except Exception as hook_exc:
                    logger.warning(
                        f"Pipeline {self.template.id}: "
                        f"on_pipeline_complete hook (exception path) failed: {hook_exc}"
                    )
            raise

        # Call pipeline-complete hook on success
        if self.on_pipeline_complete is not None:
            try:
                self.on_pipeline_complete(self.pipeline_context, final_result)
            except Exception as hook_exc:
                logger.warning(
                    f"Pipeline {self.template.id}: on_pipeline_complete hook failed: {hook_exc}"
                )

        return final_result

    # ------------------------------------------------------------------
    # Wave execution strategies
    # ------------------------------------------------------------------

    def _execute_wave_sequential(
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

            # Build the prompt for this phase
            phase_input = self._build_phase_input(phase, initial_input)

            # Resolve model tier to a ModelTier enum value (if possible)
            preferred_model = self._resolve_model_tier(phase.model_tier)

            # Create and queue the TaskSpec
            task = TaskSpec(
                type=self._resolve_task_type(phase.task_type),
                payload={
                    "prompt": phase_input,
                    "phase_id": phase.id,
                    "pipeline_id": self.template.id,
                },
                priority=Priority.HIGH,
                preferred_model=preferred_model,
                timeout_seconds=phase.timeout_minutes * 60,
            )

            task_id = self.runner.queue.submit_task(task)
            logger.info(
                f"Pipeline {self.template.id}: submitted phase '{phase_id}' "
                f"(task_id={task_id})"
            )

            # Execute synchronously and store output
            result = self._execute_and_wait(task_id, phase, initial_input=initial_input)

            # Write FILE blocks to disk if phase requests it (#189)
            if phase.write_files:
                self._handle_file_write(phase, result)

            with self._phase_outputs_lock:
                self.phase_outputs[phase_id] = result

            # Supervisor hook (#194) — sequential path
            if getattr(phase, 'supervisor', False) and result.get('state') == 'success':
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

            # Notify caller (e.g. CLI progress display)
            self._invoke_on_phase_complete(phase_id, result)

            phase_state = result.get('state', 'unknown')
            logger.info(
                f"Pipeline {self.template.id}: phase '{phase_id}' completed "
                f"(state={phase_state})"
            )

            # Stop pipeline on phase failure — don't feed errors downstream
            if phase_state in ('failed', 'permanently_failed'):
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

    def _execute_wave_parallel(
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

        def _run_phase(phase_id: str) -> dict:
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

            # Build prompt — read phase_outputs under lock to avoid races
            with self._phase_outputs_lock:
                phase_input = self._build_phase_input(phase, initial_input)

            preferred_model = self._resolve_model_tier(phase.model_tier)

            task = TaskSpec(
                type=self._resolve_task_type(phase.task_type),
                payload={
                    "prompt": phase_input,
                    "phase_id": phase.id,
                    "pipeline_id": self.template.id,
                },
                priority=Priority.HIGH,
                preferred_model=preferred_model,
                timeout_seconds=phase.timeout_minutes * 60,
            )

            task_id = self.runner.queue.submit_task(task)
            logger.info(
                f"Pipeline {self.template.id}: submitted phase '{phase_id}' "
                f"(task_id={task_id}, parallel=True)"
            )

            result = self._execute_and_wait(task_id, phase, initial_input=initial_input)

            # Write FILE blocks to disk if phase requests it (#189)
            if phase.write_files:
                self._handle_file_write(phase, result)

            # Write result under lock — prevents lost updates on shared dict
            with self._phase_outputs_lock:
                self.phase_outputs[phase_id] = result

            # Supervisor hook (#194) — parallel path
            if getattr(phase, 'supervisor', False) and result.get('state') == 'success':
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
                except Exception as exc:
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
                        for other_fut, other_pid in future_to_phase.items():
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
            except Exception:
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
            except Exception:
                pass  # Never let a callback crash the pipeline

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
                logger.debug(
                    f"Phase '{phase.id}': dry-run — no FILE blocks found in output"
                )
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
                        "supervisor_reason": (
                            f"max_retries ({max_retries}) exhausted: {reason}"
                        ),
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
                    remainder = stripped[len(verdict):].lstrip(":").strip()
                    return verdict, remainder
        logger.warning(
            f"Supervisor response had no APPROVE/REVISE/ABORT verdict; "
            f"defaulting to APPROVE. Response preview: {text[:200]!r}"
        )
        return "APPROVE", "no verdict found — defaulting to APPROVE"

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

    def _build_phase_input(self, phase: PhaseDefinition, initial_input: dict, failure_context: str = "") -> str:
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

        # Wrap dicts in a safe mapping that returns a placeholder for missing keys
        safe_input = _SafeDict(initial_input)
        safe_config = _SafeDict(self.config)

        # ── fix/243: smart previous_output proxy ──────────────────────────────
        # When output_dir is set, {previous_output} emits compact file-path
        # summaries instead of dumping all content inline.
        # {previous_output_inline} always gives the full raw content regardless.
        _output_dir_for_proxy = str(self.output_dir) if self.output_dir else None
        previous_output_proxy = _PreviousOutputProxy(
            self.phase_outputs, _output_dir_for_proxy, self._phase_map
        )
        previous_output_inline_proxy = _PreviousOutputInlineProxy(self.phase_outputs)

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
                except Exception as exc:
                    logger.warning(
                        f"Phase '{phase.id}': failed to load skill_ref '{skill_ref}' — {exc}"
                    )
                    skill_context[Path(skill_ref).stem] = f"<SKILL_LOAD_ERROR:{skill_ref}>"

        safe_skills = _SafeDict(skill_context)

        # Load context_files and build file_context dict
        file_context: Dict[str, str] = {}
        if hasattr(phase, 'context_files') and phase.context_files:
            for file_path in phase.context_files:
                try:
                    p = Path(file_path).expanduser()
                    if p.exists():
                        content = p.read_text(errors="replace")
                        # Use filename (without extension) as key
                        key = p.stem.replace("-", "_").replace(".", "_")
                        file_context[key] = content
                        logger.debug(f"Phase '{phase.id}': loaded context file '{file_path}' ({len(content)} chars)")
                    else:
                        logger.warning(f"Phase '{phase.id}': context file not found: {file_path}")
                        file_context[p.stem] = f"<FILE_NOT_FOUND:{file_path}>"
                except Exception as exc:
                    logger.warning(f"Phase '{phase.id}': failed to read context file '{file_path}' — {exc}")
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
                    except Exception as exc:
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
            summary_lines.append(f"- {phase_name} ({pid}): completed, ~{word_count} words → {output_dir_str}/{pid.replace('-', '_')}.md")
        phase_summary = "\n".join(summary_lines) if summary_lines else "This is the first phase — no prior work."

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
                **phase_kwargs,
            )
        except (KeyError, IndexError, AttributeError) as exc:
            logger.warning(
                f"Phase '{phase.id}': format error in prompt template — {exc}. "
                f"Returning raw template."
            )
            prompt = phase.prompt_template

        return prompt

    @staticmethod
    def _load_skill(skill_ref: str, template_dir: Optional[Path] = None) -> Tuple[str, str]:
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
                except Exception:
                    frontmatter_data = {}

        # Skill name: prefer frontmatter 'name:', else filename stem
        skill_name: str = (
            str(frontmatter_data.get("name", "")).strip()
            or resolved_real.stem
        )

        return skill_name, body.strip()

    def _execute_and_wait(self, task_id: str, phase: PhaseDefinition, initial_input: Optional[dict] = None) -> dict:
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
            raise RuntimeError(
                f"Phase '{phase.id}': task {task_id} not found in queue"
            )

        # Find the first executor that can handle this task type
        executor = None
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
            except Exception as exc:
                last_error_msg = str(exc)
                logger.warning(
                    f"Phase '{phase.id}': attempt {attempt}/{total_attempts} "
                    f"raised exception — {exc}"
                )
                sanitized = self._sanitize_error_for_prompt(last_error_msg)
                attempt_history.append({
                    "attempt": attempt,
                    "error": sanitized,
                    "partial_output": "",
                    "tokens_used": 0,
                })
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
            attempt_history.append({
                "attempt": attempt,
                "error": sanitized_err,
                "partial_output": partial_output,  # full output in history
                "tokens_used": tokens_used,
            })

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
        ansi_escape = re.compile(r'\x1b\[[0-9;]*[mGKHFJsr]')
        error = ansi_escape.sub('', error)

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
            if line.strip().startswith('Traceback (most recent call last):'):
                in_traceback = True
                continue
            if in_traceback:
                # Traceback body: lines indented with spaces or tabs
                if line.startswith('  ') or line.startswith('\t'):
                    continue
                else:
                    # Non-indented line → the exception class/message line
                    in_traceback = False
                    filtered.append(line)
            else:
                filtered.append(line)

        error = '\n'.join(filtered).strip()

        # 3. Truncate to 500 chars
        if len(error) > 500:
            error = error[:497] + '...'

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

    @staticmethod
    def _resolve_task_type(task_type_str: str) -> TaskType:
        """Map a string task type to a TaskType enum, defaulting to CONTENT."""
        try:
            return TaskType(task_type_str.lower())
        except ValueError:
            logger.warning(
                f"Unknown task_type '{task_type_str}'; defaulting to 'content'"
            )
            return TaskType.CONTENT

    @staticmethod
    def _resolve_model_tier(model_tier_str: str):
        """Map a friendly model tier name to a ModelTier enum value.

        The PhaseDefinition uses short names (haiku, sonnet, opus) while
        the schema uses versioned names (haiku-4-5, sonnet-4, opus-4-6).
        Returns None if the tier is not recognised (runner will use its default).
        """
        from .schemas import ModelTier

        _MAP = {
            "haiku": ModelTier.HAIKU,
            "sonnet": ModelTier.SONNET,
            "opus": ModelTier.OPUS,
            # allow full enum values too
            "haiku-4-5": ModelTier.HAIKU,
            "sonnet-4": ModelTier.SONNET,
            "opus-4-6": ModelTier.OPUS,
        }
        resolved = _MAP.get(model_tier_str.lower() if model_tier_str else "")
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


class _SafeDict(dict):
    """A dict subclass that returns a placeholder string for missing keys.

    This prevents ``str.format()`` calls from raising ``KeyError`` when the
    template references a phase output that has not yet been produced (e.g.
    due to template authoring errors).
    """

    def __missing__(self, key: str) -> str:
        logger.debug(f"Template referenced missing key: '{key}'")
        return f"<MISSING:{key}>"

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            logger.debug(f"Template referenced missing attribute: '{key}'")
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

    def __init__(self, phase_outputs: dict, output_dir: Optional[str], phase_map: dict) -> None:
        self._phase_outputs = phase_outputs
        self._output_dir = output_dir
        self._phase_map = phase_map

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
        return f"_PreviousOutputProxy(output_dir={self._output_dir!r}, phases={list(self._phase_outputs.keys())})"


class _PreviousOutputInlineProxy:
    """Proxy for ``{previous_output_inline}`` — always returns full inline content.

    Preserves the pre-fix/243 ``{previous_output}`` behaviour for templates
    that explicitly require every prior phase output dumped inline.  The full
    raw ``phase_outputs`` dict repr is returned as a string, regardless of
    whether *output_dir* is configured.
    """

    def __init__(self, phase_outputs: dict) -> None:
        self._phase_outputs = phase_outputs

    def __format__(self, format_spec: str) -> str:
        return format(str(self._phase_outputs), format_spec)

    def __str__(self) -> str:
        return str(self._phase_outputs)

    def __getitem__(self, key: str) -> str:
        if key not in self._phase_outputs:
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
        super().__init__(*args, **kwargs)
        self.iteration_history: Dict[str, List[dict]] = defaultdict(list)
        """Per-phase list of prior results (oldest first).  Current result is in
        ``phase_outputs``.  Reset on each :meth:`execute` call."""
        self.iteration_counts: Dict[str, int] = defaultdict(int)
        """Total execution count per phase.  Reset on each :meth:`execute` call."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, initial_input: dict) -> dict:
        """Execute the pipeline following state-machine transitions.

        Starts at the first phase in ``template.phases``, resolves each
        transition after completion, and halts when:

        * A phase has no matching transition for its outcome (terminal state).
        * A loop phase (``max_iterations > 0``) exceeds its iteration limit —
          returns an abort dict with ``abort_reason = "MAX_ITERATIONS_EXCEEDED"``.
        * A non-loop phase (``max_iterations == 0``) would be revisited — logs
          a WARNING and stops (legacy cycle guard).

        Args:
            initial_input: Pipeline input dict (e.g. article brief).

        Returns:
            Dict with keys:

            - ``phase_outputs``:     mapping of phase_id → latest result dict
            - ``final_output``:      result dict of the last executed phase
            - ``iteration_history``: mapping of phase_id → list of prior results
              (present only when execution completes normally or via cycle guard;
              also included in MAX_ITERATIONS_EXCEEDED abort dicts)
            - ``iteration_counts``:  mapping of phase_id → total execution count
        """
        if not self.template.phases:
            logger.warning(
                f"StateMachineSequencer: template '{self.template.id}' has no "
                f"phases — returning empty result."
            )
            return {"phase_outputs": {}, "final_output": {}}

        # ── Reset per-execution tracking (supports re-use of the sequencer) ───
        self.iteration_history = defaultdict(list)
        self.iteration_counts = defaultdict(int)

        # ── Pipeline-start hook (e.g. git branch creation) ────────────────────
        if self.on_pipeline_start is not None:
            try:
                self.on_pipeline_start(self.pipeline_context)
            except Exception as exc:
                logger.error(
                    f"Pipeline {self.template.id}: on_pipeline_start hook failed: {exc}"
                )
                raise

        # Entry point: first phase in template order
        current_phase_id: Optional[str] = self.template.phases[0].id

        # executed_sequence tracks phases in execution order (may contain repeats
        # for loop phases).  Used for final_output determination and logging.
        executed_sequence: List[str] = []

        final_result: dict = {}

        try:
            while current_phase_id is not None:
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
                                self.phase_outputs.get(last_phase, {})
                                if last_phase else {}
                            ),
                            "iteration_history": dict(self.iteration_history),
                            "iteration_counts": dict(self.iteration_counts),
                            "aborted": True,
                            "abort_reason": "MAX_ITERATIONS_EXCEEDED",
                            "exceeded_phase": current_phase_id,
                        }
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
                self._invoke_on_phase_start(
                    current_phase_id, phase, len(executed_sequence) - 1
                )

                # ── Build prompt and submit task ──────────────────────────────
                phase_input = self._build_phase_input(phase, initial_input)
                preferred_model = self._resolve_model_tier(phase.model_tier)

                task = TaskSpec(
                    type=self._resolve_task_type(phase.task_type),
                    payload={
                        "prompt": phase_input,
                        "phase_id": phase.id,
                        "pipeline_id": self.template.id,
                    },
                    priority=Priority.HIGH,
                    preferred_model=preferred_model,
                    timeout_seconds=phase.timeout_minutes * 60,
                )

                task_id = self.runner.queue.submit_task(task)
                logger.info(
                    f"Pipeline {self.template.id}: submitted phase "
                    f"'{current_phase_id}' "
                    f"(task_id={task_id}, iteration={current_iter})"
                )

                # ── Execute and wait (with retry logic from parent) ───────────
                result = self._execute_and_wait(
                    task_id, phase, initial_input=initial_input
                )

                # ── Write FILE blocks if requested (#189) ─────────────────────
                if phase.write_files:
                    self._handle_file_write(phase, result)

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
            final_output = (
                self.phase_outputs.get(last_phase_id, {}) if last_phase_id else {}
            )
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

    def _resolve_next_phase(
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

        # ── Content-based routing (opt-in via verdict keys in transitions) ───
        # Only attempt verdict extraction when at least one verdict keyword
        # appears as a transition key — this keeps the common path fast and
        # avoids any text-parsing overhead for non-review phases.
        _verdict_keys = {"approve", "request_changes", "abort"}
        if result is not None and _verdict_keys.intersection(effective):
            output_text: str = ""
            raw_result = result.get("result", {})
            if isinstance(raw_result, dict):
                output_text = raw_result.get("text", "") or ""
                # Fallback: OpenClaw executor stores output in partial_output
                if not output_text:
                    output_text = raw_result.get("partial_output", "") or ""
            if not output_text:
                output_text = result.get("text", "") or ""

            verdict = extract_verdict(output_text)
            if verdict is not None and verdict in effective:
                logger.debug(
                    f"Pipeline {self.template.id}: phase '{phase.id}' "
                    f"content-routed via verdict '{verdict}'"
                )
                return effective[verdict]

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
        except Exception as hook_exc:
            logger.warning(
                f"Pipeline {pipeline_id}: on_pipeline_complete hook failed: {hook_exc}"
            )
