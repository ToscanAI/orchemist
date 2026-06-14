"""Sealed acceptance tests for #985 — opt-in acceptance MATRIX on acceptance_run.

Derived from behavioral.md (contracts C1-C7) ALONE. The implementation of the
matrix path was NOT consulted; these tests are the immutable constraint.

The feature: a phase MAY declare an ordered ``acceptance_matrix`` —
``[{"name": str, "command": str}, ...]`` — reaching the executor via
``task.payload["acceptance_matrix"]``. When present and non-empty, the engine
runs EVERY entry (run-all-then-report, never fail-fast), enforces the per-phase
``allowed_commands`` allowlist + the dangerous-pattern denylist with
``shell=False`` semantics, truncates each entry's output at
``MAX_OUTPUT_BYTES = 1048576`` with the marker ``"\\n[OUTPUT TRUNCATED]"``, and
writes a per-entry ``"matrix"`` array PLUS aggregate fields into
``acceptance_results.json``. When NO matrix is declared, or it is an empty list
``[]``, the engine emits the byte-identical legacy 10-key file with NO
``"matrix"`` key.

=== EXPECTED-TODAY LEDGER (HEAD @ e62b0b5; matrix handling is [NEW]) ===========
The OpenClawExecutor ACCEPTANCE_RUN handler at HEAD does, IN THIS ORDER:
  1. guard: missing ``output_dir`` → error;
  2. guard: ``acceptance_tests.py`` missing in ``output_dir`` → ``test_file_not_found``
     error and writes NO results file;
  3. dry-run shortcut (AFTER the test-file guard) → writes today's fixed 10-key
     legacy result;
  4. else run real pytest on ``acceptance_tests.py``.
It IGNORES ``acceptance_matrix`` entirely — there is no per-entry runner and no
``"matrix"`` key.

CRITICAL HARNESS FACT (round-2 fix): because the dry-run shortcut sits AFTER the
``test_file_not_found`` guard, EVERY test that drives the real ACCEPTANCE_RUN
dispatch (shield or matrix) must place an ``acceptance_tests.py`` into the
task's ``output_dir`` first — otherwise the handler returns ``test_file_not_found``
and writes NO results file. The shields therefore plant a trivially-passing test
file and take the legacy 10-key dry-run path; the matrix tests plant the same
file (via ``_run``) so that — IF the implementer wrongly orders the matrix branch
AFTER the test-file guard — the legacy pytest path runs and writes a 10-key file
with NO ``matrix`` key, and the matrix tests fail cleanly on the missing
``matrix`` key (a meaningful ordering bug) rather than on an indistinguishable
FileNotFoundError.

  test_c1_no_matrix_legacy_ten_keys            SHIELD  PASS-now
      plants acceptance_tests.py, dry_run=True → today's exact 10-key legacy
      file; no matrix key today AND post-impl (legacy path untouched).
  test_c7_empty_list_matrix_routes_to_legacy   SHIELD  PASS-now
      plants acceptance_tests.py; empty-[] is ignored by today's handler exactly
      like "no matrix" → legacy 10-key dry-run file, no matrix key. Stays a true
      GREEN shield post-impl (empty-[] must route to legacy).
  test_c2_all_pass_matrix_aggregate_green      RED     FAIL-now
      _run plants acceptance_tests.py → today's handler runs legacy pytest and
      writes a 10-key file with NO "matrix" key → assert on data["matrix"]
      raises KeyError → clean RED-by-design (missing matrix key).
  test_c3_run_all_then_report_not_fail_fast    RED     FAIL-now   (load-bearing)
      same: legacy 10-key file, no "matrix" key → fails on missing matrix key.
  test_c4_per_entry_output_truncation          RED     FAIL-now
      same: no "matrix" key → fails on data["matrix"][0].
  test_c5_allowlist_block_others_still_run      RED     FAIL-now
      same: no "matrix" key → _entry() can't index data["matrix"].
  test_c5_shell_false_no_chaining               RED     FAIL-now
      same: no "matrix" key.
  test_c6_aggregate_field_consistency_green      RED     FAIL-now
      same: _assert_aggregate_consistent indexes data["matrix"] → KeyError.
  test_c6_aggregate_field_consistency_red        RED     FAIL-now
      same for the mixed (failing) matrix.
===============================================================================

Run from the worktree with: PYTHONPATH=src python3 -m pytest tests/test_acceptance_matrix_985.py
"""

