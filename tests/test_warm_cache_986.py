"""Sealed acceptance tests for #986 — per-run content-hash-keyed warm build/seed
CACHE + lifecycle hooks.

Derived ONLY from behavioral.md (7 contracts C1-C7 + §0 setup + §0.6 determinism
+ the 2026-06-14 orchestrator glob-semantics amendment). The tester has NOT seen
the spec or any implementation of the warm cache / lifecycle hooks. These tests
are the immutable constraint the implement phase must satisfy.

================================================================================
EXPECTED-TODAY LEDGER (8 test functions; 1 SHIELD green-at-HEAD, 7 RED-until-impl)
================================================================================
  test_c4_no_lifecycle_hooks_byte_identical        SHIELD  PASS-now
      No lifecycle_hooks block ⇒ today's engine already runs no hook, creates no
      marker, walks phases identically. Reachable & satisfied at HEAD; MUST stay
      green post-impl (the load-bearing opt-in / byte-identity shield, §0.8/§C4).

  test_c1_cache_hit_runs_once_across_n_phases       RED    FAIL-now
  test_c2_cache_miss_in_glob_change_reruns          RED    FAIL-now
  test_c3_no_rerun_on_out_of_glob_change            RED    FAIL-now
  test_c5_unallowlisted_command_aborts_run          RED    FAIL-now
  test_c6_nonzero_exit_aborts_and_not_cached        RED    FAIL-now
  test_c7_multiple_hooks_invalidate_independently   RED    FAIL-now
  test_det_hash_glob_set_determinism                RED    FAIL-now
      RED rationale: at HEAD the lifecycle_hooks template field, the per-phase
      hook-runner, and file_guard.hash_glob_set [NEW] do not exist. The template
      loader ignores the lifecycle_hooks: YAML block, so getattr(t,
      "lifecycle_hooks", None) is None and NO hook ever runs ⇒ no marker file is
      ever created and no permanently_failed hook-abort ever occurs. Each RED test
      fails at HEAD on the absent marker / absent hook-abort, and turns GREEN once
      the feature lands. test_det fails at HEAD via the guarded lazy import
      (file_guard.hash_glob_set is absent → AttributeError surfaced as assert).

Run from the worktree:
    PYTHONPATH=src python3 -m pytest tests/test_warm_cache_986.py
================================================================================
"""

from __future__ import annotations

import sys
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# --- Path shim so production imports resolve from the worktree's src/ ----------
# (Editable .pth may pin a different checkout — see worktree-pythonpath memory.)
_SRC = "/home/toscan/ToscanWorkspace/.wt/orchemist-986/src"
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# --- TODAY-REAL imports ONLY at module scope (§0.1) ---------------------------
# These symbols exist at HEAD, so module COLLECTION succeeds before the feature
# exists and the SHIELD/RED tests actually run. The [NEW] lifecycle_hooks field,
# the per-phase hook-runner, and file_guard.hash_glob_set are referenced LAZILY
# inside test bodies (via the loader / via attribute access).
from orchestration_engine.sequencer import StateMachineSequencer  # noqa: E402
from orchestration_engine.templates import (  # noqa: E402
    PhaseDefinition,
    PipelineTemplate,
    load_template,
)
from orchestration_engine.schemas import (  # noqa: E402
    TaskResult,
    TaskState,
    TaskType,
)
from orchestration_engine.command_executor import CommandExecutor  # noqa: E402

# Touch the symbols so linters/collectors confirm they import at HEAD. The hook
# command shells out through a real CommandExecutor (the tester never calls it
# directly — the [NEW] hook-runner does, per §0.1).
_ = (PipelineTemplate, TaskType, CommandExecutor)


