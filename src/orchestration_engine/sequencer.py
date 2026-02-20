"""Phase sequencer — executes pipeline phases in order, passing outputs forward."""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from .schemas import Priority, TaskResult, TaskSpec, TaskState, TaskType
from .templates import PhaseDefinition, PipelineTemplate, TemplateEngine

logger = logging.getLogger(__name__)


class PhaseSequencer:
    """Executes a pipeline template phase by phase.

    Uses synchronous, sequential execution for MVP.  All phase outputs are
    accumulated in ``self.phase_outputs`` and forwarded to downstream phases
    via the prompt template formatting mechanism.
    """

    def __init__(self, template: PipelineTemplate, runner, config: dict = None) -> None:
        """Initialise the sequencer.

        Args:
            template: The pipeline template to execute.
            runner:   A TaskRunner instance (must have ``.queue`` and ``.executors``).
            config:   Optional pipeline-level configuration dict (passed to templates).
        """
        self.template = template
        self.runner = runner
        self.config = config or {}
        self.phase_outputs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, initial_input: dict) -> dict:
        """Execute the full pipeline.

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

        for wave in execution_order:
            # MVP: sequential within each wave (no actual parallelism)
            for phase_id in wave:
                phase = self._get_phase(phase_id)

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
                self.phase_outputs[phase_id] = result

                logger.info(
                    f"Pipeline {self.template.id}: phase '{phase_id}' completed "
                    f"(state={result.get('state', 'unknown')})"
                )

        # Determine the final output (last phase of the last wave)
        last_phase_id = execution_order[-1][-1]
        final_output = self.phase_outputs.get(last_phase_id, {})

        return {
            "phase_outputs": self.phase_outputs,
            "final_output": final_output,
        }

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

        Missing keys produce a ``<MISSING:key>`` placeholder (via SafeDict)
        rather than raising ``KeyError``.
        """
        if not phase.prompt_template:
            return ""

        # Wrap dicts in a safe mapping that returns a placeholder for missing keys
        safe_input = _SafeDict(initial_input)
        safe_outputs = _SafeDict(self.phase_outputs)

        try:
            prompt = phase.prompt_template.format(
                input=safe_input,
                previous_output=safe_outputs,
                config=self.config,
            )
        except (KeyError, IndexError, AttributeError) as exc:
            logger.warning(
                f"Phase '{phase.id}': format error in prompt template — {exc}. "
                f"Returning raw template."
            )
            prompt = phase.prompt_template

        return prompt

    def _execute_and_wait(self, task_id: str, phase: PhaseDefinition) -> dict:
        """Execute a queued task synchronously and return its result as a dict.

        Retrieves the TaskSpec from the queue, runs it through the runner's
        first available executor, marks the task complete (or failed) in the
        queue, and returns a plain dict representation of the result.

        Args:
            task_id: ID of the task previously submitted to the runner queue.
            phase:   The PhaseDefinition (used for logging / context).

        Returns:
            Dict with at least ``state``, ``result``, ``confidence`` keys.
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

        # Execute synchronously (blocking)
        result: TaskResult = executor.execute(
            task_spec,
            worker_id="sequencer-worker",
            model_tier=phase.model_tier,
            thinking_level=phase.thinking_level,
        )

        # Persist result in queue
        if result.state == TaskState.SUCCESS:
            self.runner.queue.complete_task(task_id, result)
        else:
            error_msg = "Phase execution failed"
            if result.errors:
                first = result.errors[0]
                error_msg = (
                    first.get("message", error_msg)
                    if isinstance(first, dict)
                    else getattr(first, "message", error_msg)
                )
            self.runner.queue.fail_task(task_id, error_msg)

        # Return a serialisable dict for downstream phase templates
        try:
            return result.model_dump()
        except AttributeError:
            return result.dict()  # Pydantic v1 fallback

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


class _SafeDict(dict):
    """A dict subclass that returns a placeholder string for missing keys.

    This prevents ``str.format()`` calls from raising ``KeyError`` when the
    template references a phase output that has not yet been produced (e.g.
    due to template authoring errors).
    """

    def __missing__(self, key: str) -> str:
        logger.debug(f"Template referenced missing key: '{key}'")
        return f"<MISSING:{key}>"