import json
import os
import sys

# Worktree src on path so production imports resolve from any cwd.
sys.path.insert(0, "/home/toscan/ToscanWorkspace/.wt/orchemist-985/src")

import pytest

# TODAY-REAL symbols only at module scope (rule 1). The matrix HANDLING is [NEW]
# but needs no new import — it is a payload dict key + an output-JSON key.
from orchestration_engine.openclaw_executor import OpenClawExecutor
from orchestration_engine.schemas import TaskSpec, TaskState, TaskType


# Shared constants pinned by the contract (§0 "Shared facts").
MAX_OUTPUT_BYTES = 1048576  # 1024 * 1024
TRUNCATION_MARKER = "\n[OUTPUT TRUNCATED]"
TRUNCATION_MARKER_LEN = 19  # len("\n[OUTPUT TRUNCATED]")

LEGACY_KEYS = {
    "phase",
    "status",
    "test_file",
    "passed",
    "failed",
    "errors",
    "total",
    "pass_rate",
    "failure_details",
    "exit_code",
}

# A trivially-passing pytest file. Planted into a task's output_dir BEFORE the
# real ACCEPTANCE_RUN dispatch so the handler clears its ``test_file_not_found``
# guard (the dry-run shortcut + the real pytest path both sit AFTER that guard).
_PASSING_TEST_FILE = "def test_ok():\n    assert True\n"


def _plant_passing_test_file(output_dir):
    """Write a trivially-passing ``acceptance_tests.py`` into ``output_dir``.

    Required before driving the real ACCEPTANCE_RUN dispatch: at HEAD (and in any
    implementation that keeps the legacy path) the handler returns
    ``test_file_not_found`` — and writes NO results file — unless this file
    exists in ``output_dir``. Planting it makes the shields a true legacy GREEN
    and makes the matrix tests fail on the absent ``matrix`` key (a contract
    ordering bug) instead of an indistinguishable missing-file error.
    """
    (output_dir / "acceptance_tests.py").write_text(_PASSING_TEST_FILE)


# ---------------------------------------------------------------------------
# Helpers (all hermetic; never touch the network, the real build, or ~/.orch*)
# ---------------------------------------------------------------------------


def _matrix_task(tmp_path, matrix, allowed_commands):
    """Build an ACCEPTANCE_RUN TaskSpec the way the COMMAND/acceptance tests do.

    TaskSpec required fields are ``type`` + ``payload`` (rule 3 idiom); extras on
    the payload dict are free-form. We pass the BARE TaskSpec to ``execute`` —
    never a TaskState-wrap.
    """
    return TaskSpec(
        type=TaskType.ACCEPTANCE_RUN,
        payload={
            "output_dir": str(tmp_path),
            "working_dir": str(tmp_path),
            "acceptance_matrix": matrix,
            "allowed_commands": allowed_commands,
        },
    )


def _run(task):
    """Drive the PUBLIC dispatch (style 1): execute the TaskSpec on a real,
    non-dry-run executor. The executor routes ACCEPTANCE_RUN to its handler.
    Local subprocess only (matrix commands are trivial allowlisted shell) —
    hermetic, no LLM/transport construction on this path.

    Round-2 robustness (test-adversary FIX 3): plant a trivially-passing
    ``acceptance_tests.py`` into the task's ``output_dir`` BEFORE dispatch, keyed
    off the payload. If the implementer correctly places the matrix branch BEFORE
    the test-file guard, this file is simply ignored and the matrix runs as
    designed (green post-impl). If the matrix branch is WRONGLY placed AFTER the
    guard, the legacy pytest path runs and writes a 10-key file with NO ``matrix``
    key — so the matrix tests fail on the absent ``matrix`` key (a meaningful
    ordering bug) rather than on an indistinguishable FileNotFoundError. At HEAD
    (no matrix handling at all) the same legacy path runs → missing ``matrix`` key
    → clean RED-by-design.
    """
    from pathlib import Path

    output_dir = Path(task.payload["output_dir"])
    _plant_passing_test_file(output_dir)
    return OpenClawExecutor(dry_run=False).execute(task)