# ==============================================================================
# Shared hermetic helpers (§0.4 / §0.5) — NO daemon, NO DB, NO network, NO LLM.
# Only the lifecycle hook ever shells out, via a real CommandExecutor; each phase
# "executes" by returning a fixed success TaskResult.
# ==============================================================================
def _build_runner(execute_fn):
    """Mock TaskRunner (§0.4): records every dispatched phase_id in ._submits,
    runs execute_fn for each phase. Mirrors the existing sequencer tests.
    """
    runner = MagicMock()
    store: dict = {}
    submits: list = []  # records every dispatched phase_id (in dispatch order)

    def submit_task(spec):
        store[spec.id] = spec
        submits.append(spec.payload.get("phase_id"))
        return spec.id

    runner.queue.submit_task.side_effect = submit_task
    runner.queue.get_task.side_effect = lambda tid: store.get(tid)
    runner.queue.complete_task = MagicMock()
    runner.queue.fail_task = MagicMock()

    ex = MagicMock()
    ex.can_handle.return_value = True
    # CRITICAL (§0.4 / harness rule 2): the sequencer calls
    # executor.execute(task_spec, worker_id=…, model_tier=…, thinking_level=…).
    # The stub MUST absorb those kwargs or a swallowed TypeError makes every
    # phase FAILED and the test fails for the wrong reason.
    ex.execute.side_effect = execute_fn
    runner.executors = [ex]

    runner._submits = submits  # so the test can read the dispatched-phase order
    return runner


def _ok(task_spec, **kwargs):
    """Every phase succeeds with NO shell-out (only the hook shells out). Returns
    a real TaskResult whose model_dump() yields state == "success", which routes
    via PhaseOutcome.SUCCESS (confirmed against transitions.determine_outcome).
    """
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=TaskState.SUCCESS,
        confidence=0.9,
        result={"text": "ok"},
    )


def _phase(pid, transitions=None, max_iterations=0):
    """A minimal content phase (§0.4)."""
    return PhaseDefinition(
        id=pid,
        name=pid,
        prompt_template="do",
        task_type="content",
        transitions=transitions or {},
        max_iterations=max_iterations,
    )


def _linear_chain(n):
    """§0.4 path A: N distinct phases p1→p2→…→pN, pN terminal (no transitions).
    Terminates CLEANLY: result.get("aborted") is falsy. Avoids the
    MAX_ITERATIONS_EXCEEDED cap-abort entirely (preferred per the task brief).
    """
    phases = []
    for i in range(1, n + 1):
        pid = f"p{i}"
        if i < n:
            phases.append(_phase(pid, transitions={"success": f"p{i + 1}"}))
        else:
            phases.append(_phase(pid, transitions={}))
    return phases


def _write_template_yaml(tmp_path, phases, lifecycle_hooks_yaml: str = "") -> Path:
    """Build a PipelineTemplate YAML on disk and return its path.

    Construction idiom (§0.2): we drive the implementation's OWN loader
    (templates.load_template) with a YAML file that declares the phases and,
    optionally, a top-level `lifecycle_hooks:` block. This is the sealed-correct
    route — it uses the real parser (no hand-rolled config object, no [NEW]
    symbol imported), so getattr(template, "lifecycle_hooks", None) reflects the
    declared block once the parser learns the field. At HEAD the loader ignores
    the block (the field is [NEW]) ⇒ lifecycle_hooks is None ⇒ the test is RED.
    The C4 baseline passes lifecycle_hooks_yaml="" so NO block is declared and
    getattr(template, "lifecycle_hooks", None) is None on BOTH sides.

    YAML LAYOUT (fixed round-2): `phases:` is a COLUMN-0 sibling of
    id/name/version/parallel (load_template reads a top-level `phases:` key via
    data.get("phases")). Each phase: `- id:` at col 2; name/task_type/
    prompt_template/max_iterations/transitions at col 4; transition keys at col 6.
    (Round-1 over-indented `phases:` to col 4, so YAML folded it into the
    `parallel:` scalar and raised a ScannerError at load_template.)
    """
    phase_blocks = []
    for p in phases:
        if p.transitions:
            trans = "\n".join(
                f"      {k}: {v}" for k, v in p.transitions.items()
            )
            trans_block = f"    transitions:\n{trans}\n"
        else:
            trans_block = "    transitions: {}\n"
        phase_blocks.append(
            f"  - id: {p.id}\n"
            f"    name: {p.id}\n"
            f"    task_type: content\n"
            f"    prompt_template: do\n"
            f"    max_iterations: {p.max_iterations}\n"
            f"{trans_block}"
        )
    phases_yaml = "phases:\n" + "".join(phase_blocks)

    doc = textwrap.dedent(
        """\
        id: warm-cache-986-test
        name: Warm Cache 986 Test
        version: 1.0.0
        parallel: false
        """
    )
    doc = doc + phases_yaml
    if lifecycle_hooks_yaml:
        doc = doc + "\n" + lifecycle_hooks_yaml + "\n"

    path = tmp_path / "template.yaml"
    path.write_text(doc, encoding="utf-8")
    return path


