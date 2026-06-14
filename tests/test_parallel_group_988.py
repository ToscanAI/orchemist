"""Sealed acceptance suite for #988 — concurrent fan-out + JOIN via ``parallel_group``.

Derived ONLY from ``/home/toscan/ToscanWorkspace/.runs/run-988/behavioral.md`` (contracts
C1–C8 + §0 setup). The implementer must satisfy every assertion below verbatim; the suite is
the immutable constraint. No network, no real LLM, no daemon, no subprocess — concurrency is
proven with mock executors that record start/finish timestamps and a lock-guarded live counter,
and every group run is bounded by an anti-hang watchdog (§0.6) so a wrong impl FAILS instead of
HANGING the runner.

================================================================================================
EXPECTED-TODAY LEDGER (HEAD == main @ 45b96f6; ``parallel_group`` has ZERO occurrences in src/)
================================================================================================
``PhaseDefinition.parallel_group`` is a [NEW] dataclass kwarg that does NOT exist at HEAD, so any
test that constructs ``PhaseDefinition(..., parallel_group=[...])`` raises ``TypeError`` at HEAD —
that IS the RED. Per §0.2 the field is NOT getattr-guarded; it is passed directly as a constructor
kwarg, INSIDE the test body (never at module scope), so collection survives at HEAD and only the
RED tests fail when run. TODAY-real symbols import at module top.

  test_C1_members_execute_concurrently            -> FAIL-now  (RED): builds parallel_group → TypeError at HEAD;
                                                       post-impl proves live-counter peak==2 + window overlap.
  test_C2_consumer_joins_after_both_members       -> FAIL-now  (RED): parallel_group ctor TypeError at HEAD;
                                                       post-impl proves the JOIN barrier ordering.
  test_C3_lock_safe_merge_no_lost_write           -> FAIL-now  (RED): parallel_group ctor TypeError at HEAD;
                                                       post-impl proves both member outputs survive + keyed by own id.
  test_C4_member_failure_aborts_consumer_skipped  -> FAIL-now  (RED): parallel_group ctor TypeError at HEAD;
                                                       post-impl proves enriched abort + consumer never ran.
  test_C5_no_group_byte_identical_serial_walk     -> PASS-now  (SHIELD): NO parallel_group kwarg anywhere →
                                                       exercises today's serial walk; reachable now, must stay green.
  test_C6_loop_member_is_validation_error         -> FAIL-now  (RED): parallel_group ctor TypeError at HEAD;
                                                       post-impl proves validate_template flags the loop member.
  test_C7_missing_member_is_validation_error      -> FAIL-now  (RED): parallel_group ctor TypeError at HEAD;
                                                       post-impl proves validate_template flags the ghost member.
  test_C8a_self_loop_phase_unperturbed            -> PASS-now  (SHIELD): NO parallel_group anywhere; today's loop
                                                       walk; reachable now, must stay green.
  test_C8b_normal_phase_dispatches_singly         -> PASS-now  (SHIELD): NO parallel_group anywhere; today's serial
                                                       walk; reachable now, must stay green.

  Count: 9 test functions — 6 RED (C1,C2,C3,C4,C6,C7), 3 SHIELD (C5,C8a,C8b).

Why C5/C8 are SHIELDS and not RED: their behavioral content is the UNCHANGED serial / loop walk
(§0.2, C5/C8 notes). They declare NO ``parallel_group`` kwarg, so they construct and run against
today's code and pass now; introducing the field + fan-out branch must not perturb them.

CONTRACT-GAP notes (see acceptance_test.md): none block the suite — every C1–C8 assertion is
pinned by §0.9 exact terminal shapes and §0.4/§0.5 mechanisms.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "/home/toscan/ToscanWorkspace/.wt/orchemist-988/src")

# --- TODAY-REAL symbols only at module scope (collection must survive at HEAD) ---
from orchestration_engine.schemas import TaskError, TaskResult, TaskState, TaskType  # noqa: E402,F401
from orchestration_engine.sequencer import StateMachineSequencer  # noqa: E402
from orchestration_engine.templates import (  # noqa: E402
    PhaseDefinition,
    PipelineTemplate,
    TemplateEngine,
)
from orchestration_engine.transitions import PhaseOutcome, determine_outcome  # noqa: E402,F401

# NOTE: ``parallel_group`` is [NEW] and intentionally NOT imported/guarded here. It is passed as a
# PhaseDefinition constructor kwarg INSIDE each RED test body so collection works at HEAD and the
# RED tests fail with TypeError until the field lands.


# ================================================================================================
# In-repo runner shim — copied verbatim from tests/test_state_machine_sequencer.py (harness rule 3:
# never hand-roll the contract-named construction). The sequencer is driven by a mock TaskRunner
# whose single executor's ``execute`` side-effect is ``execute_fn(task_spec, **kwargs) -> TaskResult``.
# Every member is dispatched on its own worker thread which calls execute_fn, so the execute_fn body
# is where members run concurrently and where we record timing.
# ================================================================================================


def _build_runner(execute_fn: Callable) -> MagicMock:
    """Mock TaskRunner with a custom execute side-effect (verbatim in-repo helper)."""
    runner = MagicMock()
    _task_store: Dict[str, Any] = {}

    def submit_task(spec):
        _task_store[spec.id] = spec
        return spec.id

    def get_task(task_id):
        return _task_store.get(task_id)

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = get_task
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()

    executor = MagicMock()
    executor.can_handle.return_value = True
    executor.execute.side_effect = execute_fn
    runner.executors = [executor]
    return runner


def _make_sequencer(template: PipelineTemplate, execute_fn: Callable) -> StateMachineSequencer:
    """Build a StateMachineSequencer with a mock runner (verbatim in-repo helper)."""
    return StateMachineSequencer(template=template, runner=_build_runner(execute_fn))


# --- Result-dict factories (§0.3) realised as the in-repo TaskResult the executor returns. ---
def _ok_result(task_spec, text: str = "", **kwargs) -> TaskResult:
    """A SUCCESS TaskResult; text defaults to ``out::<phase_id>`` (§0.3 _ok)."""
    pid = task_spec.payload.get("phase_id", "?")
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": text or f"out::{pid}"},
    )


def _fail_result(task_spec, **kwargs) -> TaskResult:
    """A FAILED TaskResult (§0.3 _fail)."""
    pid = task_spec.payload.get("phase_id", "?")
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.FAILED,
        confidence=0.0,
        result={"text": ""},
        errors=[TaskError(code="EXEC_ERR", message=f"{pid} intentional failure", severity="error")],
    )


# ================================================================================================
# §0.6 ANTI-HANG watchdog — run execute() on a daemon thread with a join timeout, then assert the
# thread is no longer alive. A deadlocking/serial-wrong fan-out FAILS FAST instead of hanging.
# ================================================================================================


def run_bounded(seq: StateMachineSequencer, payload=None, timeout: float = 5.0):
    box: Dict[str, Any] = {}

    def _go() -> None:
        box["result"] = seq.execute(payload if payload is not None else {})

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return t, box


# ================================================================================================
# Template builders (§0.7). NOTE: ``phase_group`` passes the [NEW] parallel_group kwarg directly —
# it is only ever CALLED inside RED test bodies, never at module/collection scope.
# ================================================================================================


def _phase_no_group(
    pid: str,
    *,
    transitions: Optional[Dict[str, str]] = None,
    max_iterations: int = 0,
    prompt: str = "do",
) -> PhaseDefinition:
    """A TODAY-real PhaseDefinition with NO parallel_group kwarg (collection-safe)."""
    return PhaseDefinition(
        id=pid,
        name=pid,
        prompt_template=prompt,
        transitions=transitions or {},
        max_iterations=max_iterations,
    )


def _phase_group(
    pid: str,
    parallel_group: List[str],
    *,
    transitions: Optional[Dict[str, str]] = None,
    max_iterations: int = 0,
    prompt: str = "do",
) -> PhaseDefinition:
    """A PhaseDefinition WITH the [NEW] parallel_group kwarg — RED at HEAD (TypeError).

    Per §0.2 the kwarg is passed directly (NOT getattr-guarded) so the contract genuinely fails
    until the field lands. Only invoked inside RED test bodies.
    """
    return PhaseDefinition(
        id=pid,
        name=pid,
        prompt_template=prompt,
        transitions=transitions or {},
        max_iterations=max_iterations,
        parallel_group=parallel_group,  # [NEW] — does not exist at HEAD
    )


def _template(phases: List[PhaseDefinition], template_id: str = "fanout-test") -> PipelineTemplate:
    return PipelineTemplate(id=template_id, name="Fan-out Test", phases=phases)


# ================================================================================================
# §0.4 concurrency recorder + §0.5 event log — fresh per test via this factory (no cross-test state).
# ================================================================================================


def _make_concurrency_recorder():
    """Return (state, record_member) where record_member(pid, sleep_s) does the §0.4 dance."""
    state = {
        "lock": threading.Lock(),
        "live": [0],
        "max_live": [0],
        "starts": {},
        "finishes": {},
    }

    def record_member(pid: str, sleep_s: float = 0.05) -> None:
        with state["lock"]:
            state["live"][0] += 1
            if state["live"][0] > state["max_live"][0]:
                state["max_live"][0] = state["live"][0]
            state["starts"][pid] = time.monotonic()
        time.sleep(sleep_s)  # a real sleep so windows can overlap
        with state["lock"]:
            state["finishes"][pid] = time.monotonic()
            state["live"][0] -= 1

    return state, record_member


def _make_event_log():
    """Return (evlog, log_event, idx) implementing the §0.5 lock-appended global event log."""
    lock = threading.Lock()
    evlog: List[tuple] = []

    def log_event(pid: str, event: str) -> None:
        with lock:
            evlog.append((pid, event))

    def idx(pid: str, event: str) -> int:
        return next(i for i, (p, e) in enumerate(evlog) if p == pid and e == event)

    return evlog, log_event, idx


# ================================================================================================
# C1 — ACTUAL concurrency (members run AT THE SAME TIME) — [RED]
# ================================================================================================


class TestC1ActualConcurrency:
    def test_C1_members_execute_concurrently(self) -> None:
        """C1: 'the walk dispatches the listed member phases CONCURRENTLY'. The decisive
        proof (§0.4) is real overlap, NOT 'both ran': assert max simultaneously-live members
        == 2 AND the execution windows overlap (max(starts) < min(finishes)). A serial impl
        that calls members back-to-back yields max_live==1 and non-overlapping windows → FAILS.

        Expected-today: FAIL (RED) — _phase_group passes the [NEW] parallel_group kwarg, which
        raises TypeError at HEAD.
        """
        state, record_member = _make_concurrency_recorder()

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid in ("m1", "m2"):
                record_member(pid, sleep_s=0.05)
            return _ok_result(task_spec)

        fan = _phase_group("fan", ["m1", "m2"], transitions={"success": "consumer"})
        m1 = _phase_no_group("m1")
        m2 = _phase_no_group("m2")
        consumer = _phase_no_group("consumer")
        seq = _make_sequencer(_template([fan, m1, m2, consumer]), execute_fn)

        t, box = run_bounded(seq)

        assert not t.is_alive(), "execute() did not terminate — fan-out deadlock/hang regression"
        result = box["result"]
        assert not result.get("aborted"), "all-success fan-out must not abort"
        # (3) load-bearing: two members were live simultaneously.
        assert state["max_live"][0] >= 2, (
            f"members did not run concurrently — max simultaneously-live was "
            f"{state['max_live'][0]} (serial impl?)"
        )
        # (4) independent confirmation: the execution windows overlap.
        assert state["starts"] and state["finishes"], "members never recorded start/finish"
        assert max(state["starts"].values()) < min(state["finishes"].values()), (
            "member execution windows did not overlap (one finished before the other started)"
        )


# ================================================================================================
# C2 — JOIN before the next phase (the hard barrier) — [RED]
# ================================================================================================


class TestC2JoinBarrier:
    def test_C2_consumer_joins_after_both_members(self) -> None:
        """C2: 'JOINS (a hard barrier — all members finish before anything downstream runs)'.
        The consumer (the fan-out's success target) STARTS only after BOTH members FINISH:
        idx(consumer,start) > idx(m1,finish) AND idx(consumer,start) > idx(m2,finish); and at
        the instant consumer starts, both m1 and m2 are already keyed in phase_outputs.

        Expected-today: FAIL (RED) — _phase_group raises TypeError at HEAD.
        """
        state, record_member = _make_concurrency_recorder()
        evlog, log_event, idx = _make_event_log()
        consumer_saw: Dict[str, List[str]] = {}

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid in ("m1", "m2"):
                log_event(pid, "start")
                record_member(pid, sleep_s=0.05)
                log_event(pid, "finish")
                return _ok_result(task_spec)
            if pid == "consumer":
                log_event(pid, "start")
                # Snapshot which member ids are already present in the live merged outputs.
                snap = [k for k in ("m1", "m2") if k in (seq.phase_outputs or {})]
                consumer_saw["members"] = snap
                log_event(pid, "finish")
                return _ok_result(task_spec)
            return _ok_result(task_spec)

        fan = _phase_group("fan", ["m1", "m2"], transitions={"success": "consumer"})
        m1 = _phase_no_group("m1")
        m2 = _phase_no_group("m2")
        consumer = _phase_no_group("consumer")
        seq = _make_sequencer(_template([fan, m1, m2, consumer]), execute_fn)

        t, box = run_bounded(seq)

        assert not t.is_alive(), "execute() did not terminate — JOIN-barrier deadlock regression"
        result = box["result"]
        assert not result.get("aborted")
        # The consumer starts strictly after BOTH members finish (the hard barrier).
        assert idx("consumer", "start") > idx("m1", "finish"), "consumer started before m1 finished"
        assert idx("consumer", "start") > idx("m2", "finish"), "consumer started before m2 finished"
        # When consumer runs, the complete merged fan-out output is visible.
        assert set(consumer_saw.get("members", [])) == {"m1", "m2"}, (
            "consumer did not observe BOTH member outputs at start — JOIN is not a full barrier"
        )


# ================================================================================================
# C3 — lock-safe merged outputs (no lost write) — [RED]
# ================================================================================================


class TestC3LockSafeMerge:
    def test_C3_lock_safe_merge_no_lost_write(self) -> None:
        """C3: 'merges each member's output into phase_outputs keyed by member id'. Both members
        return distinct text (m1→ALPHA, m2→BETA) under concurrent recording; after the group both
        are present in phase_outputs, each keyed by its OWN id and carrying its OWN text — no lost
        write, no cross-keying.

        Expected-today: FAIL (RED) — _phase_group raises TypeError at HEAD.
        """
        state, record_member = _make_concurrency_recorder()
        texts = {"m1": "ALPHA", "m2": "BETA"}

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid in ("m1", "m2"):
                record_member(pid, sleep_s=0.05)
                return _ok_result(task_spec, text=texts[pid])
            return _ok_result(task_spec)

        fan = _phase_group("fan", ["m1", "m2"], transitions={"success": "consumer"})
        m1 = _phase_no_group("m1")
        m2 = _phase_no_group("m2")
        consumer = _phase_no_group("consumer")
        seq = _make_sequencer(_template([fan, m1, m2, consumer]), execute_fn)

        t, box = run_bounded(seq)

        assert not t.is_alive(), "execute() did not terminate — merge deadlock regression"
        result = box["result"]
        assert not result.get("aborted")
        outputs = result["phase_outputs"]
        # Both member outputs survived the concurrent merge.
        assert "m1" in outputs and "m2" in outputs, (
            f"a member output was lost in the concurrent merge — present: {sorted(outputs)}"
        )
        # Each entry is correctly attributed: own id → own text.
        m1_text = (outputs["m1"].get("result") or {}).get("text", "")
        m2_text = (outputs["m2"].get("result") or {}).get("text", "")
        assert "ALPHA" in m1_text, f"m1 output mis-keyed/clobbered: {m1_text!r}"
        assert "BETA" in m2_text, f"m2 output mis-keyed/clobbered: {m2_text!r}"


# ================================================================================================
# C4 — failure path: one member fails → run aborts, success transition NOT taken — [RED]
# ================================================================================================


class TestC4MemberFailureAborts:
    def test_C4_member_failure_aborts_consumer_skipped(self) -> None:
        """C4: 'any member fails → the run aborts on the failure path, success transition NOT
        taken'. With m2 failing and m1 succeeding: terminal shape is the enriched #102 abort dict
        (§0.9) — aborted is True, failed_phase == 'm2', iteration_history + iteration_counts
        present — and the consumer (success target) NEVER ran. A succeeding m1 must NOT manufacture
        a false success.

        Expected-today: FAIL (RED) — _phase_group raises TypeError at HEAD.
        """
        state, record_member = _make_concurrency_recorder()
        evlog, log_event, idx = _make_event_log()

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            if pid in ("m1", "m2"):
                log_event(pid, "start")
                record_member(pid, sleep_s=0.05)
                log_event(pid, "finish")
                return _fail_result(task_spec) if pid == "m2" else _ok_result(task_spec)
            if pid == "consumer":
                log_event(pid, "start")
                return _ok_result(task_spec)
            return _ok_result(task_spec)

        fan = _phase_group("fan", ["m1", "m2"], transitions={"success": "consumer"})
        m1 = _phase_no_group("m1")
        m2 = _phase_no_group("m2")
        consumer = _phase_no_group("consumer")
        seq = _make_sequencer(_template([fan, m1, m2, consumer]), execute_fn)

        t, box = run_bounded(seq)

        assert not t.is_alive(), "execute() did not terminate — failure-path deadlock regression"
        result = box["result"]
        # (2) the run took the failure/abort path, NOT the success transition.
        assert result.get("aborted") is True, "member failure must abort the run"
        # (3) the abort names the failed member.
        assert result.get("failed_phase") == "m2", (
            f"abort must name the failed member 'm2', got {result.get('failed_phase')!r}"
        )
        assert "m2" in result.get("failed_phases", []), "failed_phases must contain 'm2'"
        # (4) enriched with the walk's bookkeeping (matches the normal abort contract).
        assert "iteration_history" in result, "abort missing iteration_history"
        assert "iteration_counts" in result, "abort missing iteration_counts"
        # (5) the consumer (success target) NEVER ran.
        assert "consumer" not in result.get("phase_outputs", {}), "consumer ran despite abort"
        assert not any(
            p == "consumer" and e == "start" for (p, e) in evlog
        ), "consumer started despite the fan-out failure"


# ================================================================================================
# C5 — byte-identical default (THE SHIELD) — [GREEN at HEAD]
# ================================================================================================


class TestC5SerialWalkShield:
    def test_C5_no_group_byte_identical_serial_walk(self) -> None:
        """C5 (SHIELD): 'A phase with an empty parallel_group (the default) takes the
        byte-identical serial single-phase walk path.' With NO parallel_group anywhere, a linear
        chain a→b→c dispatches one phase at a time in order, reaches the terminal, and max observed
        concurrency is 1.

        Expected-today: PASS (SHIELD) — declares no parallel_group kwarg, exercises today's serial
        walk; adding the field must not perturb this.
        """
        dispatched: List[str] = []
        state, record_member = _make_concurrency_recorder()

        def execute_fn(task_spec, **kwargs):
            pid = task_spec.payload["phase_id"]
            dispatched.append(pid)
            # Wire the live-counter into the baseline runner: serial walk must peak at 1.
            record_member(pid, sleep_s=0.01)
            return _ok_result(task_spec)

        a = _phase_no_group("a", transitions={"success": "b"})
        b = _phase_no_group("b", transitions={"success": "c"})
        c = _phase_no_group("c")
        seq = _make_sequencer(_template([a, b, c]), execute_fn)

        result = seq.execute({})  # a serial walk cannot hang — no watchdog needed.

        assert dispatched == ["a", "b", "c"], f"serial dispatch order changed: {dispatched}"
        assert not result.get("aborted")
        assert "c" in result["phase_outputs"], "walk did not reach the terminal phase"
        assert state["max_live"][0] == 1, (
            f"no-group walk ran phases concurrently (max_live={state['max_live'][0]}) — "
            f"serial walk was perturbed"
        )


# ================================================================================================
# C6 — validation: a loop member is an ERROR (race-prevention guard) — [RED]
# ================================================================================================


class TestC6LoopMemberValidationError:
    def test_C6_loop_member_is_validation_error(self) -> None:
        """C6: a parallel_group member that is a loop phase (max_iterations > 0) is a build-time
        ERROR (the race-prevention guard). validate_template returns a non-empty list and at least
        one error names the member AND references the loop/max_iterations constraint.

        Expected-today: FAIL (RED) — _phase_group raises TypeError at HEAD.
        """
        fan = _phase_group("fan", ["m1"], transitions={"success": "fan"})
        m1 = _phase_no_group("m1", max_iterations=2)  # loop member → must be rejected
        tpl = _template([fan, m1])

        errors = TemplateEngine().validate_template(tpl)

        assert errors, "loop member must produce a validation error"
        assert any(
            "m1" in e and ("max_iterations" in e or "loop" in e.lower()) for e in errors
        ), f"no error names the loop member 'm1' with a loop/max_iterations reason: {errors}"


# ================================================================================================
# C7 — validation: a missing member is an ERROR — [RED]
# ================================================================================================


class TestC7MissingMemberValidationError:
    def test_C7_missing_member_is_validation_error(self) -> None:
        """C7: a parallel_group referencing a non-existent phase ('ghost') is a build-time ERROR,
        exactly as an unknown depends_on is. validate_template returns a non-empty list with at
        least one error naming 'ghost' (mirrors the depends_on-unknown precedent which asserts
        any("ghost" in e for e in errors)).

        Expected-today: FAIL (RED) — _phase_group raises TypeError at HEAD.
        """
        fan = _phase_group("fan", ["ghost"], transitions={"success": "fan"})
        m1 = _phase_no_group("m1")  # a real phase; 'ghost' is absent from the template
        tpl = _template([fan, m1])

        errors = TemplateEngine().validate_template(tpl)

        assert errors, "missing member must produce a validation error"
        assert any("ghost" in e for e in errors), (
            f"no error names the missing member 'ghost': {errors}"
        )


# ================================================================================================
# C8 — loop / normal-phase preservation outside a group (SHIELD) — [GREEN at HEAD]
# ================================================================================================


class TestC8Preservation:
    def test_C8a_self_loop_phase_unperturbed(self) -> None:
        """C8(a) (SHIELD): a self-loop phase OUTSIDE any group behaves exactly as on HEAD. With NO
        parallel_group anywhere, 'loop' (max_iterations=2, success→loop, exhausted→end) dispatches
        more than once then exits via exhausted to 'end'; the run is NOT a WALK_STEP_LIMIT abort.

        Expected-today: PASS (SHIELD) — no parallel_group; today's loop walk.
        """
        dispatched: List[str] = []

        def execute_fn(task_spec, **kwargs):
            dispatched.append(task_spec.payload["phase_id"])
            return _ok_result(task_spec)

        loop = _phase_no_group(
            "loop", max_iterations=2, transitions={"success": "loop", "exhausted": "end"}
        )
        end = _phase_no_group("end")
        seq = _make_sequencer(_template([loop, end]), execute_fn)

        result = seq.execute({})

        assert dispatched.count("loop") > 1, (
            f"loop phase did not iterate more than once: {dispatched}"
        )
        assert "end" in result.get("phase_outputs", {}), "loop did not exit to 'end' via exhausted"
        assert result.get("abort_reason") != "WALK_STEP_LIMIT", "loop walk hit the step ceiling"

    def test_C8b_normal_phase_dispatches_singly(self) -> None:
        """C8(b) (SHIELD): declaring the parallel_group construct must not turn an ordinary phase
        into a fan-out. With NO parallel_group anywhere, a→b→c dispatches in order and each id
        appears EXACTLY once (b is dispatched singly, not fanned out).

        Expected-today: PASS (SHIELD) — no parallel_group; today's serial walk.
        """
        dispatched: List[str] = []

        def execute_fn(task_spec, **kwargs):
            dispatched.append(task_spec.payload["phase_id"])
            return _ok_result(task_spec)

        a = _phase_no_group("a", transitions={"success": "b"})
        b = _phase_no_group("b", transitions={"success": "c"})
        c = _phase_no_group("c")
        seq = _make_sequencer(_template([a, b, c]), execute_fn)

        result = seq.execute({})

        assert dispatched == ["a", "b", "c"], f"serial dispatch order changed: {dispatched}"
        assert dispatched.count("b") == 1, "the middle phase was not dispatched singly"
        assert not result.get("aborted")