def _read_results(tmp_path):
    """Load the written acceptance_results.json.

    With a planted ``acceptance_tests.py`` (see ``_run``), the handler always
    writes this file: today the legacy 10-key file (no ``matrix`` key);
    post-impl the matrix file with a ``matrix`` array.
    """
    path = tmp_path / "acceptance_results.json"
    return json.loads(path.read_text())


def _names(data):
    return [e["name"] for e in data["matrix"]]


def _entry(data, name):
    for e in data["matrix"]:
        if e["name"] == name:
            return e
    raise AssertionError(f"matrix entry {name!r} missing; got {_names(data)!r}")


# ===========================================================================
# C1 — Opt-in / byte-identical legacy default (NO matrix declared)  [SHIELD]
# ===========================================================================


def test_c1_no_matrix_legacy_ten_keys(tmp_path):
    """C1: a no-matrix acceptance_run emits EXACTLY today's 10-key legacy
    acceptance_results.json with NO "matrix" key and no key reordering.

    Contract: "contains EXACTLY the 10 legacy keys ... assert set(data.keys())
    == {those 10}" and "`"matrix" not in data`". Uses the dry-run mock (C1
    option b), which writes a fixed passing legacy result.

    Expected-today: PASS (shield). With a planted acceptance_tests.py the dry-run
    shortcut (which sits AFTER the test_file_not_found guard) writes today's exact
    10-key legacy file and never adds a matrix key — green now AND post-impl (the
    implementer does not touch the legacy path).
    """
    # The dry-run shortcut sits AFTER the test_file_not_found guard, so the
    # handler needs a real acceptance_tests.py in output_dir to reach it.
    _plant_passing_test_file(tmp_path)

    # No acceptance_matrix key at all in the payload.
    task = TaskSpec(
        type=TaskType.ACCEPTANCE_RUN,
        payload={"output_dir": str(tmp_path), "working_dir": str(tmp_path)},
    )
    result = OpenClawExecutor(dry_run=True).execute(task)

    assert result.state == TaskState.SUCCESS

    data = _read_results(tmp_path)
    assert set(data.keys()) == LEGACY_KEYS, (
        f"legacy file must have exactly the 10 keys; got {sorted(data.keys())}"
    )
    assert "matrix" not in data, "no per-entry array may be added in the legacy case"
    assert data["status"] == "pass"


# ===========================================================================
# C2 — All-pass matrix → aggregate GREEN, declared order  [RED]
# ===========================================================================


def test_c2_all_pass_matrix_aggregate_green(tmp_path):
    """C2: an N-entry matrix whose commands ALL exit 0 yields an aggregate pass,
    a "matrix" array of exactly N entries each pass/exit 0, IN DECLARED ORDER.

    Contract: status=="pass", passed==N, failed==0, errors==0, total==N,
    pass_rate==1.0, top-level exit_code==0; matrix is a list of exactly N
    entries; every entry status=="pass" & exit_code==0; declared order
    [e["name"] for e ...] == ["a","b"]; TaskResult.state == SUCCESS.

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    runs legacy pytest and writes a 10-key file with NO "matrix" key, so the
    data["matrix"] assertions raise KeyError → clean RED-by-design on the absent
    matrix key (not a missing-file error).
    """
    matrix = [
        {"name": "a", "command": "true"},
        {"name": "b", "command": "echo ok"},
    ]
    result = _run(_matrix_task(tmp_path, matrix, allowed_commands=["true", "echo"]))

    data = _read_results(tmp_path)

    assert data["status"] == "pass"
    assert data["passed"] == 2
    assert data["failed"] == 0
    assert data["errors"] == 0
    assert data["total"] == 2
    assert data["pass_rate"] == 1.0
    assert data["exit_code"] == 0

    assert isinstance(data["matrix"], list)
    assert len(data["matrix"]) == 2
    for e in data["matrix"]:
        assert e["status"] == "pass"
        assert e["exit_code"] == 0
    # Declared order — no sorting, no reordering.
    assert _names(data) == ["a", "b"]

    assert result.state == TaskState.SUCCESS


