"""Integration tests for Issue #331.3 — Confidence Scoring, Routing Dispatch, and Audit Logging.

Tests cover:
- auto_merge path (high confidence >= 0.90)
- human_review path (medium confidence 0.75-0.89)
- reject path (low confidence < 0.60)
- DB persistence (routing_decisions table, signals_json completeness)
- Error resilience (ConfidenceCalculator / RoutingEngine errors are non-fatal)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.db import Database
from orchestration_engine.daemon import _compute_and_dispatch_routing


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Database:
    """Create an in-memory Database and seed a pipeline_run row."""
    db = Database(Path(":memory:"))
    return db


def _seed_run(db: Database, run_id: str, output_dir: Path) -> None:
    """Insert a minimal pipeline_runs row so FK constraints pass."""
    db.insert_pipeline_run({
        "run_id": run_id,
        "template_path": "/tmp/fake_template.yaml",
        "template_id": "fake-template",
        "input_json": "{}",
        "mode": "dry-run",
        "output_dir": str(output_dir),
        "status": "running",
    })


def _call_routing(
    *,
    db: Database,
    run_id: str,
    output_dir: Path,
    final_status: str = "success",
    auto_merge_config: Any = None,
    routing_config: Any = None,
    phase_outputs: Optional[dict] = None,
) -> str:
    """Convenience wrapper around _compute_and_dispatch_routing."""
    return _compute_and_dispatch_routing(
        run_id=run_id,
        output_dir=output_dir,
        db=db,
        auto_merge_config=auto_merge_config,
        routing_config=routing_config,
        scoring_passed=True,
        scoring_score=None,
        phase_outputs=phase_outputs or {},
        final_status=final_status,
    )


# ---------------------------------------------------------------------------
# Fixtures: output directories
# ---------------------------------------------------------------------------


@pytest.fixture
def routing_output_dir_high_confidence(tmp_path: Path) -> Path:
    """Create task JSON files that produce composite confidence >= 0.90.

    Exactly 2 tasks: 1 review + 1 non-review, both with state=success and
    confidence=0.95.  This produces all 4 signals with a composite score of
    ~0.903 (HIGH tier, >= 0.90).

    Breakdown:
        llm_judge (w=0.40):          0.95
        test_pass_rate (w=0.30):     1.00 (1/1 succeeded)
        review_quality (w=0.20):     0.95
        change_complexity (w=0.10):  1/(1+2) ≈ 0.333
        composite ≈ 0.903 → HIGH
    """
    out = tmp_path / "output"
    out.mkdir()
    # 1 non-review task (content type) — triggers test_pass_rate signal
    (out / "phase_0.json").write_text(json.dumps({
        "state": "success",
        "confidence": 0.95,
        "task_type": "content",
    }))
    # 1 review task — triggers llm_judge signal
    (out / "review_phase.json").write_text(json.dumps({
        "state": "success",
        "confidence": 0.95,
        "task_type": "review",
    }))
    return out


@pytest.fixture
def routing_output_dir_medium_confidence(tmp_path: Path) -> Path:
    """Create task JSON files that produce composite confidence in 0.75-0.89 range.

    Two non-review tasks: one succeeded with 0.80 confidence, one succeeded
    without a confidence field.  With 2 tasks and 0.80 avg confidence:
        test_pass_rate (w=0.30):    1.00 (2/2 succeeded)
        review_quality (w=0.20):    0.80 (only phase_a has confidence)
        change_complexity (w=0.10): 1/(1+2) ≈ 0.333
        composite ≈ 0.822 → MEDIUM
    """
    out = tmp_path / "output"
    out.mkdir()
    (out / "phase_a.json").write_text(json.dumps({"state": "success", "confidence": 0.80}))
    (out / "phase_b.json").write_text(json.dumps({"state": "success"}))
    return out


@pytest.fixture
def routing_output_dir_low_confidence(tmp_path: Path) -> Path:
    """Create task JSON files that produce composite confidence < 0.60.

    All 5 tasks failed with no confidence fields — only change_complexity signal
    fires (1/6 ≈ 0.167), well below the 0.60 reject threshold.
    """
    out = tmp_path / "output"
    out.mkdir()
    for i in range(5):
        (out / f"phase_{i}.json").write_text(json.dumps({"state": "failed"}))
    return out


# ---------------------------------------------------------------------------
# Helper: AutoMergeConfig stand-in
# ---------------------------------------------------------------------------


def _make_auto_merge_config(**kwargs: Any) -> Any:
    from orchestration_engine.templates import AutoMergeConfig
    defaults = dict(
        enabled=True,
        min_score=0.85,
        require_approve=False,
        strategy="squash",
        review_phase_id="review",
    )
    defaults.update(kwargs)
    return AutoMergeConfig(**defaults)


# ===========================================================================
# Class: TestRoutingIntegrationAutoMerge
# ===========================================================================


class TestRoutingIntegrationAutoMerge:
    """When confidence >= 0.90 the routing engine selects 'auto_merge'."""

    def test_auto_merge_action_persisted(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """High-confidence run persists a routing_decisions row with action='auto_merge'."""
        db = _make_db(tmp_path)
        run_id = "test-high-conf-001"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        am_cfg = _make_auto_merge_config(require_approve=False)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/auto"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            returned_status = _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=am_cfg,
            )

        row = db.get_routing_decision(run_id)
        assert row is not None
        assert row["action"] == "auto_merge"
        # auto_merge should NOT change the final_status string returned
        assert returned_status == "success"

    def test_auto_merge_pr_called_on_high_confidence(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """When routing selects auto_merge, GitContext.auto_merge_pr is called."""
        db = _make_db(tmp_path)
        run_id = "test-high-conf-002"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        am_cfg = _make_auto_merge_config(require_approve=False, strategy="squash")

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge, \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/auto-merge-branch"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=am_cfg,
            )

        mock_merge.assert_called_once_with(
            run_id=run_id,
            branch_name="feat/auto-merge-branch",
            strategy="squash",
        )

    def test_no_merge_when_no_auto_merge_config(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """High confidence + no auto_merge config → action persisted but no PR merge."""
        db = _make_db(tmp_path)
        run_id = "test-high-conf-no-cfg"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge:
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=None,
            )

        mock_merge.assert_not_called()
        row = db.get_routing_decision(run_id)
        assert row is not None
        assert row["action"] == "auto_merge"


# ===========================================================================
# Class: TestRoutingIntegrationHumanReview
# ===========================================================================


class TestRoutingIntegrationHumanReview:
    """When confidence is in the 0.75-0.89 range the routing engine selects 'human_review'."""

    def test_human_review_action_persisted(
        self, tmp_path: Path, routing_output_dir_medium_confidence: Path
    ) -> None:
        """Medium-confidence run persists a routing_decisions row with action='human_review'."""
        db = _make_db(tmp_path)
        run_id = "test-medium-conf-001"
        _seed_run(db, run_id, routing_output_dir_medium_confidence)

        returned_status = _call_routing(
            db=db,
            run_id=run_id,
            output_dir=routing_output_dir_medium_confidence,
        )

        row = db.get_routing_decision(run_id)
        assert row is not None
        assert row["action"] == "human_review"

    def test_returned_status_is_pending_review_for_medium_confidence(
        self, tmp_path: Path, routing_output_dir_medium_confidence: Path
    ) -> None:
        """_compute_and_dispatch_routing returns 'pending_review' for medium confidence."""
        db = _make_db(tmp_path)
        run_id = "test-medium-conf-002"
        _seed_run(db, run_id, routing_output_dir_medium_confidence)

        returned_status = _call_routing(
            db=db,
            run_id=run_id,
            output_dir=routing_output_dir_medium_confidence,
        )

        assert returned_status == "pending_review"

    def test_no_merge_on_medium_confidence(
        self, tmp_path: Path, routing_output_dir_medium_confidence: Path
    ) -> None:
        """Medium confidence → GitContext.auto_merge_pr is NOT called."""
        db = _make_db(tmp_path)
        run_id = "test-medium-conf-003"
        _seed_run(db, run_id, routing_output_dir_medium_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge:
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_medium_confidence,
                auto_merge_config=_make_auto_merge_config(),
            )

        mock_merge.assert_not_called()


# ===========================================================================
# Class: TestRoutingIntegrationReject
# ===========================================================================


class TestRoutingIntegrationReject:
    """When confidence is below 0.60 the routing engine selects 'reject'."""

    def test_reject_action_persisted(
        self, tmp_path: Path, routing_output_dir_low_confidence: Path
    ) -> None:
        """Low-confidence run persists a routing_decisions row with action='reject'."""
        db = _make_db(tmp_path)
        run_id = "test-low-conf-001"
        _seed_run(db, run_id, routing_output_dir_low_confidence)

        _call_routing(
            db=db,
            run_id=run_id,
            output_dir=routing_output_dir_low_confidence,
        )

        row = db.get_routing_decision(run_id)
        assert row is not None
        assert row["action"] == "reject"

    def test_returned_status_is_rejected_for_low_confidence(
        self, tmp_path: Path, routing_output_dir_low_confidence: Path
    ) -> None:
        """_compute_and_dispatch_routing returns 'rejected' for low confidence."""
        db = _make_db(tmp_path)
        run_id = "test-low-conf-002"
        _seed_run(db, run_id, routing_output_dir_low_confidence)

        returned_status = _call_routing(
            db=db,
            run_id=run_id,
            output_dir=routing_output_dir_low_confidence,
        )

        assert returned_status == "rejected"

    def test_no_merge_on_low_confidence(
        self, tmp_path: Path, routing_output_dir_low_confidence: Path
    ) -> None:
        """Low confidence → GitContext.auto_merge_pr is NOT called."""
        db = _make_db(tmp_path)
        run_id = "test-low-conf-003"
        _seed_run(db, run_id, routing_output_dir_low_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge:
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_low_confidence,
                auto_merge_config=_make_auto_merge_config(),
            )

        mock_merge.assert_not_called()


# ===========================================================================
# Class: TestRoutingDecisionPersisted
# ===========================================================================


class TestRoutingDecisionPersisted:
    """Every routing invocation writes a routing_decisions row to the DB."""

    def test_routing_decision_row_has_correct_fields(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """routing_decisions row has correct run_id, confidence_score, tier_name, action."""
        db = _make_db(tmp_path)
        run_id = "test-persist-001"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/test"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=_make_auto_merge_config(require_approve=False),
            )

        row = db.get_routing_decision(run_id)
        assert row is not None
        assert row["run_id"] == run_id
        assert row["confidence_score"] >= 0.90
        assert row["tier_name"] == "auto_merge"
        assert row["action"] == "auto_merge"
        assert row["justification"] is not None and len(row["justification"]) > 0

    def test_signals_json_is_parsed_dict(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """signals_json in routing_decisions is parsed to a dict by get_routing_decision."""
        db = _make_db(tmp_path)
        run_id = "test-persist-002"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/test"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=_make_auto_merge_config(require_approve=False),
            )

        row = db.get_routing_decision(run_id)
        assert row is not None
        # signals_json should be parsed to a dict by _row_to_dict (json_fields)
        assert isinstance(row["signals_json"], dict), (
            f"signals_json should be a parsed dict, got {type(row['signals_json'])}: "
            f"{row['signals_json']!r}"
        )

    def test_signals_json_contains_expected_signals(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """signals_json dict contains at minimum llm_judge and test_pass_rate signals."""
        db = _make_db(tmp_path)
        run_id = "test-persist-003"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/test"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=_make_auto_merge_config(require_approve=False),
            )

        row = db.get_routing_decision(run_id)
        assert row is not None
        signals = row["signals_json"]
        assert isinstance(signals, dict)
        # High-confidence dir has both review and non-review tasks → both signal types
        assert "llm_judge" in signals, f"Expected 'llm_judge' in {list(signals.keys())}"
        assert "test_pass_rate" in signals, f"Expected 'test_pass_rate' in {list(signals.keys())}"
        assert "change_complexity" in signals, f"Expected 'change_complexity' in {list(signals.keys())}"
        # Each signal entry should have a 'value' key
        for name, sig in signals.items():
            assert "value" in sig, f"Signal '{name}' missing 'value': {sig}"

    def test_all_4_signals_present_when_review_and_non_review_tasks(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """All 4 signal types are captured when both review and non-review tasks are present."""
        db = _make_db(tmp_path)
        run_id = "test-persist-all-signals"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/test"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=_make_auto_merge_config(require_approve=False),
            )

        row = db.get_routing_decision(run_id)
        assert row is not None
        signals = row["signals_json"]
        assert isinstance(signals, dict)
        expected = {"llm_judge", "test_pass_rate", "review_quality", "change_complexity"}
        found = set(signals.keys())
        assert expected == found, f"Expected signals {expected}, got {found}"


# ===========================================================================
# Class: TestRoutingIntegrationFallback
# ===========================================================================


class TestRoutingIntegrationFallback:
    """Errors in confidence/routing never block pipeline completion."""

    def test_fallback_when_confidence_module_errors(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """RuntimeError in ConfidenceCalculator.compute_confidence is caught — no exception."""
        db = _make_db(tmp_path)
        run_id = "test-fallback-conf-error"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch(
            "orchestration_engine.confidence.ConfidenceCalculator.compute_confidence",
            side_effect=RuntimeError("simulated confidence failure"),
        ):
            # Must NOT raise
            returned_status = _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
            )

        # Status must remain 'success' — the error is non-fatal
        assert returned_status == "success"
        # No routing decision should have been persisted
        row = db.get_routing_decision(run_id)
        assert row is None, f"Expected no routing decision on error, got {row}"

    def test_fallback_when_routing_engine_errors(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """RuntimeError in RoutingEngine.route is caught — returns original final_status."""
        db = _make_db(tmp_path)
        run_id = "test-fallback-routing-error"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch(
            "orchestration_engine.routing.RoutingEngine.route",
            side_effect=RuntimeError("simulated routing failure"),
        ):
            returned_status = _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                final_status="success",
            )

        assert returned_status == "success"

    def test_dispatch_skipped_when_final_status_not_success(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """When final_status != 'success', routing decision is persisted but dispatch is skipped."""
        db = _make_db(tmp_path)
        run_id = "test-skip-dispatch"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr") as mock_merge:
            returned_status = _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                final_status="scoring_failed",
                auto_merge_config=_make_auto_merge_config(require_approve=False),
            )

        # Routing decision IS persisted (audit trail even on failure)
        row = db.get_routing_decision(run_id)
        assert row is not None

        # But merge was NOT dispatched (pipeline did not succeed)
        mock_merge.assert_not_called()

        # And the returned status must be the original (unchanged)
        assert returned_status == "scoring_failed"

    def test_empty_output_dir_produces_low_confidence_reject(
        self, tmp_path: Path
    ) -> None:
        """An empty output directory causes score=0.0 → 'reject' action."""
        out = tmp_path / "empty_output"
        out.mkdir()

        db = _make_db(tmp_path)
        run_id = "test-empty-dir"
        _seed_run(db, run_id, out)

        returned_status = _call_routing(
            db=db,
            run_id=run_id,
            output_dir=out,
        )

        row = db.get_routing_decision(run_id)
        assert row is not None
        assert row["action"] == "reject"
        assert returned_status == "rejected"

    def test_signals_json_stored_as_valid_json_string_in_raw_db(
        self, tmp_path: Path, routing_output_dir_high_confidence: Path
    ) -> None:
        """The raw signals_json column value is a valid JSON string (no serialisation errors)."""
        db = _make_db(tmp_path)
        run_id = "test-signals-json-valid"
        _seed_run(db, run_id, routing_output_dir_high_confidence)

        with patch("orchestration_engine.git_integration.GitContext.auto_merge_pr"), \
             patch("orchestration_engine.git_integration.GitContext.load_gate",
                   return_value={"branch": "feat/test"}), \
             patch("orchestration_engine.git_integration.GitContext.update_gate_status"):
            _call_routing(
                db=db,
                run_id=run_id,
                output_dir=routing_output_dir_high_confidence,
                auto_merge_config=_make_auto_merge_config(require_approve=False),
            )

        # Fetch the raw string directly from SQLite to verify it's valid JSON
        conn = db.get_connection()
        cursor = conn.execute(
            "SELECT signals_json FROM routing_decisions WHERE run_id = ?", (run_id,)
        )
        raw_row = cursor.fetchone()
        assert raw_row is not None
        raw_value = raw_row[0]
        # Should be parseable as JSON
        parsed = json.loads(raw_value)
        assert isinstance(parsed, dict)
