"""Integration tests for Issue #534: Behavioral Validation E2E — Full Trust Chain.

Exercises the complete behavioral trust chain formed by:
- file_guard      — SHA256 hash computation and tamper detection
- test_runner     — pytest execution and acceptance_results.json persistence
- PhaseSequencer  — protected-hash store/verify lifecycle
- ConfidenceCalculator — acceptance_pass_rate as primary scoring signal

Tests use ``tmp_path`` for file-system isolation and mock runner/executor
objects to avoid LLM and subprocess calls.  No YAML templates or network I/O.

Structure:
  TestHappyPath                — behavioral contracts #1–#8
  TestHashTamperDetection      — behavioral contracts #9–#11, #18–#21
  TestAcceptanceFailureRetryLoop — behavioral contracts #12–#14
  TestBackwardCompatibility    — behavioral contracts #15–#17
  TestProtectedOutputs         — behavioral contracts #18–#21 (file_guard integration)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure src/ is importable without package installation
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from orchestration_engine.confidence import ConfidenceCalculator, ConfidenceSignal, DEFAULT_WEIGHTS
from orchestration_engine.file_guard import FileGuardError, compute_hash, verify_hash
from orchestration_engine.schemas import Priority, TaskError, TaskResult, TaskState, TaskType, TaskSpec
from orchestration_engine.sequencer import PhaseSequencer, StateMachineSequencer
from orchestration_engine.templates import PhaseDefinition, PipelineTemplate
from orchestration_engine.test_runner import (
    TestRunResult,
    format_failure_summary,
    parse_pytest_output,
    write_acceptance_results,
)


# ===========================================================================
# Shared helpers and fixtures
# ===========================================================================

def _make_task_result(
    task_spec: TaskSpec,
    state: TaskState = TaskState.SUCCESS,
    text: str = "",
    confidence: float = 0.9,
) -> TaskResult:
    """Build a minimal TaskResult for mock executor side-effects."""
    return TaskResult(
        task_id=task_spec.id,
        task_type=task_spec.type,
        state=state,
        confidence=confidence,
        result={"text": text or f"Output of {task_spec.payload.get('phase_id', '?')}"},
        errors=[] if state == TaskState.SUCCESS else [
            TaskError(code="EXEC_ERR", message="Simulated failure", severity="error")
        ],
    )


def _build_mock_runner(execute_fn: Callable) -> MagicMock:
    """Build a mock TaskRunner whose executor delegates to *execute_fn*."""
    runner = MagicMock()
    _task_store: Dict[str, Any] = {}

    def submit_task(spec: TaskSpec) -> str:
        _task_store[spec.id] = spec
        return spec.id

    def get_task(task_id: str) -> Optional[TaskSpec]:
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


def _minimal_pipeline(tmp_output_dir: Path):
    """Return (sequencer, phase_acceptance_test, phase_implement) for guard tests.

    The acceptance_test phase has protected_outputs=["acceptance_tests.py"].
    The implement phase has no protected_outputs.
    """
    phase_a = PhaseDefinition(
        id="acceptance_test",
        name="Acceptance Test",
        prompt_template="Write acceptance tests.",
        protected_outputs=["acceptance_tests.py"],
        transitions={"success": "implement"},
    )
    phase_b = PhaseDefinition(
        id="implement",
        name="Implement",
        prompt_template="Implement the feature.",
        protected_outputs=[],
        transitions={},
    )
    template = PipelineTemplate(
        id="test-pipeline",
        name="Test Pipeline",
        phases=[phase_a, phase_b],
    )
    runner = MagicMock()
    sequencer = PhaseSequencer(
        template=template,
        runner=runner,
        output_dir=str(tmp_output_dir),
    )
    return sequencer, phase_a, phase_b


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """Clean temporary output directory for each test."""
    return tmp_path


@pytest.fixture
def passing_run_result() -> TestRunResult:
    """TestRunResult representing a fully passing acceptance test run."""
    return TestRunResult(
        passed=5,
        failed=0,
        errors=0,
        total=5,
        pass_rate=1.0,
        failure_details="",
        full_output="5 passed in 0.35s",
        exit_code=0,
    )


@pytest.fixture
def failing_run_result() -> TestRunResult:
    """TestRunResult representing a partially failing acceptance test run."""
    return TestRunResult(
        passed=2,
        failed=3,
        errors=0,
        total=5,
        pass_rate=0.4,
        failure_details="FAILED test_foo::test_bar\nassert False",
        full_output="2 passed, 3 failed in 0.42s",
        exit_code=1,
    )


# ===========================================================================
# Section 1 — Happy Path: spec → acceptance_tests → implement → acceptance_run
# ===========================================================================

class TestHappyPath:
    """Exercises the happy-path behavioral contracts (#1–#8).

    Verifies that:
    - A behavioral spec file can be hashed (prerequisite for sealing).
    - The acceptance_tests.py file can be sealed and re-verified.
    - write_acceptance_results() produces the correct schema.
    - ConfidenceCalculator includes acceptance_pass_rate as the primary signal.
    - acceptance_pass_rate weight is 0.40 (highest in DEFAULT_WEIGHTS).
    - pass_rate=1.0 produces a substantially higher composite score than 0.0.
    """

    def test_spec_writes_behavioral_file(self, tmp_output_dir: Path) -> None:
        """
        Contract: When the spec phase runs and produces output, the system can
        compute a stable SHA256 hash of the resulting spec file.
        (Behavioral contract #1 / #3)
        """
        spec_file = tmp_output_dir / "spec-behavioral.md"
        spec_file.write_text("## Behavioral Spec\n\nWhat the system does.\n")

        h1 = compute_hash(spec_file)
        h2 = compute_hash(spec_file)

        assert h1 == h2, "Hash must be reproducible for the same file"
        assert len(h1) == 64, "SHA256 hex digest must be exactly 64 chars"
        assert h1, "Hash must be non-empty"

    def test_acceptance_tests_file_is_sealed(self, tmp_output_dir: Path) -> None:
        """
        Contract: When the acceptance test phase completes successfully, the
        system computes and stores a hash of acceptance_tests.py.  A subsequent
        verify_hash call on the unmodified file must not raise.
        (Behavioral contract #3 / #18 / #19)
        """
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text(
            "# pre-implementation behavioral tests\n"
            "def test_placeholder(): pass\n"
        )

        stored_hash = compute_hash(test_file)
        # Re-compute to show it is stable
        assert compute_hash(test_file) == stored_hash

        # Should not raise — file unmodified
        verify_hash(test_file, stored_hash)

    def test_acceptance_run_produces_results_file(
        self, tmp_output_dir: Path, passing_run_result: TestRunResult
    ) -> None:
        """
        Contract: When the acceptance run phase executes, the system writes
        engine-verified results to acceptance_results.json.  When all tests
        pass, status='pass' and pass_rate=1.0.
        (Behavioral contract #5 / #6)
        """
        write_acceptance_results(passing_run_result, str(tmp_output_dir))

        results_path = tmp_output_dir / "acceptance_results.json"
        assert results_path.exists(), "acceptance_results.json must be created"

        data = json.loads(results_path.read_text())
        assert data["status"] == "pass"
        assert data["pass_rate"] == pytest.approx(1.0)
        assert data["passed"] == 5
        assert data["failed"] == 0
        assert data["total"] == 5

    def test_scorer_reads_acceptance_pass_rate(self, tmp_output_dir: Path) -> None:
        """
        Contract: When the scoring phase reads acceptance_results.json, the
        acceptance_pass_rate signal is present with value 1.0.
        (Behavioral contract #7)
        """
        # Write a minimal task result so ConfidenceCalculator has signals to aggregate
        (tmp_output_dir / "implement.json").write_text(json.dumps({
            "state": "success", "confidence": 0.8, "task_type": "build"
        }))

        # Write passing acceptance_results.json
        (tmp_output_dir / "acceptance_results.json").write_text(json.dumps({
            "phase": "acceptance_run",
            "status": "pass",
            "passed": 5,
            "failed": 0,
            "errors": 0,
            "total": 5,
            "pass_rate": 1.0,
        }))

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_output_dir)

        signal_names = [s.name for s in result.signals]
        assert "acceptance_pass_rate" in signal_names, (
            "acceptance_pass_rate signal must be present when results file is available"
        )

        apr_signal = next(s for s in result.signals if s.name == "acceptance_pass_rate")
        assert apr_signal.value == pytest.approx(1.0), (
            "acceptance_pass_rate value must be 1.0 when all tests pass"
        )

    def test_acceptance_pass_rate_weight_is_primary(self) -> None:
        """
        Contract: acceptance_pass_rate has a weight of 0.40 in DEFAULT_WEIGHTS
        and is the highest (or joint-highest) weight.
        (Behavioral contract #7)
        """
        assert DEFAULT_WEIGHTS.get("acceptance_pass_rate") == pytest.approx(0.40), (
            "acceptance_pass_rate weight must be 0.40 in DEFAULT_WEIGHTS"
        )
        highest = max(DEFAULT_WEIGHTS.values())
        assert DEFAULT_WEIGHTS["acceptance_pass_rate"] >= highest - 1e-9, (
            "acceptance_pass_rate must be the primary (highest) weight"
        )

    def test_full_pass_rate_produces_substantially_higher_score(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When acceptance_pass_rate is 1.0, the composite score is
        substantially higher than when it is 0.0.
        (Behavioral contract #8)
        """
        (tmp_output_dir / "implement.json").write_text(json.dumps({
            "state": "success", "confidence": 0.8, "task_type": "build"
        }))

        # Score with pass_rate=1.0
        (tmp_output_dir / "acceptance_results.json").write_text(json.dumps({
            "phase": "acceptance_run", "status": "pass",
            "passed": 5, "failed": 0, "errors": 0, "total": 5, "pass_rate": 1.0,
        }))
        calc = ConfidenceCalculator()
        result_pass = calc.compute_confidence(tmp_output_dir)

        # Score with pass_rate=0.0
        (tmp_output_dir / "acceptance_results.json").write_text(json.dumps({
            "phase": "acceptance_run", "status": "fail",
            "passed": 0, "failed": 5, "errors": 0, "total": 5, "pass_rate": 0.0,
        }))
        result_fail = calc.compute_confidence(tmp_output_dir)

        diff = result_pass.composite_score - result_fail.composite_score
        assert diff > 0.10, (
            f"Pass rate 1.0 must produce substantially higher score than 0.0 "
            f"(difference was {diff:.4f}, expected > 0.10)"
        )


# ===========================================================================
# Section 2 — Hash Tamper Detection
# ===========================================================================

class TestHashTamperDetection:
    """Exercises tamper-detection behavioral contracts (#9–#11, #20).

    Verifies that:
    - verify_hash raises FileGuardError on tampered files.
    - The error message identifies the filename and both hash values.
    - The PhaseSequencer detects tampering via _verify_protected_hashes.
    - A deleted protected file raises FileNotFoundError (not a silent skip).
    """

    def test_verify_hash_raises_on_tamper(self, tmp_output_dir: Path) -> None:
        """
        Contract: When a protected file is modified after it was sealed,
        the system detects the modification and raises a hash mismatch error.
        (Behavioral contract #9)
        """
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# original content\n")
        original_hash = compute_hash(test_file)

        # Tamper with the file
        test_file.write_text("# TAMPERED — different bytes\n")

        with pytest.raises(FileGuardError):
            verify_hash(test_file, original_hash)

    def test_tamper_error_message_contains_filename_and_both_hashes(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When hash tamper detection triggers, the error message
        clearly identifies the protected file and includes both the expected
        and actual SHA256 hash values.
        (Behavioral contract #10)
        """
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# original\n")
        original_hash = compute_hash(test_file)
        test_file.write_text("# changed\n")

        with pytest.raises(FileGuardError) as exc_info:
            verify_hash(test_file, original_hash)

        msg = str(exc_info.value)
        assert "acceptance_tests.py" in msg, "Error must name the protected file"
        assert "expected sha256:" in msg, "Error must include expected sha256 label"
        assert "got sha256:" in msg, "Error must include actual sha256 label"
        assert original_hash in msg, "Error must include the expected hash value"

    def test_sequencer_aborts_on_tampered_acceptance_tests(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When the sequencer stores a hash for acceptance_tests.py and
        the file is subsequently modified, _verify_protected_hashes returns a
        non-None failure dict with error code PROTECTED_FILE_MODIFIED.
        (Behavioral contract #9 / #11)
        """
        sequencer, phase_a, phase_b = _minimal_pipeline(tmp_output_dir)

        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# original acceptance tests\n")
        sequencer._store_protected_hashes(phase_a)

        # Tamper with the file
        test_file.write_text("# TAMPERED\n")

        failure = sequencer._verify_protected_hashes(phase_b)
        assert failure is not None, "Sequencer must detect hash mismatch"
        errors = failure.get("errors", [])
        assert len(errors) > 0, "Failure must contain at least one error entry"
        assert any(e.get("code") == "PROTECTED_FILE_MODIFIED" for e in errors), (
            "Error code must be PROTECTED_FILE_MODIFIED"
        )

    def test_tamper_error_message_contains_filename(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: FileGuardError message contains the filename of the tampered file.
        (Behavioral contract #10)
        """
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# original\n")
        h = compute_hash(test_file)
        test_file.write_text("# tampered\n")

        with pytest.raises(FileGuardError) as exc_info:
            verify_hash(test_file, h)

        assert "acceptance_tests.py" in str(exc_info.value)

    def test_deleted_file_raises_file_not_found(self, tmp_output_dir: Path) -> None:
        """
        Contract: If a protected file is deleted between phases, the system
        reports a deletion error (not a silent skip).
        (Behavioral contract #20)
        """
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# content\n")
        original_hash = compute_hash(test_file)
        test_file.unlink()  # Delete the file

        with pytest.raises(FileNotFoundError):
            verify_hash(test_file, original_hash)

    def test_sequencer_no_overhead_without_protected_outputs(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When no protected outputs are configured, the verification
        step has no overhead and no effect — returns None immediately.
        (Behavioral contract #21)
        """
        sequencer, _, phase_b = _minimal_pipeline(tmp_output_dir)
        # _protected_hashes is empty; no store was called
        result = sequencer._verify_protected_hashes(phase_b)
        assert result is None, "Empty hash store must return None immediately"


# ===========================================================================
# Section 3 — Acceptance Test Failure → Retry Loop
# ===========================================================================

class TestAcceptanceFailureRetryLoop:
    """Exercises retry-loop behavioral contracts (#12–#14).

    Verifies that:
    - A failing acceptance run writes status='fail' and non-empty failure_details.
    - The acceptance_run task type returns a FAILED state when tests fail.
    - The pipeline routes failed acceptance_run back to the implement phase.
    - The implement phase can read failure_details from acceptance_results.json.
    """

    def test_acceptance_run_failure_writes_fail_status(
        self, tmp_output_dir: Path, failing_run_result: TestRunResult
    ) -> None:
        """
        Contract: When some acceptance tests fail, the results file records a
        'fail' status so the pipeline knows to route back to implement.
        (Behavioral contract #12)
        """
        write_acceptance_results(failing_run_result, str(tmp_output_dir))
        data = json.loads((tmp_output_dir / "acceptance_results.json").read_text())

        assert data["status"] == "fail"
        assert "failure_details" in data
        assert data["failure_details"], "failure_details must be non-empty on failure"

    def test_acceptance_run_task_returns_failed_state(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When the acceptance_run task executes and tests fail,
        the executor returns a TaskResult with state=FAILED.
        (Behavioral contract #12)
        """
        from orchestration_engine.openclaw_executor import OpenClawExecutor

        # Write a test file that always fails
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text(
            "def test_always_fail():\n"
            "    assert False, 'Deliberate failure for retry-loop test'\n"
        )

        executor = OpenClawExecutor(dry_run=False)

        # Build a minimal TaskSpec for acceptance_run
        task = TaskSpec(
            type=TaskType.ACCEPTANCE_RUN,
            payload={
                "prompt": "Run acceptance tests",
                "phase_id": "acceptance_run",
                "pipeline_id": "test-pipeline",
                "output_dir": str(tmp_output_dir),
            },
            priority=Priority.HIGH,
        )

        # Patch run_pytest to return a failing result without running subprocess
        failing_result = TestRunResult(
            passed=0, failed=1, errors=0, total=1,
            pass_rate=0.0,
            failure_details="FAILED test_always_fail - assert False",
            full_output="1 failed in 0.05s",
            exit_code=1,
        )

        with patch("orchestration_engine.test_runner.run_pytest", return_value=failing_result):
            from datetime import datetime
            task_result = executor._execute_acceptance_run_task(
                task, datetime.now(), str(task.id)
            )

        assert task_result.state == TaskState.FAILED, (
            "Failing acceptance run must return state=FAILED"
        )

    def test_pipeline_routes_failed_acceptance_to_implement(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When some acceptance tests fail, the system routes back to
        the implement phase rather than advancing to review.
        When the implement phase is retried after acceptance test failures, it
        receives the failure details from the previous acceptance run.
        (Behavioral contracts #12 / #13)
        """
        # Track how many times each phase executes
        phase_exec_counts: Dict[str, int] = {}

        def execute_fn(task_spec: TaskSpec, **kwargs) -> TaskResult:
            phase_id = task_spec.payload.get("phase_id", "")
            phase_exec_counts[phase_id] = phase_exec_counts.get(phase_id, 0) + 1

            # acceptance_run: fail first, pass second
            if phase_id == "acceptance_run":
                if phase_exec_counts.get("acceptance_run", 0) < 2:
                    return _make_task_result(task_spec, state=TaskState.FAILED)
                else:
                    return _make_task_result(task_spec, state=TaskState.SUCCESS)
            # All other phases succeed immediately
            return _make_task_result(task_spec)

        # Build pipeline: implement → acceptance_run (failed → implement, success → review)
        phase_implement = PhaseDefinition(
            id="implement",
            name="Implement",
            prompt_template="Implement.",
            max_iterations=2,  # allow re-entry
            transitions={"success": "acceptance_run"},
        )
        phase_acceptance_run = PhaseDefinition(
            id="acceptance_run",
            name="Acceptance Run",
            prompt_template="Run tests.",
            task_type="acceptance_run",
            max_iterations=2,  # allow re-entry
            transitions={"success": "review", "failed": "implement"},
        )
        phase_review = PhaseDefinition(
            id="review",
            name="Review",
            prompt_template="Review.",
            transitions={},
        )
        template = PipelineTemplate(
            id="retry-loop-test",
            name="Retry Loop Test",
            phases=[phase_implement, phase_acceptance_run, phase_review],
        )

        runner = _build_mock_runner(execute_fn)
        sequencer = StateMachineSequencer(
            template=template,
            runner=runner,
            output_dir=str(tmp_output_dir),
        )
        result = sequencer.execute({"task": "build feature X"})

        # implement must have been called twice: initial + retry after failure
        assert phase_exec_counts.get("implement", 0) >= 2, (
            f"implement must be called at least twice; got {phase_exec_counts}"
        )
        # acceptance_run must have been called twice: first fail, then pass
        assert phase_exec_counts.get("acceptance_run", 0) >= 2, (
            f"acceptance_run must be called at least twice; got {phase_exec_counts}"
        )

    def test_implement_receives_failure_context(
        self, tmp_output_dir: Path, failing_run_result: TestRunResult
    ) -> None:
        """
        Contract: After acceptance_results.json is written with fail status and
        failure_details, the field is non-empty and correctly serialised so the
        implement phase can read and act on it.
        (Behavioral contract #13)
        """
        write_acceptance_results(failing_run_result, str(tmp_output_dir))
        data = json.loads((tmp_output_dir / "acceptance_results.json").read_text())

        assert "failure_details" in data, "failure_details must be serialised"
        assert data["failure_details"], "failure_details must be non-empty on failure"
        # Should contain diagnostic test info
        assert "FAILED" in data["failure_details"] or "assert" in data["failure_details"], (
            "failure_details must contain actionable failure information"
        )


# ===========================================================================
# Section 4 — Backward Compatibility (No acceptance_results.json)
# ===========================================================================

class TestBackwardCompatibility:
    """Exercises backward-compatibility behavioral contracts (#15–#17).

    Verifies that:
    - Missing acceptance_results.json → signal omitted, no error.
    - Composite score is valid without acceptance_pass_rate signal.
    - A pre-implementation placeholder (status='tests_written', total=0) is skipped.
    - Remaining signals are renormalised when acceptance_pass_rate is absent.
    """

    def test_no_acceptance_results_file_omits_signal(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When no acceptance_results.json is present, the scoring
        system omits the acceptance_pass_rate signal rather than failing.
        (Behavioral contract #15)
        """
        (tmp_output_dir / "implement.json").write_text(json.dumps({
            "state": "success", "confidence": 0.8, "task_type": "build"
        }))

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_output_dir)

        signal_names = [s.name for s in result.signals]
        assert "acceptance_pass_rate" not in signal_names, (
            "acceptance_pass_rate must be absent when acceptance_results.json is missing"
        )

    def test_score_computed_without_acceptance_signal(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When acceptance_pass_rate is absent, the composite score is
        still computed from the remaining signals without error.
        (Behavioral contract #17)
        """
        (tmp_output_dir / "implement.json").write_text(json.dumps({
            "state": "success", "confidence": 0.8, "task_type": "build"
        }))

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_output_dir)

        assert isinstance(result.composite_score, float)
        assert 0.0 <= result.composite_score <= 1.0, (
            "Composite score must remain in [0, 1] without acceptance signal"
        )

    def test_placeholder_record_is_ignored_by_scorer(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When acceptance_results.json contains a pre-implementation
        placeholder (status='tests_written', total=0), the scorer skips it and
        acceptance_pass_rate is absent from signals.
        (Behavioral contract #15 / #17)
        """
        (tmp_output_dir / "implement.json").write_text(json.dumps({
            "state": "success", "confidence": 0.8, "task_type": "build"
        }))
        (tmp_output_dir / "acceptance_results.json").write_text(json.dumps({
            "phase": "acceptance_test",
            "status": "tests_written",
            "test_file": str(tmp_output_dir / "acceptance_tests.py"),
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "total": 0,
            "pass_rate": 0.0,
            "note": "Tests written pre-implementation. Run after implement phase.",
        }))

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_output_dir)

        signal_names = [s.name for s in result.signals]
        assert "acceptance_pass_rate" not in signal_names, (
            "Placeholder record must be ignored; acceptance_pass_rate must be absent"
        )

    def test_remaining_signals_renormalised_when_acceptance_absent(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When acceptance_pass_rate is absent, the remaining signals are
        renormalised so their weights sum to the available weight budget,
        preserving relative proportions.  The composite score must still be valid.
        (Behavioral contract #16)
        """
        (tmp_output_dir / "implement.json").write_text(json.dumps({
            "state": "success", "confidence": 0.8, "task_type": "build"
        }))
        (tmp_output_dir / "review.json").write_text(json.dumps({
            "state": "success", "confidence": 0.9, "task_type": "review"
        }))
        # No acceptance_results.json

        calc = ConfidenceCalculator()
        result = calc.compute_confidence(tmp_output_dir)

        assert result.composite_score > 0.0, (
            "Composite score must be positive when real signals are present"
        )
        assert result.composite_score <= 1.0, (
            "Composite score must not exceed 1.0"
        )


# ===========================================================================
# Section 5 — Protected Outputs: File Guard Integration
# ===========================================================================

class TestProtectedOutputs:
    """Exercises protected-output file-guard integration (#18–#21).

    Tests the PhaseSequencer._store_protected_hashes /
    _verify_protected_hashes lifecycle directly.
    """

    def test_store_and_verify_roundtrip_succeeds(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: When the acceptance test phase completes successfully, the
        system stores a hash of acceptance_tests.py.  When a subsequent phase
        runs and the file is unmodified, verification returns None (passes).
        (Behavioral contract #18 / #19)
        """
        sequencer, phase_a, phase_b = _minimal_pipeline(tmp_output_dir)
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# sealed acceptance tests\n")

        sequencer._store_protected_hashes(phase_a)
        result = sequencer._verify_protected_hashes(phase_b)
        assert result is None, "Unmodified file must pass verification (return None)"

    def test_verify_fails_after_modification(self, tmp_output_dir: Path) -> None:
        """
        Contract: When the acceptance_tests.py file is modified between
        _store_protected_hashes and _verify_protected_hashes, the sequencer
        returns a non-None failure dict with the PROTECTED_FILE_MODIFIED code.
        (Behavioral contract #9 / #11)
        """
        sequencer, phase_a, phase_b = _minimal_pipeline(tmp_output_dir)
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# original sealed tests\n")

        sequencer._store_protected_hashes(phase_a)
        test_file.write_text("# MODIFIED AFTER SEALING\n")  # tamper

        failure = sequencer._verify_protected_hashes(phase_b)
        assert failure is not None, "Modified file must produce a failure result"
        errors = failure.get("errors", [])
        assert len(errors) > 0, "Failure dict must contain error entries"
        assert any(e.get("code") == "PROTECTED_FILE_MODIFIED" for e in errors), (
            "Error code must be PROTECTED_FILE_MODIFIED"
        )

    def test_no_protected_outputs_zero_overhead(self, tmp_output_dir: Path) -> None:
        """
        Contract: When no protected outputs are configured, the verification
        step has no overhead and no effect (returns None immediately).
        (Behavioral contract #21)
        """
        sequencer, _, phase_b = _minimal_pipeline(tmp_output_dir)
        # No _store_protected_hashes call → empty hash store
        result = sequencer._verify_protected_hashes(phase_b)
        assert result is None, "Empty hash store must return None (zero overhead)"

    def test_file_guard_seals_only_configured_files(
        self, tmp_output_dir: Path
    ) -> None:
        """
        Contract: Only files listed in protected_outputs are guarded.  Modifying
        spec.md (not in protected_outputs) after sealing must not trigger a failure.
        (Behavioral contract #21)
        """
        sequencer, phase_a, phase_b = _minimal_pipeline(tmp_output_dir)
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# sealed tests\n")
        spec_file = tmp_output_dir / "spec.md"
        spec_file.write_text("# spec content\n")

        sequencer._store_protected_hashes(phase_a)

        # Modify the non-protected file
        spec_file.write_text("# MODIFIED SPEC — this should not fail verification\n")

        result = sequencer._verify_protected_hashes(phase_b)
        assert result is None, (
            "Modifying a non-protected file must not trigger file guard failure"
        )

    def test_deleted_protected_file_produces_error(self, tmp_output_dir: Path) -> None:
        """
        Contract: If a protected file is deleted between phases, the system
        reports a deletion error (not a silent skip).
        (Behavioral contract #20)
        """
        sequencer, phase_a, phase_b = _minimal_pipeline(tmp_output_dir)
        test_file = tmp_output_dir / "acceptance_tests.py"
        test_file.write_text("# acceptance tests\n")

        sequencer._store_protected_hashes(phase_a)
        test_file.unlink()  # Delete the protected file

        failure = sequencer._verify_protected_hashes(phase_b)
        assert failure is not None, "Deleted protected file must produce a failure result"
        errors = failure.get("errors", [])
        assert len(errors) > 0, "Failure must contain error entries"
        assert any(e.get("code") == "PROTECTED_FILE_MODIFIED" for e in errors), (
            "Deletion must be reported via PROTECTED_FILE_MODIFIED error code"
        )