# ===========================================================================
# C3 — Run-all-then-report (NOT fail-fast)  [RED]  (load-bearing)
# ===========================================================================


def test_c3_run_all_then_report_not_fail_fast(tmp_path):
    """C3 (load-bearing anti-fail-fast): with a failing entry in the MIDDLE, all
    3 entries are recorded in order and the entry AFTER the failure still ran
    and passed.

    Contract: status=="fail", failed==1, passed==2, total==3, errors==0; matrix
    has exactly 3 entries in order ["a","b","c"]; entry b status=="fail" &
    exit_code==1; entry c PRESENT, status=="pass", exit_code==0, output contains
    "c"; top-level exit_code==1; TaskResult.state == FAILED. "A test that finds
    only 2 entries, or c missing, MUST fail."

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    writes a 10-key legacy file with NO "matrix" key → entry "c" never recorded;
    fails on the missing matrix key.
    """
    matrix = [
        {"name": "a", "command": "true"},
        {"name": "b", "command": "false"},
        {"name": "c", "command": "echo c"},
    ]
    result = _run(
        _matrix_task(tmp_path, matrix, allowed_commands=["true", "false", "echo"])
    )

    data = _read_results(tmp_path)

    assert data["status"] == "fail"
    assert data["failed"] == 1
    assert data["passed"] == 2
    assert data["total"] == 3
    assert data["errors"] == 0
    # Aggregate top-level exit_code is 1 for the failing case (not any single
    # entry's code) — per behavioral.md "top-level exit_code == 0 iff all passed
    # else 1".
    assert data["exit_code"] == 1

    # Exactly 3 ordered entries — no abort after the middle failure.
    assert len(data["matrix"]) == 3
    assert _names(data) == ["a", "b", "c"]

    b = _entry(data, "b")
    assert b["status"] == "fail"
    assert b["exit_code"] == 1

    # The decisive assertion: the entry AFTER the failing one ran and passed.
    c = _entry(data, "c")
    assert c["status"] == "pass"
    assert c["exit_code"] == 0
    assert "c" in c["output"]

    assert result.state == TaskState.FAILED


# ===========================================================================
# C4 — Per-entry output truncation at MAX_OUTPUT_BYTES  [RED]
# ===========================================================================


def test_c4_per_entry_output_truncation(tmp_path):
    """C4: an entry emitting >1MB of stdout has its recorded output truncated to
    <= MAX_OUTPUT_BYTES + 19 and ending with the exact marker; the entry itself
    still passes.

    Contract: len(output) <= 1048576 + 19 (== 1048595); output.endswith(
    "\\n[OUTPUT TRUNCATED]"); entry status=="pass" & exit_code==0; aggregate
    status=="pass". Arithmetic is load-bearing — assert against the exact
    constants, do not hard-code a different cap.

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    writes a 10-key legacy file with NO "matrix" key → data["matrix"][0] raises
    KeyError; fails on the missing matrix key.
    """
    matrix = [{"name": "big", "command": "python3 -c \"print('x'*2000000)\""}]
    result = _run(_matrix_task(tmp_path, matrix, allowed_commands=["python3"]))

    data = _read_results(tmp_path)

    entry = data["matrix"][0]
    assert entry["name"] == "big"

    output = entry["output"]
    assert len(output) <= MAX_OUTPUT_BYTES + TRUNCATION_MARKER_LEN  # <= 1048595
    assert output.endswith(TRUNCATION_MARKER)

    # The command succeeds; only its output was truncated.
    assert entry["status"] == "pass"
    assert entry["exit_code"] == 0
    assert data["status"] == "pass"

    assert result.state == TaskState.SUCCESS


