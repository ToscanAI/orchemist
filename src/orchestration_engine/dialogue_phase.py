"""Dialogue phase — cross-model adversarial review at the phase boundary (Issue #677).

A *dialogue* phase orchestrates an alternating drafter ↔ reviewer conversation
across two (typically different-provider) model executors.  The drafter
produces an artefact; the reviewer either approves it or requests changes; the
drafter incorporates the feedback and produces a new version.  The loop runs
for up to ``max_rounds`` rounds, terminating early on a convergence signal
(default: ``APPROVED``) emitted by the reviewer.

This is the Track B prototype of the Orchemist pivot: the genuine wedge of
"cross-model adversarial review at the phase boundary".  It is intentionally
kept narrow — no refactor of the generic adversary system (#700), no daemon /
queue / GitHub integration.

Public API
----------
* :class:`DialoguePhaseConfig`  — Pydantic config parsed from ``type: dialogue`` YAML.
* :class:`DialogueParticipant`  — Pydantic sub-config for drafter or reviewer.
* :class:`DialogueRound`        — Result of a single round.
* :class:`DialogueResult`       — Full transcript + cost summary.
* :class:`DialogueRunner`       — Orchestrates the round loop.
* :func:`run_dialogue`          — Convenience wrapper around :class:`DialogueRunner`.

Cost tracking honours :issue:`#801` — we use ``TaskResult.cost_usd`` (which
already reflects ``usage.total_cost`` from the OpenRouter response when
present) and never apply a $10/Mtok fallback inflator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .schemas import TaskResult, TaskSpec, TaskState, TaskType
from .verdict_parser import extract_verdict

logger = logging.getLogger(__name__)

__all__ = [
    "DialogueParticipant",
    "DialoguePhaseConfig",
    "DialogueRound",
    "DialogueResult",
    "DialogueRunner",
    "run_dialogue",
    "DEFAULT_CONVERGENCE_SIGNAL",
    "DEFAULT_MAX_ROUNDS",
    "DRIFT_SIMILARITY_THRESHOLD",
]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_ROUNDS: int = 4
"""Default maximum number of drafter ↔ reviewer rounds before bail-out."""

DEFAULT_CONVERGENCE_SIGNAL: str = "APPROVED"
"""Default sentinel keyword the reviewer must emit to terminate the loop."""

DRIFT_SIMILARITY_THRESHOLD: float = 0.95
"""Jaccard similarity above which two consecutive drafts are flagged as drifted.