def _make_repo(tmp_path) -> Path:
    """A tmp repo working_dir with src/ and seeds/ subdirs (the invalidation-glob
    targets). Markers live at the repo ROOT — OUTSIDE every glob (§0.3 / §0.7).
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "seeds").mkdir(parents=True)
    (repo / "docs").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "seeds" / "s.sql").write_text("INSERT INTO t VALUES (1);\n", encoding="utf-8")
    return repo


def _seq(template, execute_fn, repo) -> StateMachineSequencer:
    """Construct the run-scoped sequencer (§0.4). config['repo_path'] roots the
    invalidation globs and is the cwd the hook command runs in (§0.3).
    """
    return StateMachineSequencer(
        template=template,
        runner=_build_runner(execute_fn),
        config={"repo_path": str(repo)},
    )


def _lines(path: Path) -> int:
    """Marker line-count = number of hook executions. Missing file ⇒ -1 sentinel
    (the hook never ran), which all marker assertions treat as 'no execution'.
    """
    if not path.exists():
        return -1
    text = path.read_text(encoding="utf-8")
    if text == "":
        return 0
    return len(text.splitlines())


# A build hook that APPENDS exactly one line to repo-root `build_marker` per run.
# The marker is written relative to cwd (= repo root, §0.3), so it lands at
# repo/build_marker — OUTSIDE src/** (the load-bearing marker-outside-glob trap,
# §0.7): if it were inside src/**, writing it would self-dirty the glob.
_BUILD_HOOK_CMD = (
    "python3 -c \"open('build_marker','a').write('x\\n')\""
)
_SEED_HOOK_CMD = (
    "python3 -c \"open('seed_marker','a').write('y\\n')\""
)


def _build_hook_yaml(allowed='["python3"]', command=_BUILD_HOOK_CMD,
                     invalidation='["src/**"]') -> str:
    """A single-`build`-hook lifecycle_hooks block (§0.2 shape)."""
    return textwrap.dedent(
        f"""\
        lifecycle_hooks:
          allowed_commands: {allowed}
          timeout_seconds: 120
          build:
            command: {json.dumps(command)}
            invalidation: {invalidation}
        """
    )


# ==============================================================================
# C1 — cache HIT: run once across N phases, then reuse                    [RED]
# ==============================================================================
def test_c1_cache_hit_runs_once_across_n_phases(tmp_path):
    """C1: 'build_marker exists and has EXACTLY 1 line — the build hook command
    executed EXACTLY ONCE (on phase 1's MISS); phases 2 and 3 were cache-HITs and
    skipped the command.' The src/** inputs are UNCHANGED across all 3 phases.

    Expected-today: FAIL (red) — at HEAD no hook runs, so build_marker does not
    exist; the 'exactly 1 line' assertion fails on a missing file. The '== 1'
    (not '>= 1') is the sealing trap: a re-run-every-phase impl yields 3 lines.
    """
    repo = _make_repo(tmp_path)
    tpl_path = _write_template_yaml(
        tmp_path, _linear_chain(3), _build_hook_yaml()
    )
    template = load_template(str(tpl_path))

    seq = _seq(template, _ok, repo)  # src/** never mutated ⇒ HIT on phases 2,3
    result = seq.execute({})

    marker = repo / "build_marker"
    assert marker.exists(), "C1: build hook never ran (build_marker absent)"
    assert _lines(marker) == 1, (
        f"C1: build hook must run EXACTLY ONCE across 3 phases (MISS on p1, "
        f"HIT on p2/p3); got {_lines(marker)} line(s)"
    )
    # Path A: clean completion (no cap-abort).
    assert not result.get("aborted"), "C1: linear chain must complete cleanly"


# ==============================================================================
# C2 — cache MISS on an in-glob change: the hook RE-RUNS                  [RED]
# ==============================================================================
def test_c2_cache_miss_in_glob_change_reruns(tmp_path):
    """C2: 'build_marker has EXACTLY 2 lines — phase 1 was the first-ever MISS
    (run #1); the in-glob change made phase 2 a MISS too (run #2 / re-run).' A
    file UNDER src/** is changed between phase 1 and phase 2 (§0.5).

    Per the 2026-06-14 amendment: modifying src/x.ts (a FILE under src/**) flips
    the build-glob hash → MISS → re-run.

    Expected-today: FAIL (red) — at HEAD no marker is created; the re-run count
    assertion fails on the missing file.
    """
    repo = _make_repo(tmp_path)
    tpl_path = _write_template_yaml(
        tmp_path, _linear_chain(2), _build_hook_yaml()
    )
    template = load_template(str(tpl_path))

    calls = {"n": 0}

    def execute_with_mutation(task_spec, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # AFTER p1 dispatched, BEFORE p2's pre-dispatch hash
            (repo / "src" / "x.ts").write_text("changed", encoding="utf-8")
        return _ok(task_spec, **kwargs)

    seq = _seq(template, execute_with_mutation, repo)
    seq.execute({})

    marker = repo / "build_marker"
    assert marker.exists(), "C2: build hook never ran (build_marker absent)"
    assert _lines(marker) == 2, (
        f"C2: in-glob change between phases must RE-RUN the hook (MISS, MISS); "
        f"expected 2 lines, got {_lines(marker)}"
    )


# ==============================================================================
# C3 — no re-run on an out-of-glob change                                 [RED]
# ==============================================================================
def test_c3_no_rerun_on_out_of_glob_change(tmp_path):
    """C3: 'build_marker has EXACTLY 1 line — the out-of-glob docs/y.md change
    left src/**'s hash unchanged, so phase 2 was a HIT (no re-run).' The
    build_marker is at the repo ROOT, also OUTSIDE src/** — if it were inside
    src/**, appending it would itself dirty the glob and falsely force a MISS.

    Per the 2026-06-14 amendment: modifying docs/y.md (outside src/**) does NOT
    flip the hash → HIT.

    Expected-today: FAIL (red) — at HEAD no marker exists; the 'exactly 1 line'
    assertion fails on the missing file.
    """
    repo = _make_repo(tmp_path)
    tpl_path = _write_template_yaml(
        tmp_path, _linear_chain(2), _build_hook_yaml()
    )
    template = load_template(str(tpl_path))

    calls = {"n": 0}

    def execute_with_mutation(task_spec, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # change a file OUTSIDE src/** between p1 and p2
            (repo / "docs" / "y.md").write_text("changed", encoding="utf-8")
        return _ok(task_spec, **kwargs)

    seq = _seq(template, execute_with_mutation, repo)
    seq.execute({})

    marker = repo / "build_marker"
    assert marker.exists(), "C3: build hook never ran (build_marker absent)"
    assert _lines(marker) == 1, (
        f"C3: out-of-glob change must NOT re-run the hook (MISS then HIT); "
        f"expected 1 line, got {_lines(marker)}"
    )


# ==============================================================================
# C4 — byte-identical default: NO hooks ⇒ nothing runs   [SHIELD — green at HEAD]
# ==============================================================================
def test_c4_no_lifecycle_hooks_byte_identical(tmp_path):
    """C4 (SHIELD): a template IDENTICAL to C1's EXCEPT it declares NO
    lifecycle_hooks block ⇒ 'No marker file is EVER created' and 'the run's phase
    sequence/outcome is the no-hooks baseline'. getattr(template,
    "lifecycle_hooks", None) is None.

    Expected-today: PASS (shield). Today's engine has no hook machinery, so an
    undeclared-hooks template already produces no marker and an unchanged phase
    walk. This MUST stay GREEN post-impl (the load-bearing opt-in / byte-identity
    promise). If it ever fails, the feature changed default behavior — a hard
    regression (§2).
    """
    repo = _make_repo(tmp_path)
    tpl_path = _write_template_yaml(
        tmp_path, _linear_chain(3), lifecycle_hooks_yaml=""  # NO block
    )
    template = load_template(str(tpl_path))

    # The public opt-in switch is OFF (the single internal-ish hook allowed, §0.2).
    assert getattr(template, "lifecycle_hooks", None) is None, (
        "C4: a template with no lifecycle_hooks block must have "
        "lifecycle_hooks == None"
    )

    seq = _seq(template, _ok, repo)
    result = seq.execute({})

    # No marker file is EVER created.
    assert not (repo / "build_marker").exists(), "C4: build_marker must not exist"
    assert not (repo / "seed_marker").exists(), "C4: seed_marker must not exist"

    # The phase sequence/outcome is the no-hooks baseline.
    assert seq.runner._submits == ["p1", "p2", "p3"], (
        f"C4: all 3 phases must dispatch in order; got {seq.runner._submits}"
    )
    assert not result.get("aborted"), "C4: run must complete cleanly"
    assert set(result["phase_outputs"]) == {"p1", "p2", "p3"}, (
        f"C4: phase_outputs must be the 3-phase baseline; "
        f"got {set(result['phase_outputs'])}"
    )


# ==============================================================================
# C5 — security: an un-allowlisted hook command ABORTS the run            [RED]
# ==============================================================================
def test_c5_unallowlisted_command_aborts_run(tmp_path):
    """C5: a build hook whose command token[0] (curl) is NOT in
    allowed_commands ["python3","touch"] ⇒ 'The run ABORTS terminally:
    result.get("aborted") is True, result["failed_phase"] == the first phase's
    id, result["final_output"]["state"] == "permanently_failed"', AND
    'build_marker was never created … runner._submits == []'.

    The blocked command WOULD write build_marker if it ran (it must not get the
    chance). Per §0.7 we assert specifically on permanently_failed and exclude
    the benign MAX_ITERATIONS_EXCEEDED cap-abort.

    Expected-today: FAIL (red) — at HEAD there is no hook-runner, so the (no-hook)
    phase dispatches normally: result.get("aborted") is NOT True and
    runner._submits is non-empty.
    """
    repo = _make_repo(tmp_path)
    blocked_cmd = "curl http://example.test/x ; python3 -c \"open('build_marker','a').write('x\\n')\""
    hooks = _build_hook_yaml(
        allowed='["python3", "touch"]',
        command=blocked_cmd,  # token[0] == "curl" ⇒ blocked
        invalidation='["src/**"]',
    )
    # A single terminal phase suffices: the hook fires before that phase's
    # submit_task, so the abort precedes any dispatch.
    tpl_path = _write_template_yaml(tmp_path, _linear_chain(1), hooks)
    template = load_template(str(tpl_path))

    seq = _seq(template, _ok, repo)
    result = seq.execute({})

    assert result.get("aborted") is True, "C5: blocked hook must ABORT the run"
    assert result["final_output"]["state"] == "permanently_failed", (
        f"C5: hook-failure terminal state must be 'permanently_failed'; got "
        f"{result['final_output'].get('state')!r}"
    )
    assert result.get("failed_phase") == "p1", (
        f"C5: failed_phase must be the about-to-dispatch phase 'p1'; got "
        f"{result.get('failed_phase')!r}"
    )
    # Exclude the benign cap-abort (§0.7 sealing caveat).
    assert result.get("abort_reason") != "MAX_ITERATIONS_EXCEEDED", (
        "C5: a security-block abort must NOT be the MAX_ITERATIONS cap-abort"
    )
    # The blocked command never executed, and the phase was never dispatched.
    assert not (repo / "build_marker").exists(), (
        "C5: the blocked command must never run (build_marker absent)"
    )
    assert seq.runner._submits == [], (
        f"C5: the phase must NOT be submitted after a blocked hook; got "
        f"{seq.runner._submits}"
    )


# ==============================================================================
# C6 — a hook command that exits non-zero ABORTS the run                  [RED]
# ==============================================================================
def test_c6_nonzero_exit_aborts_and_not_cached(tmp_path):
    """C6: an allowlisted build hook that EXITS NON-ZERO ⇒ 'The run ABORTS
    terminally: result.get("aborted") is True, result["final_output"]["state"]
    == "permanently_failed", result["failed_phase"] == the first phase's id',
    'runner._submits == []', AND 'a SECOND, fresh run … ABORTS AGAIN with
    permanently_failed' (the failed hash is NOT falsely cached as success — a
    stored success hash would HIT and let the run proceed).

    Expected-today: FAIL (red) — at HEAD no hook runs; the phase dispatches and
    the run does not abort with permanently_failed.
    """
    repo = _make_repo(tmp_path)
    nonzero_cmd = "python3 -c \"import sys; sys.exit(1)\""
    hooks = _build_hook_yaml(
        allowed='["python3"]', command=nonzero_cmd, invalidation='["src/**"]'
    )
    tpl_path = _write_template_yaml(tmp_path, _linear_chain(1), hooks)
    template = load_template(str(tpl_path))

    # First run: aborts terminally, phase never dispatched.
    seq1 = _seq(template, _ok, repo)
    result1 = seq1.execute({})

    assert result1.get("aborted") is True, "C6: non-zero hook must ABORT (run 1)"
    assert result1["final_output"]["state"] == "permanently_failed", (
        f"C6 (run 1): terminal state must be 'permanently_failed'; got "
        f"{result1['final_output'].get('state')!r}"
    )
    assert result1.get("failed_phase") == "p1", "C6 (run 1): failed_phase == p1"
    assert result1.get("abort_reason") != "MAX_ITERATIONS_EXCEEDED", (
        "C6 (run 1): hook-failure abort must NOT be the cap-abort"
    )
    assert seq1.runner._submits == [], (
        f"C6 (run 1): broken build must NOT let the phase dispatch; got "
        f"{seq1.runner._submits}"
    )

    # SECOND, fresh run over the SAME unchanged repo (fresh sequencer + runner).
    # A stored 'success' hash would produce a HIT and let the run proceed; a
    # correctly-uncached failure must abort AGAIN with permanently_failed.
    seq2 = _seq(template, _ok, repo)
    result2 = seq2.execute({})

    assert result2.get("aborted") is True, (
        "C6: the failed hash must NOT be cached — a second fresh run must abort "
        "AGAIN (run 2)"
    )
    assert result2["final_output"]["state"] == "permanently_failed", (
        f"C6 (run 2): terminal state must be 'permanently_failed'; got "
        f"{result2['final_output'].get('state')!r}"
    )
    assert seq2.runner._submits == [], (
        f"C6 (run 2): the phase must NOT dispatch on the re-run; got "
        f"{seq2.runner._submits}"
    )


# ==============================================================================
# C7 — multiple hooks (build + seed) invalidate INDEPENDENTLY             [RED]
# ==============================================================================
def test_c7_multiple_hooks_invalidate_independently(tmp_path):
    """C7: two hooks (declaration order build then seed); build invalidation
    src/** → build_marker, seed invalidation seeds/** → seed_marker. Over 3
    phases with a src/** change between p1/p2 and a seeds/** change between
    p2/p3:
      - 'build_marker has EXACTLY 2 lines' (p1 first-ever MISS; p2 MISS on its
        src/** change; p3 HIT — src/** unchanged), and
      - 'seed_marker has EXACTLY 2 lines' (p1 first-ever MISS; p2 HIT — change
        was out of seeds/**; p3 MISS on its seeds/** change).
    Both markers end at 2 from DIFFERENT phases ⇒ changing a build-glob file
    re-runs build but NOT seed, and a seed-glob change re-runs seed but NOT
    build. Both markers are at the repo ROOT, OUTSIDE both globs.

    Expected-today: FAIL (red) — at HEAD neither marker is created; the per-hook
    count assertions fail on the missing files.
    """
    repo = _make_repo(tmp_path)
    two_hooks = textwrap.dedent(
        f"""\
        lifecycle_hooks:
          allowed_commands: ["python3"]
          timeout_seconds: 120
          build:
            command: {json.dumps(_BUILD_HOOK_CMD)}
            invalidation: ["src/**"]
          seed:
            command: {json.dumps(_SEED_HOOK_CMD)}
            invalidation: ["seeds/**"]
        """
    )
    tpl_path = _write_template_yaml(tmp_path, _linear_chain(3), two_hooks)
    template = load_template(str(tpl_path))

    calls = {"n": 0}

    def execute_with_mutation(task_spec, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # between p1 and p2: touch src/** ONLY
            (repo / "src" / "x.ts").write_text("b2", encoding="utf-8")
        elif calls["n"] == 2:  # between p2 and p3: touch seeds/** ONLY
            (repo / "seeds" / "s2.sql").write_text("d2", encoding="utf-8")
        return _ok(task_spec, **kwargs)

    seq = _seq(template, execute_with_mutation, repo)
    seq.execute({})

    build_marker = repo / "build_marker"
    seed_marker = repo / "seed_marker"
    assert build_marker.exists(), "C7: build hook never ran (build_marker absent)"
    assert seed_marker.exists(), "C7: seed hook never ran (seed_marker absent)"
    assert _lines(build_marker) == 2, (
        f"C7: build must re-run on its src/** change only (p1 MISS, p2 MISS, "
        f"p3 HIT); expected 2 lines, got {_lines(build_marker)}"
    )
    assert _lines(seed_marker) == 2, (
        f"C7: seed must re-run on its seeds/** change only (p1 MISS, p2 HIT, "
        f"p3 MISS); expected 2 lines, got {_lines(seed_marker)}"
    )


# ==============================================================================
# §0.6 — the glob-set content-hash helper: determinism contract           [RED]
# ==============================================================================
def test_det_hash_glob_set_determinism(tmp_path):
    """§0.6: file_guard.hash_glob_set(root, globs) is the per-hook invalidation
    key. Asserts all six OBSERVABLE properties:
      (i)   identical content ⇒ identical digest
      (ii)  1-byte change ⇒ different digest
      (iii) order-independent (globs listed in a different order ⇒ same digest)
      (iv)  empty match-set ⇒ a stable, non-empty hex digest, equal across calls
            and DIFFERENT from any non-empty match-set's digest
      (v)   add/remove a matched file ⇒ different digest
      (vi)  out-of-glob change ⇒ UNCHANGED digest (unit twin of C3)

    Per the 2026-06-14 amendment, src/** matches FILES recursively under src/.

    Expected-today: FAIL (red) — the [NEW] helper is imported LAZILY and guarded,
    so at HEAD the missing-symbol assertion fails as a RED test (not a collection
    error).
    """
    # Lazy [NEW] import (§0.1 / §0.6): surfaces a HEAD absence as a RED failure.
    from orchestration_engine import file_guard

    fn = getattr(file_guard, "hash_glob_set", None)
    assert fn is not None, "file_guard.hash_glob_set [NEW] not implemented yet"

    root = tmp_path / "r"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("alpha\n", encoding="utf-8")
    (root / "src" / "b.ts").write_text("beta\n", encoding="utf-8")
    (root / "docs" / "z.md").write_text("zed\n", encoding="utf-8")

    globs = ["src/**"]

    # (i) identical content ⇒ identical digest.
    h1 = fn(str(root), globs)
    h2 = fn(str(root), globs)
    assert isinstance(h1, str) and h1, "(i) digest must be a non-empty string"
    assert h1 == h2, "(i) identical inputs must yield an identical digest"

    # (vi) out-of-glob change ⇒ UNCHANGED digest (unit twin of C3).
    (root / "docs" / "z.md").write_text("zed-CHANGED\n", encoding="utf-8")
    assert fn(str(root), globs) == h1, (
        "(vi) an out-of-glob change must leave the digest unchanged"
    )

    # (ii) 1-byte change to a matched file ⇒ different digest.
    (root / "src" / "a.ts").write_text("alphaX\n", encoding="utf-8")
    h_mod = fn(str(root), globs)
    assert h_mod != h1, "(ii) a 1-byte in-glob change must change the digest"

    # (v) add a matched file ⇒ different digest; then remove it ⇒ different again.
    (root / "src" / "c.ts").write_text("gamma\n", encoding="utf-8")
    h_add = fn(str(root), globs)
    assert h_add != h_mod, "(v) adding a matched file must change the digest"
    (root / "src" / "c.ts").unlink()
    h_rm = fn(str(root), globs)
    assert h_rm != h_add, "(v) removing a matched file must change the digest"
    assert h_rm == h_mod, "(v) removing the added file must restore the digest"

    # (iii) order-independent: two globs listed in either order ⇒ same digest.
    h_order_a = fn(str(root), ["src/**", "docs/**"])
    h_order_b = fn(str(root), ["docs/**", "src/**"])
    assert h_order_a == h_order_b, (
        "(iii) the digest must be independent of glob list order"
    )

    # (iv) empty match-set ⇒ a stable, non-empty hex digest, equal across calls
    #      and DIFFERENT from any non-empty match-set's digest.
    h_empty1 = fn(str(root), ["nonexistent_dir/**"])
    h_empty2 = fn(str(root), ["also_missing/**"])
    assert isinstance(h_empty1, str) and h_empty1, (
        "(iv) empty match-set digest must be a non-empty string"
    )
    assert h_empty1 == h_empty2, (
        "(iv) the empty-match-set sentinel must be the SAME on every call"
    )
    assert h_empty1 != h_rm, (
        "(iv) the empty-match-set digest must DIFFER from a non-empty match-set"
    )