# ===========================================================================
# C5 — Allowlist enforcement (security) + others still run  [RED]
# ===========================================================================


def test_c5_allowlist_block_others_still_run(tmp_path):
    """C5: an entry whose executable is not in allowed_commands is blocked BEFORE
    any subprocess runs (exit_code -1, "[SECURITY]" output), while the other
    allowlisted entries still run.

    Contract: entry bad status=="fail", exit_code==-1, "[SECURITY]" in output,
    NOT executed; entries ok AND ok2 both status=="pass" & exit_code==0;
    aggregate status=="fail", failed==1, passed==2, total==3;
    TaskResult.state == FAILED. (curl is absent from allowed_commands — blocked
    by the allowlist; the [SECURITY] prefix holds regardless of denylist vs
    allowlist wording.)

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    writes a 10-key legacy file with NO "matrix" key → _entry() cannot index
    data["matrix"]; fails on the missing matrix key.
    """
    matrix = [
        {"name": "ok", "command": "true"},
        {"name": "bad", "command": "curl http://x"},  # curl NOT allowlisted
        {"name": "ok2", "command": "echo y"},
    ]
    result = _run(_matrix_task(tmp_path, matrix, allowed_commands=["true", "echo"]))

    data = _read_results(tmp_path)

    bad = _entry(data, "bad")
    assert bad["status"] == "fail"
    assert bad["exit_code"] == -1
    assert "[SECURITY]" in bad["output"]

    # The blocked entry does not abort the others.
    ok = _entry(data, "ok")
    assert ok["status"] == "pass"
    assert ok["exit_code"] == 0
    ok2 = _entry(data, "ok2")
    assert ok2["status"] == "pass"
    assert ok2["exit_code"] == 0

    assert data["status"] == "fail"
    assert data["failed"] == 1
    assert data["passed"] == 2
    assert data["total"] == 3

    assert result.state == TaskState.FAILED


def test_c5_shell_false_no_chaining(tmp_path):
    """C5 sub-contract (shell=False): a command containing ';' runs the
    executable ONCE with the ';' as a literal argument — it does NOT chain.

    Contract: entry meta status=="pass", exit_code==0, echo runs once with ';'
    literal; captured output contains the literal "a; echo b"; NOT a security
    failure. Proves shlex split + shell=False, so ;/&&/| never chain.

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    writes a 10-key legacy file with NO "matrix" key → "meta" entry never
    recorded; fails on the missing matrix key.
    """
    matrix = [{"name": "meta", "command": "echo a; echo b"}]
    result = _run(_matrix_task(tmp_path, matrix, allowed_commands=["echo"]))

    data = _read_results(tmp_path)

    meta = _entry(data, "meta")
    assert meta["status"] == "pass"
    assert meta["exit_code"] == 0
    assert "[SECURITY]" not in meta["output"]
    # One echo invocation printing the literal "a; echo b" — no second command.
    assert "a; echo b" in meta["output"]

    assert result.state == TaskState.SUCCESS


# ===========================================================================
# C6 — Aggregate field consistency for the downstream confidence reader  [RED]
# ===========================================================================


def _assert_aggregate_consistent(data):
    """Shared C6 invariants over a matrix-case results dict."""
    # status is exactly pass|fail — never a placeholder like "tests_written".
    assert data["status"] in {"pass", "fail"}, (
        f"status must be pass|fail, never a placeholder; got {data['status']!r}"
    )
    # Presence + types.
    assert isinstance(data["status"], str)
    assert isinstance(data["passed"], int)
    assert isinstance(data["failed"], int)
    assert isinstance(data["errors"], int)
    assert isinstance(data["total"], int)
    assert isinstance(data["pass_rate"], float)
    # Internal consistency.
    assert data["passed"] + data["failed"] + data["errors"] == data["total"]
    assert data["total"] == len(data["matrix"])
    assert data["pass_rate"] == data["passed"] / data["total"]


