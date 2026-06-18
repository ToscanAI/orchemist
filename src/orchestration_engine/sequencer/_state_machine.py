"""StateMachineSequencer — dynamic state-machine pipeline executor.

EPIC #942 sub-issue 953c: this class was moved VERBATIM out of the
``sequencer`` package facade (``__init__.py``) into its own one-class
module. No logic changed. It subclasses :class:`PhaseSequencer`, imported
from the sibling ``._phase`` module. The facade re-exports
:class:`StateMachineSequencer` so every historical import keeps resolving
byte-identically.
"""

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from ..schemas import Priority, TaskSpec, TaskState
from ..templates import PhaseDefinition
from ..transitions import _VERDICT_KEYWORDS, PhaseOutcome, determine_outcome, extract_verdict
from ._helpers import (
    _analyze_round_findings,
    _extract_phase_text,
    _safe_call_hook,
    _wrap_callable_runner,
)
from ._phase import PhaseSequencer

logger = logging.getLogger(__name__)


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
        self._warm_cache: Dict[str, str] = {}
        """Per-run warm-build/seed cache (Issue #986): hook_name → last input-set
        content-hash. A HIT (stored hash == current glob-set hash) skips the hook;
        a MISS runs it and stores the new hash. Pure-hash state — no teardown
        needed. Reset at the start of each :meth:`execute` call."""

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
        self._warm_cache = {}  # Issue #986: per-run warm build/seed cache

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
                        from .. import feature_flags as _ff  # noqa: PLC0415

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
                                    from ..adversary_parser import (  # noqa: PLC0415
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

                # ── Concurrent fan-out group (#988) ───────────────────────────
                # When THIS phase declares a non-empty parallel_group it is a
                # pure fan-out node: run the #986 lifecycle hooks ONCE here
                # (single-threaded — neutralizes the _warm_cache race), dispatch
                # the members CONCURRENTLY via the proven #102 core, JOIN
                # (implicit at the pool __exit__), then resolve ONE transition
                # from this fan-out phase. Members are validated non-loop
                # (max_iterations==0) so the _current_build_iter / _build_phase_input
                # race cannot occur. A phase with empty parallel_group skips this
                # block entirely → the single-phase path below is byte-identical.
                if phase.parallel_group:
                    members = list(phase.parallel_group)
                    # (a) #986 hooks ONCE, single-threaded, before the group.
                    hook_failure = self._run_lifecycle_hooks(phase, current_iter)
                    if hook_failure is not None:
                        result = hook_failure
                        with self._phase_outputs_lock:
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
                    # (b)+(c)+(d) concurrent dispatch + implicit JOIN + lock-guarded
                    # member-output merge — all inside the #102 core, reused as-is.
                    group_abort = self._execute_wave_parallel(members, 0, initial_input)
                    if group_abort is not None:
                        # A member failed (respecting template fail_fast). Enrich
                        # the #102 abort dict with the walk's iteration_* fields so
                        # the daemon/postmortem see the same abort contract as every
                        # other walk abort, then propagate exactly like a single-phase
                        # failure abort.
                        group_abort.setdefault("iteration_history", dict(self.iteration_history))
                        group_abort.setdefault("iteration_counts", dict(self.iteration_counts))
                        self._invoke_on_phase_complete(current_phase_id, group_abort)
                        self._safe_call_hook(
                            self.on_pipeline_complete,
                            self.pipeline_context,
                            None,
                            pipeline_id=self.template.id,
                        )
                        return group_abort
                    # (e) all members succeeded → resolve ONE transition from the
                    # fan-out phase. Synthesize a SUCCESS result so determine_outcome
                    # maps to PhaseOutcome.SUCCESS and the fan-out node's own
                    # phase_outputs entry records the join (members are keyed
                    # individually under their own ids by _execute_wave_parallel).
                    group_result = {
                        "state": TaskState.SUCCESS.value,
                        "result": {"text": ""},
                        "parallel_group": members,
                        "metadata": {"iteration": current_iter},
                    }
                    with self._phase_outputs_lock:
                        self.phase_outputs[current_phase_id] = group_result
                    self._invoke_on_phase_complete(current_phase_id, group_result)
                    outcome = determine_outcome(group_result)
                    next_phase_id = self._resolve_next_phase(phase, outcome, group_result)
                    self._maybe_snapshot_on_approve(phase, next_phase_id)
                    if next_phase_id is None:
                        current_phase_id = None
                    else:
                        current_phase_id = next_phase_id
                    continue

                # ── Dialogue phase dispatch + gate (Track B + #840) ──────────
                # State-machine path: a type:dialogue phase routed via
                # transitions hits this dispatch site. The gate must be checked
                # here so the flag applies regardless of which sequencer the
                # consumer uses (PhaseSequencer linear / parallel paths cover
                # the non-state-machine case in _execute_wave_*).
                if getattr(phase, "dialogue_config", None) is not None:
                    from .. import feature_flags as _ff  # noqa: PLC0415

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

                # ── Snapshot protected_paths before each iteration (#706)
                # Re-snapshot per iteration so each retry baseline reflects
                # current state of the guarded directory (not iteration-1 state).
                # Local dict — no shared instance state.
                _path_snapshots = self._snapshot_protected_paths(phase)

                # ── Warm build/seed lifecycle hooks (#986) ────────────────────
                # Run declared hooks on a content-hash MISS before dispatching
                # this phase, so every phase observes an up-to-date build/seed.
                # A failed hook aborts the run (never proceed against stale state).
                hook_failure = self._run_lifecycle_hooks(phase, current_iter)
                if hook_failure is not None:
                    result = hook_failure
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

    # Stateless hook-invoker relocated to ._helpers (EPIC #942 953b). The
    # ``staticmethod(...)`` wrapper preserves no-self semantics so
    # ``self._safe_call_hook(...)`` resolves byte-identically.
    _safe_call_hook = staticmethod(_safe_call_hook)