Spec #677 acceptance criterion 6: if two consecutive draft rounds differ by
<5% (i.e. similarity >0.95) we warn ``convergence_stall``.  Threshold is
chosen conservatively — real spec drift typically shows ~0.6–0.85 similarity
between rounds.
"""


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class DialogueParticipant(BaseModel):
    """Configuration for one side of a dialogue (drafter OR reviewer).

    Attributes
    ----------
    executor:
        Name of the executor backend (e.g. ``openrouter``, ``anthropic``,
        ``gemini_cli``).  Resolved at runtime against the runner's executor
        registry; this module never imports concrete executors so it stays
        free of executor-specific dependencies.
    model:
        Concrete model identifier (e.g. ``gemini-3.1-pro-preview``).  When
        ``model_tier`` is also set, ``model_tier`` wins for tier-aware
        executors (OpenRouter) and ``model`` wins for direct-model executors
        (Gemini CLI).
    model_tier:
        Friendly tier name (``haiku`` / ``sonnet`` / ``opus``) used by
        tier-aware executors.
    role:
        System-prompt-style role description prepended to every prompt this
        participant receives.  Optional.
    thinking_level:
        Thinking budget passed to executors that support extended thinking
        (Anthropic / OpenRouter).  Defaults to ``off`` to keep prototype
        costs predictable.
    """

    executor: str = Field(
        ..., description="Executor backend name (openrouter, anthropic, gemini_cli, ...)."
    )
    model: Optional[str] = Field(default=None, description="Concrete model identifier.")
    model_tier: Optional[str] = Field(default=None, description="Tier name (haiku/sonnet/opus).")
    role: Optional[str] = Field(default=None, description="Role prompt prepended to every turn.")
    thinking_level: Optional[str] = Field(
        default="off", description="Thinking budget (off/low/medium/high)."
    )

    model_config = {"extra": "allow"}

    @field_validator("executor")
    @classmethod
    def _executor_non_empty(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("DialogueParticipant.executor must be a non-empty string")
        return v.strip()


class DialoguePhaseConfig(BaseModel):
    """Pydantic config parsed from a ``type: dialogue`` phase in pipeline YAML.

    Example YAML::

        - id: spec_review
          type: dialogue
          drafter:
            executor: openrouter
            model_tier: opus
            role: "You are a senior software engineer..."
          reviewer:
            executor: gemini_cli
            model: gemini-3.1-pro-preview
            role: "You are a principal architect reviewing..."
          max_rounds: 4
          convergence_signal: APPROVED
    """

    drafter: DialogueParticipant
    reviewer: DialogueParticipant
    max_rounds: int = Field(default=DEFAULT_MAX_ROUNDS, ge=1, le=20)
    convergence_signal: str = Field(default=DEFAULT_CONVERGENCE_SIGNAL)
    drift_similarity_threshold: float = Field(default=DRIFT_SIMILARITY_THRESHOLD, ge=0.0, le=1.0)

    model_config = {"extra": "allow"}

    @field_validator("convergence_signal")
    @classmethod
    def _signal_non_empty(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("DialoguePhaseConfig.convergence_signal must be non-empty")
        return v.strip()


# ---------------------------------------------------------------------------
# Result models (dataclasses — simpler serialisation, fewer pydantic surprises
# inside an existing dataclass-heavy module)
# ---------------------------------------------------------------------------


@dataclass
class DialogueRound:
    """Result of a single drafter ↔ reviewer round."""

    round_number: int
    draft_text: str
    review_text: str
    approved: bool
    drafter_cost: Decimal = Decimal("0")
    reviewer_cost: Decimal = Decimal("0")
    drafter_tokens: int = 0
    reviewer_tokens: int = 0
    drafter_model: Optional[str] = None
    reviewer_model: Optional[str] = None
    drift_similarity: Optional[float] = None
    """Jaccard similarity vs. previous round's draft; ``None`` for round 1."""

    @property
    def cost(self) -> Decimal:
        """Total cost of this round (drafter + reviewer)."""
        return Decimal(self.drafter_cost) + Decimal(self.reviewer_cost)