def test_c6_aggregate_field_consistency_green(tmp_path):
    """C6 (GREEN case): for an all-pass matrix the aggregate is internally
    consistent and status is "pass" with pass_rate == 1.0.

    Contract: status in {pass,fail} (never "tests_written"); passed/failed/
    errors/total ints, pass_rate float; passed+failed+errors==total==len(matrix);
    pass_rate==passed/total; GREEN → status=="pass" & pass_rate==1.0.

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    writes a 10-key legacy file with NO "matrix" key →
    _assert_aggregate_consistent indexes data["matrix"] → KeyError; fails on the
    missing matrix key.
    """
    matrix = [
        {"name": "a", "command": "true"},
        {"name": "b", "command": "echo ok"},
    ]
    _run(_matrix_task(tmp_path, matrix, allowed_commands=["true", "echo"]))

    data = _read_results(tmp_path)
    _assert_aggregate_consistent(data)
    assert data["status"] == "pass"
    assert data["pass_rate"] == 1.0


def test_c6_aggregate_field_consistency_red(tmp_path):
    """C6 (RED case): for a mixed matrix the aggregate is internally consistent
    and status is "fail" with 0.0 <= pass_rate < 1.0 (here exactly 2/3).

    Contract: same invariants as the green case; RED → status=="fail" and
    0.0 <= pass_rate < 1.0 (C3's matrix → 2/3).

    Expected-today: FAIL (red). _run plants acceptance_tests.py → today's handler
    writes a 10-key legacy file with NO "matrix" key → fails on the missing
    matrix key.
    """
    matrix = [
        {"name": "a", "command": "true"},
        {"name": "b", "command": "false"},
        {"name": "c", "command": "echo c"},
    ]
    _run(_matrix_task(tmp_path, matrix, allowed_commands=["true", "false", "echo"]))

    data = _read_results(tmp_path)
    _assert_aggregate_consistent(data)
    assert data["status"] == "fail"
    assert 0.0 <= data["pass_rate"] < 1.0
    assert data["pass_rate"] == 2 / 3


# ===========================================================================
# C7 — Empty / declared-but-[] matrix → legacy single-result path  [SHIELD]
# ===========================================================================


def test_c7_empty_list_matrix_routes_to_legacy(tmp_path):
    """C7: a payload that sets acceptance_matrix to an EMPTY list [] is treated
    as "no matrix declared" and routes to the byte-identical legacy path.

    Contract: acceptance_results.json has EXACTLY the 10 legacy keys and
    "matrix" not in data; NOT a vacuous all-pass matrix; status reflects the
    single-pytest/dry-run run ("pass" here). "A test that finds a "matrix" key
    (even []) for the empty-list input MUST fail." Uses dry_run (C7 option).

    Expected-today: PASS (shield). With a planted acceptance_tests.py the handler
    clears its test_file_not_found guard and (dry_run) writes the legacy 10-key
    file; today's handler ignores acceptance_matrix entirely, so empty-[] behaves
    exactly like the legacy dry-run path. Stays a true GREEN shield post-impl
    (empty-[] must route to legacy, not a vacuous matrix).
    """
    # The dry-run shortcut sits AFTER the test_file_not_found guard, so the
    # handler needs a real acceptance_tests.py in output_dir to reach it.
    _plant_passing_test_file(tmp_path)

    task = TaskSpec(
        type=TaskType.ACCEPTANCE_RUN,
        payload={
            "output_dir": str(tmp_path),
            "working_dir": str(tmp_path),
            "acceptance_matrix": [],  # declared but empty
        },
    )
    result = OpenClawExecutor(dry_run=True).execute(task)

    assert result.state == TaskState.SUCCESS

    data = _read_results(tmp_path)
    assert set(data.keys()) == LEGACY_KEYS, (
        f"empty-[] must take the legacy 10-key path; got {sorted(data.keys())}"
    )
    # The decisive assertion: NO matrix key — not even an empty one.
    assert "matrix" not in data, "empty-[] must NOT produce a (vacuous) matrix array"
    assert data["status"] == "pass"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
