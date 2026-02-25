"""LLM Judge grader — sends article + rubric to a judge model, parses score.

Holdout principle: the judge receives ONLY the article text and the rubric.
It does NOT receive scenario metadata, threshold, pipeline config, or any
other context. This prevents the judge from gaming the score.

Uses urllib.request to call the Anthropic Messages API directly —
no third-party SDK required.
"""

import json
import os
import re
import urllib.request
import urllib.error
from typing import Optional

from ..models import GradeResult

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 1024
_REQUEST_TIMEOUT = 60  # seconds

# Regex to extract "Score: 0.85" (or "Score: 1" etc.) from judge response
_SCORE_RE = re.compile(r"Score:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


class LLMJudgeGrader:
    """Grades pipeline output using an LLM-based rubric judge.

    The grader enforces the holdout principle: the judge model only
    ever sees the article text and the rubric text — nothing else.

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
    ):
        """Initialise with an Anthropic API key.

        Falls back to the ANTHROPIC_API_KEY environment variable when
        *api_key* is not provided.

        Parameters
        ----------
        api_key:
            Anthropic API key.  If ``None``, falls back to the
            ``ANTHROPIC_API_KEY`` environment variable.
        dry_run_stub_score:
            Score to return when ``ORCH_DRY_RUN=1`` env var is set.
            Must be in ``[0.0, 1.0]``.  Defaults to ``0.8``.
        """
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.dry_run_stub_score = max(0.0, min(1.0, dry_run_stub_score))

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

        if not self.api_key:
            return GradeResult(
                passed=False,
                score=0.0,
                details="No API key configured",
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