@dataclass
class DialogueResult:
    """Full dialogue transcript and aggregate metrics."""

    final_draft: str
    rounds: List[DialogueRound]
    converged: bool
    """True if the reviewer emitted ``convergence_signal`` within ``max_rounds``."""
    convergence_stall: bool = False
    """True if at least one round-pair exceeded the drift similarity threshold."""
    history: List[Dict[str, Any]] = field(default_factory=list)
    """Flat chronological record of every drafter/reviewer turn."""
    error: Optional[str] = None
    """Set when the dialogue failed (e.g. drafter timeout)."""

    @property
    def total_cost(self) -> Decimal:
        """Sum of per-round costs — no inflation, no fallback multiplier."""
        return sum((r.cost for r in self.rounds), Decimal("0"))

    @property
    def total_tokens(self) -> int:
        return sum(r.drafter_tokens + r.reviewer_tokens for r in self.rounds)

    @property
    def succeeded(self) -> bool:
        """True when no fatal error occurred (may still be unconverged)."""
        return self.error is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class DialogueRunner:
    """Orchestrates a dialogue phase: drafter ↔ reviewer for up to ``max_rounds`` rounds.

    The runner is deliberately ignorant of *which* executor is used — it
    speaks to the :class:`~.runner.TaskExecutor` ABC via ``execute(task, ...)``
    and reads ``result.result["output"]`` / ``result.cost_usd`` /
    ``result.tokens_consumed`` from the returned :class:`TaskResult`.
    """

    def __init__(
        self,
        config: DialoguePhaseConfig,
        drafter_executor: Any,
        reviewer_executor: Any,
        output_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
        phase_id: str = "dialogue",
    ) -> None:
        """Create a runner.

        Args:
            config: Parsed :class:`DialoguePhaseConfig`.
            drafter_executor: Object exposing ``execute(task, worker_id=..., model_tier=..., thinking_level=...)``.
            reviewer_executor: Same interface as ``drafter_executor`` (may be the same instance).
            output_dir: Directory to write per-round transcripts.  ``None`` → no files written.
            run_id: Optional run identifier (used in log lines for cross-referencing).
            phase_id: Identifier for the phase being run (default: ``"dialogue"``).
        """
        self.config = config
        self.drafter_executor = drafter_executor
        self.reviewer_executor = reviewer_executor
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.run_id = run_id
        self.phase_id = phase_id

        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, initial_input: str) -> DialogueResult:
        """Run the dialogue loop and return a :class:`DialogueResult`.

        Args:
            initial_input: The seed prompt for round 1 (e.g. a rough spec).

        Returns:
            :class:`DialogueResult` with the final draft, per-round transcripts,
            and aggregate cost/token totals.  On executor failure (timeout, API
            error) the result's ``error`` field is set and the loop stops at
            the failing round.
        """
        rounds: List[DialogueRound] = []
        history: List[Dict[str, Any]] = []
        prior_review: Optional[str] = None
        prior_draft: Optional[str] = None
        convergence_stall = False
        consecutive_drift_hits = 0

        for round_num in range(1, self.config.max_rounds + 1):
            logger.info(
                "Dialogue %s round %d/%d: drafter=%s/%s reviewer=%s/%s",
                self.run_id or self.phase_id,
                round_num,
                self.config.max_rounds,
                self.config.drafter.executor,
                self.config.drafter.model_tier or self.config.drafter.model or "default",
                self.config.reviewer.executor,
                self.config.reviewer.model_tier or self.config.reviewer.model or "default",
            )

            # --- Drafter turn -------------------------------------------------
            drafter_prompt = self._build_drafter_prompt(
                initial_input=initial_input,
                round_num=round_num,
                prior_rounds=rounds,
            )
            try:
                drafter_result = self._invoke(
                    executor=self.drafter_executor,
                    participant=self.config.drafter,
                    prompt=drafter_prompt,
                    worker_id=f"dialogue-drafter-{round_num}",
                )
            except Exception as exc:
                logger.error(
                    "Dialogue %s round %d: drafter executor failed: %s",
                    self.run_id or self.phase_id,
                    round_num,
                    exc,
                )
                return DialogueResult(
                    final_draft=prior_draft or "",
                    rounds=rounds,
                    converged=False,
                    convergence_stall=convergence_stall,
                    history=history,
                    error=f"drafter_failed_round_{round_num}: {exc}",
                )

            drafter_failure = self._failure_message(drafter_result)
            if drafter_failure:
                logger.error(
                    "Dialogue %s round %d: drafter returned FAILED: %s",
                    self.run_id or self.phase_id,
                    round_num,
                    drafter_failure,
                )
                return DialogueResult(
                    final_draft=prior_draft or "",
                    rounds=rounds,
                    converged=False,
                    convergence_stall=convergence_stall,
                    history=history,
                    error=f"drafter_failed_round_{round_num}: {drafter_failure}",
                )

            draft_text = self._extract_output(drafter_result)
            self._write_round_file(round_num, "draft", draft_text)

            history.append(
                {
                    "round": round_num,
                    "role": "drafter",
                    "executor": self.config.drafter.executor,
                    "model": self._effective_model(self.config.drafter, drafter_result),
                    "text": draft_text,
                }
            )

            # --- Drift detection (rounds 2+) ----------------------------------
            similarity: Optional[float] = None
            if prior_draft is not None:
                similarity = _jaccard_similarity(prior_draft, draft_text)
                if similarity > self.config.drift_similarity_threshold:
                    consecutive_drift_hits += 1
                    if consecutive_drift_hits >= 2 and not convergence_stall:
                        convergence_stall = True
                        logger.warning(
                            "Dialogue %s: convergence_stall — two consecutive draft pairs "
                            "with Jaccard similarity > %.2f (last=%.4f) without convergence",
                            self.run_id or self.phase_id,
                            self.config.drift_similarity_threshold,
                            similarity,
                        )
                else:
                    consecutive_drift_hits = 0

            # --- Reviewer turn ------------------------------------------------
            reviewer_prompt = self._build_reviewer_prompt(
                initial_input=initial_input,
                round_num=round_num,
                draft_text=draft_text,
            )
            try:
                reviewer_result = self._invoke(
                    executor=self.reviewer_executor,
                    participant=self.config.reviewer,
                    prompt=reviewer_prompt,
                    worker_id=f"dialogue-reviewer-{round_num}",
                )
            except Exception as exc:
                logger.error(
                    "Dialogue %s round %d: reviewer executor failed: %s",
                    self.run_id or self.phase_id,
                    round_num,
                    exc,
                )
                # Still record the drafter's round so the caller can see what was produced
                rounds.append(
                    DialogueRound(
                        round_number=round_num,
                        draft_text=draft_text,
                        review_text="",
                        approved=False,
                        drafter_cost=_safe_cost(drafter_result),
                        reviewer_cost=Decimal("0"),
                        drafter_tokens=_safe_tokens(drafter_result),
                        reviewer_tokens=0,
                        drafter_model=self._effective_model(self.config.drafter, drafter_result),
                        reviewer_model=None,
                        drift_similarity=similarity,
                    )
                )
                return DialogueResult(
                    final_draft=draft_text,
                    rounds=rounds,
                    converged=False,
                    convergence_stall=convergence_stall,
                    history=history,
                    error=f"reviewer_failed_round_{round_num}: {exc}",
                )

            reviewer_failure = self._failure_message(reviewer_result)
            if reviewer_failure:
                logger.error(
                    "Dialogue %s round %d: reviewer returned FAILED: %s",
                    self.run_id or self.phase_id,
                    round_num,
                    reviewer_failure,
                )
                rounds.append(
                    DialogueRound(
                        round_number=round_num,
                        draft_text=draft_text,
                        review_text="",
                        approved=False,
                        drafter_cost=_safe_cost(drafter_result),
                        reviewer_cost=Decimal("0"),
                        drafter_tokens=_safe_tokens(drafter_result),
                        reviewer_tokens=0,
                        drafter_model=self._effective_model(self.config.drafter, drafter_result),
                        reviewer_model=None,
                        drift_similarity=similarity,
                    )
                )
                return DialogueResult(
                    final_draft=draft_text,
                    rounds=rounds,
                    converged=False,
                    convergence_stall=convergence_stall,
                    history=history,
                    error=f"reviewer_failed_round_{round_num}: {reviewer_failure}",
                )

            review_text = self._extract_output(reviewer_result)
            self._write_round_file(round_num, "review", review_text)

            approved = self._is_approved(review_text)

            history.append(
                {
                    "round": round_num,
                    "role": "reviewer",
                    "executor": self.config.reviewer.executor,
                    "model": self._effective_model(self.config.reviewer, reviewer_result),
                    "text": review_text,
                    "approved": approved,
                }
            )

            round_record = DialogueRound(
                round_number=round_num,
                draft_text=draft_text,
                review_text=review_text,
                approved=approved,
                drafter_cost=_safe_cost(drafter_result),
                reviewer_cost=_safe_cost(reviewer_result),
                drafter_tokens=_safe_tokens(drafter_result),
                reviewer_tokens=_safe_tokens(reviewer_result),
                drafter_model=self._effective_model(self.config.drafter, drafter_result),
                reviewer_model=self._effective_model(self.config.reviewer, reviewer_result),
                drift_similarity=similarity,
            )
            rounds.append(round_record)

            prior_draft = draft_text
            prior_review = review_text

            if approved:
                logger.info(
                    "Dialogue %s round %d: reviewer APPROVED — terminating early",
                    self.run_id or self.phase_id,
                    round_num,
                )
                return DialogueResult(
                    final_draft=draft_text,
                    rounds=rounds,
                    converged=True,
                    convergence_stall=convergence_stall,
                    history=history,
                )

        # max_rounds reached without convergence — return final draft anyway
        logger.info(
            "Dialogue %s: max_rounds=%d hit without convergence; returning final draft",
            self.run_id or self.phase_id,
            self.config.max_rounds,
        )
        return DialogueResult(
            final_draft=prior_draft or "",
            rounds=rounds,
            converged=False,
            convergence_stall=convergence_stall,
            history=history,
        )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_drafter_prompt(
        self,
        initial_input: str,
        round_num: int,
        prior_rounds: List[DialogueRound],
    ) -> str:
        """Construct the drafter's prompt for the given round.

        Round 1: just the initial input + role.
        Round 2+: initial input + every prior review (accumulating history).
        """
        parts: List[str] = []
        if self.config.drafter.role:
            parts.append(self.config.drafter.role.strip())
            parts.append("")

        if round_num == 1:
            parts.append("## Task")
            parts.append(initial_input)
            parts.append("")
            parts.append("Produce your initial draft below.")
        else:
            parts.append("## Original task")
            parts.append(initial_input)
            parts.append("")
            parts.append(f"## Conversation history (rounds 1–{round_num - 1})")
            parts.append(
                "The reviewer has critiqued your prior drafts. Incorporate ALL "
                "outstanding feedback below into a new, improved draft."
            )
            parts.append("")
            for prev in prior_rounds:
                parts.append(f"### Round {prev.round_number} — your previous draft")
                parts.append(prev.draft_text)
                parts.append("")
                parts.append(f"### Round {prev.round_number} — reviewer's critique")
                parts.append(prev.review_text)
                parts.append("")
            parts.append(f"## Round {round_num} — produce a revised draft")
            parts.append(
                "Address every critique above.  Do not repeat the previous "
                "draft verbatim.  Output only the revised artefact."
            )

        return "\n".join(parts)

    def _build_reviewer_prompt(
        self,
        initial_input: str,
        round_num: int,
        draft_text: str,
    ) -> str:
        """Construct the reviewer's prompt for the given round."""
        parts: List[str] = []
        if self.config.reviewer.role:
            parts.append(self.config.reviewer.role.strip())
            parts.append("")

        parts.append("## Original task")
        parts.append(initial_input)
        parts.append("")
        parts.append(f"## Draft under review (round {round_num})")
        parts.append(draft_text)
        parts.append("")
        parts.append("## Your review")
        parts.append(
            f"Either critique this draft (be specific) or, if it is acceptable, "
            f"emit the single line `{self.config.convergence_signal}` to terminate the dialogue."
        )
        parts.append(
            f"If you approve, your response MUST contain `{self.config.convergence_signal}` "
            f"as either the first or the last meaningful line."
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Executor adapter
    # ------------------------------------------------------------------

    def _invoke(
        self,
        executor: Any,
        participant: DialogueParticipant,
        prompt: str,
        worker_id: str,
    ) -> TaskResult:
        """Adapt the participant config + prompt to a :class:`TaskSpec` and execute.

        Extra payload fields (``model``, ``role``) are forwarded so executors
        like :class:`~.executors.gemini_cli_executor.GeminiCliExecutor` that
        accept a concrete model name can pick them up.
        """
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "phase_id": self.phase_id,
        }
        if participant.model:
            payload["model"] = participant.model
        if participant.role:
            payload["role"] = participant.role

        task = TaskSpec(
            type=TaskType.REVIEW,
            payload=payload,
        )

        return executor.execute(
            task,
            worker_id=worker_id,
            model_tier=participant.model_tier,
            thinking_level=participant.thinking_level or "off",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_approved(self, review_text: str) -> bool:
        """Return True when the reviewer's text signals convergence.

        Accepts the configured ``convergence_signal`` keyword on its own line
        OR via :func:`verdict_parser.extract_verdict` for the standard
        ``APPROVE`` / ``APPROVED`` / ``REQUEST_CHANGES`` lexicon.
        """
        if not review_text:
            return False

        signal = self.config.convergence_signal.upper()
        upper = review_text.upper()

        # Pass 1: simple substring on a meaningful line — fast path.
        for line in review_text.splitlines():
            stripped = line.strip().strip("*_`#>~-").strip().upper()
            if not stripped:
                continue
            # Exact-word match on the line (avoid matching "APPROVED_FOO")
            tokens = [t.strip(".,:;!?") for t in stripped.split()]
            if signal in tokens:
                return True

        # Pass 2: defer to existing verdict_parser when keyword is the canonical APPROVE.
        # ``extract_verdict`` returns lowercase ``approve``/``request_changes``/``abort``.
        if signal in ("APPROVE", "APPROVED"):
            verdict = extract_verdict(text=review_text)
            if verdict == "approve":
                return True

        return False

    def _extract_output(self, result: TaskResult) -> str:
        """Pull the textual output from an executor's :class:`TaskResult`.

        Handles both ``result.result["output"]`` (OpenRouter / Gemini CLI
        convention) and ``result.result["text"]`` (Anthropic executor).
        Falls back to the JSON-serialised result dict to avoid swallowing
        unexpected shapes.
        """
        if result is None:
            return ""
        payload = getattr(result, "result", None) or {}
        if isinstance(payload, dict):
            for key in ("output", "text", "message", "content"):
                if key in payload and payload[key]:
                    return str(payload[key])
            # Last-ditch: stringify the dict
            return str(payload)
        return str(payload)

    def _failure_message(self, result: TaskResult) -> Optional[str]:
        """Return a human-readable failure description, or None on success."""
        state = getattr(result, "state", None)
        if state == TaskState.FAILED:
            errors = getattr(result, "errors", None) or []
            if errors:
                first = errors[0]
                code = getattr(first, "code", "") or (
                    first.get("code") if isinstance(first, dict) else ""
                )
                msg = getattr(first, "message", "") or (
                    first.get("message") if isinstance(first, dict) else ""
                )
                return f"{code}: {msg}" if code else msg or "unknown failure"
            return "executor returned FAILED with no error detail"
        return None

    def _effective_model(
        self, participant: DialogueParticipant, result: TaskResult
    ) -> Optional[str]:
        """Best-effort: prefer the executor-reported model_used, else config hint."""
        used = getattr(result, "model_used", None)
        if used:
            return used
        return participant.model or participant.model_tier

    def _write_round_file(self, round_num: int, kind: str, text: str) -> None:
        """Write ``round-N-<kind>.md`` to ``output_dir`` if configured."""
        if self.output_dir is None:
            return
        path = self.output_dir / f"round-{round_num}-{kind}.md"
        try:
            path.write_text(text or "", encoding="utf-8")
            logger.debug("Dialogue %s: wrote %s", self.run_id or self.phase_id, path)
        except OSError as exc:
            logger.warning(
                "Dialogue %s: failed to write %s: %s",
                self.run_id or self.phase_id,
                path,
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def run_dialogue(
    phase_config: DialoguePhaseConfig,
    drafter_executor: Any,
    reviewer_executor: Any,
    initial_input: str,
    output_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
    phase_id: str = "dialogue",
) -> DialogueResult:
    """Convenience wrapper around :class:`DialogueRunner`.

    Args:
        phase_config: Parsed :class:`DialoguePhaseConfig`.
        drafter_executor: TaskExecutor for the drafter side.
        reviewer_executor: TaskExecutor for the reviewer side.
        initial_input: Seed prompt for round 1.
        output_dir: Directory to write per-round transcripts (optional).
        run_id: Optional run identifier (logged for cross-referencing).
        phase_id: Identifier for the phase being run.

    Returns:
        :class:`DialogueResult` with full transcript and cost totals.
    """
    runner = DialogueRunner(
        config=phase_config,
        drafter_executor=drafter_executor,
        reviewer_executor=reviewer_executor,
        output_dir=output_dir,
        run_id=run_id,
        phase_id=phase_id,
    )
    return runner.run(initial_input)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _jaccard_similarity(a: str, b: str) -> float:
    """Return Jaccard similarity of token sets for two strings.

    Returns 1.0 when both strings are empty, 0.0 when only one is empty.
    Uses plain whitespace tokenisation (no stemming, no lower-casing) — fast,
    deterministic, stdlib-only.  Casing matters: we use ``.split()`` after
    lower-casing so "APPROVED" and "approved" do not falsely diverge.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    inter = set_a & set_b
    return len(inter) / len(union)


def _safe_cost(result: TaskResult) -> Decimal:
    """Return ``result.cost_usd`` as a Decimal, defaulting to 0 on missing/None.

    Honours #801: we use whatever the executor put in ``cost_usd`` (which for
    the OpenRouter executor is ``usage.total_cost`` when present), and never
    apply our own per-token multiplier here.
    """
    cost = getattr(result, "cost_usd", None)
    if cost is None:
        return Decimal("0")
    try:
        return Decimal(str(cost))
    except Exception:
        return Decimal("0")


def _safe_tokens(result: TaskResult) -> int:
    """Return ``result.tokens_consumed`` as an int, defaulting to 0."""
    n = getattr(result, "tokens_consumed", None)
    if n is None:
        return 0
    try:
        return int(n)
    except (TypeError, ValueError):
        return 0
