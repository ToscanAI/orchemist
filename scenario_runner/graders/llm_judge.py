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
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialise with an Anthropic API key.

        Falls back to the ANTHROPIC_API_KEY environment variable when
        *api_key* is not provided.
        """
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def grade(
        self,
        output: dict,
        rubric: str,
        judge_model: str,
    ) -> GradeResult:
        """Grade *output* against *rubric* using *judge_model*.

        Holdout guarantee: only ``output['article']`` and ``rubric`` are
        sent to the model.  Threshold, scenario ID, pipeline config, and
        all other metadata are intentionally withheld.

        Returns:
            GradeResult with score 0.0–1.0 and full judge reasoning as
            details.  ``passed`` is set to True when score >= 0.5; the
            runner will override this using the per-criterion threshold.
        """
        if not self.api_key:
            return GradeResult(
                passed=False,
                score=0.0,
                details="No API key configured",
                grader_type="llm_judge",
            )

        # --- Holdout: only article text + rubric reach the model ---
        article_text = output.get("article", "")

        user_message = (
            f"## Rubric\n\n{rubric}\n\n"
            f"## Article to Evaluate\n\n{article_text}"
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
