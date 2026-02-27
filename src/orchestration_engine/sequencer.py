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

from .schemas import Priority, TaskError, TaskResult, TaskSpec, TaskState, TaskType
from .templates import PhaseDefinition, PipelineTemplate, TemplateEngine

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

    def __init__(self, template: PipelineTemplate, runner, config: dict = None,
                 on_phase_complete=None, on_phase_start=None,
                 on_pipeline_start=None, on_pipeline_complete=None) -> None:
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

        # Thread-safety locks (Issue #102)
        self._phase_outputs_lock: threading.RLock = threading.RLock()
        """Protects ``phase_outputs`` during concurrent wave execution."""
        self._callback_lock: threading.RLock = threading.RLock()
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
            phase = self._get_phase(phase_id)

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
            result = self._execute_and_wait(task_id, phase)
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

            phase = self._get_phase(phase_id)

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

            result = self._execute_and_wait(task_id, phase)

            # Write result under lock — prevents lost updates on shared dict
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
                        # Signal remaining queued (not yet running) workers to skip
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

    def _build_phase_input(self, phase: PhaseDefinition, initial_input: dict) -> str:
        """Build the prompt string for a phase.

        Uses Python's ``str.format()`` to interpolate:
        - ``{input}``           — the initial pipeline input dict
        - ``{input[key]}``      — a specific key from the initial input
        - ``{previous_output}`` — all accumulated phase outputs so far
        - ``{previous_output[phase_id]}`` — output of a specific previous phase
        - ``{config}``          — the pipeline config dict
        - ``{skill_context[name]}`` — content of a loaded skill file (from skill_refs)

        Missing keys produce a ``<MISSING:key>`` placeholder (via SafeDict)
        rather than raising ``KeyError``.

        .. note::
            When called from a parallel worker, **the caller must hold
            ``self._phase_outputs_lock``** before invoking this method so that
            the snapshot of ``self.phase_outputs`` is consistent.
        """
        if not phase.prompt_template:
            return ""

        # Wrap dicts in a safe mapping that returns a placeholder for missing keys
        safe_input = _SafeDict(initial_input)
        safe_outputs = _SafeDict(self.phase_outputs)
        safe_config = _SafeDict(self.config)

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
        phase_kwargs: Dict[str, _PhaseOutput] = {
            pid: _PhaseOutput(_extract_phase_text(pout))
            for pid, pout in self.phase_outputs.items()
        }

        # Wrap pipeline_context so {context.key} works in prompt templates
        safe_context = _SafeDict(self.pipeline_context)

        try:
            prompt = phase.prompt_template.format(
                input=safe_input,
                previous_output=safe_outputs,
                config=safe_config,
                skill_context=safe_skills,
                files=safe_files,
                context=safe_context,
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

    def _execute_and_wait(self, task_id: str, phase: PhaseDefinition) -> dict:
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

        for attempt in range(1, total_attempts + 1):
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
                if attempt < total_attempts:
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

            # Sleep between attempts, but NOT after the final one
            if attempt < total_attempts:
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

        try:
            return last_result.model_dump()
        except AttributeError:
            return last_result.dict()  # Pydantic v1 fallback

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

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
