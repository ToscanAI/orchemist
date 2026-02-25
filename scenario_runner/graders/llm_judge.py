"""LLM Judge grader — sends article + rubric to a judge model, parses score.

Holdout principle: the judge receives ONLY the article text and the rubric.
It does NOT receive scenario metadata, threshold, pipeline config, or any
other context. This prevents the judge from gaming the score.

Supports three routing modes (evaluated in priority order):
1. **dry-run** — ``ORCH_DRY_RUN=1`` env var → return a configurable stub score,
   no API call made.
2. **executor** — an executor object (e.g. ``OpenClawExecutor`` or
   ``AnthropicExecutor``) is provided → route the judge prompt through
   ``executor.execute()``.  This allows the OpenClaw subscription token to
   be used for judge scoring without a raw API key.
3. **api-key** — an Anthropic API key is available → make a raw ``urllib``
   call to ``api.anthropic.com/v1/messages`` directly.

Uses urllib.request as the fallback when calling the Anthropic API directly —
no third-party SDK required.
"""

import json
import os
import re
import urllib.request
import urllib.error
from typing import Any, Optional

from ..models import GradeResult

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 1024
_REQUEST_TIMEOUT = 60  # seconds

# Regex to extract "Score: 0.85" (or "Score: 1" etc.) from judge response
_SCORE_RE = re.compile(r"Score:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)

# System prompt sent via the TaskSpec payload when routing through an executor.
# This ensures the judge model behaves consistently regardless of any default
# system prompt that the executor's sub-agent session may apply.
_JUDGE_SYSTEM_PROMPT = (
    "You are a rubric-based grading assistant. "
    "Evaluate the provided article against the given rubric criteria. "
    "Your response MUST end with a score line in exactly this format:\n"
    "Score: X.X\n"
    "where X.X is a decimal number between 0.0 and 1.0 (e.g. Score: 0.75). "
    "Do not omit the Score line — it is required for automated parsing."
)


class LLMJudgeGrader:
    """Grades pipeline output using an LLM-based rubric judge.

    The grader enforces the holdout principle: the judge model only
    ever sees the article text and the rubric text — nothing else.

    Routing priority
    ----------------
    1. ``ORCH_DRY_RUN=1`` — return stub score immediately (no API call).
    2. *executor* is set — route through ``executor.execute()``; supports
       OpenClaw gateway subscription token and AnthropicExecutor.
    3. *api_key* is set — call ``api.anthropic.com`` directly with urllib.
    4. Neither — return ``GradeResult(passed=False, score=0.0)``.

    Dry-run mode
    ------------
    When the environment variable ``ORCH_DRY_RUN=1`` is set at the time
    :meth:`grade` is called, no API request is made and a configurable
    stub score is returned instead (default ``0.8``).  This enables
    deterministic CI runs without an API key.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        dry_run_stub_score: float = 0.8,
        executor: Optional[Any] = None,
    ):
        """Initialise the grader.

        Parameters
        ----------
        api_key:
            Anthropic API key.  If ``None`` and *executor* is also ``None``,
            falls back to the ``ANTHROPIC_API_KEY`` environment variable.
            When *executor* is provided this parameter (and the env var) are
            intentionally ignored so that executor failures surface as explicit
            ``GradeResult(score=0.0)`` rather than silently retrying via
            the urllib path.
        dry_run_stub_score:
            Score to return when ``ORCH_DRY_RUN=1`` env var is set.
            Must be in ``[0.0, 1.0]``.  Defaults to ``0.8``.
        executor:
            Optional executor object (e.g. ``OpenClawExecutor`` or
            ``AnthropicExecutor``).  When provided, the judge prompt is
            routed through ``executor.execute()`` instead of a direct
            ``urllib`` call.  This allows the OpenClaw subscription token
            to be used for judge scoring.  Typed as ``Any`` to avoid
            importing ``orchestration_engine.schemas`` at module load time
            (which would create a circular dependency when ``cli.py``
            imports both packages).
        """
        # Only fall back to the environment variable when no executor is
        # provided.  When an executor is present it takes priority over the
        # api-key path — storing a key here would create a confusing silent
        # fallback if the executor fails (executor failures already surface as
        # GradeResult(score=0.0) and should not silently retry via urllib).
        if executor is None:
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        else:
            self.api_key = None
        self.dry_run_stub_score = max(0.0, min(1.0, dry_run_stub_score))
        self.executor = executor

    def grade(
        self,
        output: dict,
        rubric: str,
        judge_model: str,
        output_field: Optional[str] = None,
    ) -> GradeResult:
        """Grade *output* against *rubric* using *judge_model*.

        Holdout guarantee: only the extracted output text and *rubric* are
        sent to the model.  Threshold, scenario ID, pipeline config, and
        all other metadata are intentionally withheld.

        Parameters
        ----------
        output:
            The pipeline output dict.  Text extraction follows this priority:

            1. ``output[output_field]`` — if *output_field* is given.
            2. ``output["article"]`` — backward-compat with article pipelines.
            3. ``output["text"]`` / ``output["content"]`` — common text keys.
            4. ``output["final"]`` — present when output comes from the CLI
               (structured as ``{"final": <last-phase-dict>, "phases": {...}}``).
            5. Full JSON serialization of *output* — always produces non-empty
               content so the judge never evaluates an empty string.

        output_field:
            Optional explicit key to extract from *output* before falling
            back to the priority-order search above.

        Returns:
            GradeResult with score 0.0–1.0 and full judge reasoning as
            details.  ``passed`` is set to True when score >= 0.5; the
            runner will override this using the per-criterion threshold.
        """
        # --- Dry-run short-circuit ---
        if os.environ.get("ORCH_DRY_RUN") == "1":
            stub = self.dry_run_stub_score
            return GradeResult(
                passed=stub >= 0.5,
                score=stub,
                details=(
                    f"[dry-run stub] ORCH_DRY_RUN=1 detected — "
                    f"returning stub score {stub:.2f} (no API call made)."
                ),
                grader_type="llm_judge",
            )

        # --- Holdout: only extracted output text + rubric reach the model ---
        #
        # Priority-order text extraction:
        #  1. explicit output_field (if provided)
        #  2. "article"  — backward compat for article-generating pipelines
        #  3. "text" / "content"  — other common single-field outputs
        #  4. "final"  — CLI-structured output {"final": <dict>, "phases": {...}}
        #  5. full JSON of the output dict  — ensures the judge always gets
        #     something meaningful; never sends an empty string.
        if output_field is not None:
            raw = output.get(output_field)
            extracted_text = str(raw) if raw is not None else ""
        else:
            extracted_text = (
                output.get("output")
                or output.get("result")
                or output.get("article")
                or output.get("text")
                or output.get("content")
                or None
            )
            if not extracted_text:
                # Try "final" sub-dict (CLI-structured output)
                final_sub = output.get("final")
                if final_sub and isinstance(final_sub, dict):
                    extracted_text = json.dumps(final_sub, default=str, indent=2)
                else:
                    # Last resort: serialise the full output dict
                    extracted_text = json.dumps(output, default=str, indent=2)

        # --- Routing: executor → api_key → error ---
        if self.executor is not None:
            return self._grade_with_executor(extracted_text, rubric, judge_model)

        if not self.api_key:
            return GradeResult(
                passed=False,
                score=0.0,
                details="No API key configured",
                grader_type="llm_judge",
            )

        # --- Direct urllib path (existing behaviour) ---
        return self._grade_with_api_key(extracted_text, rubric, judge_model)

    # ------------------------------------------------------------------
    # Private: executor routing (new)
    # ------------------------------------------------------------------

    def _grade_with_executor(
        self,
        extracted_text: str,
        rubric: str,
        judge_model: str,
    ) -> GradeResult:
        """Route the judge prompt through ``self.executor.execute()``.

        Builds a ``TaskSpec`` with the judge prompt and an explicit system
        prompt (``_JUDGE_SYSTEM_PROMPT``), calls the executor, extracts the
        response text, and parses the score.  The system prompt ensures the
        model produces a ``Score: X.X`` line regardless of any default system
        prompt the executor's sub-agent session may apply.

        The import of ``orchestration_engine.schemas`` is deferred to call
        time to avoid a circular import at module load (``cli.py`` imports
        both ``scenario_runner`` and ``orchestration_engine``).

        Parameters
        ----------
        extracted_text:
            The pipeline output text (post holdout extraction).
        rubric:
            The rubric text.
        judge_model:
            Requested judge model name hint (used for ModelTier mapping).

        Returns
        -------
        GradeResult
        """
        try:
            from orchestration_engine.schemas import (  # noqa: PLC0415
                ModelTier,
                TaskSpec,
                TaskState,
                TaskType,
            )
        except ImportError as exc:
            return GradeResult(
                passed=False,
                score=0.0,
                details=(
                    f"Executor routing unavailable — could not import "
                    f"orchestration_engine.schemas: {exc}"
                ),
                grader_type="llm_judge",
            )

        user_message = (
            f"## Rubric\n\n{rubric}\n\n"
            f"## Article to Evaluate\n\n{extracted_text}"
        )

        # Best-effort ModelTier mapping from the judge_model hint string.
        # Falls back to HAIKU for any unrecognised model name.
        model_hint = judge_model.lower()
        if "opus" in model_hint:
            tier = ModelTier.OPUS
        elif "sonnet" in model_hint:
            tier = ModelTier.SONNET
        else:
            tier = ModelTier.HAIKU

        task = TaskSpec(
            type=TaskType.ANALYSIS,
            payload={
                "prompt": user_message,
                # Explicit system prompt ensures the judge responds with the
                # required "Score: X.X" format regardless of any default system
                # prompt the executor's sub-agent session may apply.
                "system": _JUDGE_SYSTEM_PROMPT,
            },
            preferred_model=tier,
            created_by="llm_judge_grader",
        )

        # Execute via the provided executor
        try:
            task_result = self.executor.execute(task)
        except Exception as exc:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Executor error: {type(exc).__name__}: {exc}",
                grader_type="llm_judge",
            )

        # Verify the executor returned a success state
        result_state = getattr(task_result, "state", None)
        if result_state is not None and result_state != TaskState.SUCCESS:
            errors = getattr(task_result, "errors", []) or []
            error_msg = "; ".join(
                e.message if hasattr(e, "message") else str(e)
                for e in errors
            ) if errors else str(result_state)
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Executor returned non-success state ({result_state}): {error_msg}",
                grader_type="llm_judge",
            )

        # Extract response text from TaskResult.result dict
        result_data = getattr(task_result, "result", {}) or {}
        if isinstance(result_data, dict):
            response_text = result_data.get("text", "") or ""
        elif isinstance(result_data, str):
            response_text = result_data
        else:
            response_text = ""

        if not response_text:
            return GradeResult(
                passed=False,
                score=0.0,
                details="Executor returned an empty response",
                grader_type="llm_judge",
            )

        # Parse "Score: X.X" from the judge response
        match = _SCORE_RE.search(response_text)
        if match:
            score = max(0.0, min(1.0, float(match.group(1))))
        else:
            score = 0.0
            response_text = f"[No score found in response]\n{response_text}"

        return GradeResult(
            passed=score >= 0.5,
            score=score,
            details=response_text[:1000],
            grader_type="llm_judge",
        )

    # ------------------------------------------------------------------
    # Private: direct API key path (existing behaviour, extracted to method)
    # ------------------------------------------------------------------

    def _grade_with_api_key(
        self,
        extracted_text: str,
        rubric: str,
        judge_model: str,
    ) -> GradeResult:
        """Call the Anthropic Messages API directly via urllib.

        This is the original implementation, preserved intact and extracted
        into a private method so the routing logic in :meth:`grade` stays
        clean.

        Parameters
        ----------
        extracted_text:
            The pipeline output text (post holdout extraction).
        rubric:
            The rubric text.
        judge_model:
            Anthropic model ID to use as the judge.

        Returns
        -------
        GradeResult
        """
        user_message = (
            f"## Rubric\n\n{rubric}\n\n"
            f"## Article to Evaluate\n\n{extracted_text}"
        )

        payload = {
            "model": judge_model,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": user_message}],
        }

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }

        try:
            request = urllib.request.Request(
                _API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            response_text = body["content"][0]["text"]

        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"HTTP {exc.code}: {error_body[:300]}",
                grader_type="llm_judge",
            )
        except Exception as exc:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"API error: {type(exc).__name__}: {exc}",
                grader_type="llm_judge",
            )

        # Parse "Score: X.X" from judge response
        match = _SCORE_RE.search(response_text)
        if match:
            score = float(match.group(1))
            score = max(0.0, min(1.0, score))  # clamp to [0, 1]
        else:
            # No parseable score — default to 0.0
            score = 0.0
            response_text = f"[No score found in response]\n{response_text}"

        # passed=True when score >= 0.5; runner overrides with criterion threshold
        return GradeResult(
            passed=score >= 0.5,
            score=score,
            details=response_text[:1000],
            grader_type="llm_judge",
        )
