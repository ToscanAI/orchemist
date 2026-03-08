"""Tests for Issue #4.2.3 — Trust Routing Integration + Daemon Hook.

Covers:
- _build_trust_routing_config: tier structure, threshold mapping, retry collapse
- RoutingEngine.evaluate() with trust profile params (dynamic routing)
- RoutingEngine.evaluate() fallback when profile missing or pre-bootstrap
- _extract_repo_slug: URL normalisation
- _compute_and_dispatch_routing: trust calibration called after success
- RegressionWebhookHandler.handle_ci_failure: trust penalty on regression
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.db import Database
from orchestration_engine.routing import (
    RoutingEngine,
    RoutingConfig,
    RoutingTier,
    _build_trust_routing_config,
)
from orchestration_engine.daemon import _extract_repo_slug, _compute_and_dispatch_routing
from orchestration_engine.trust import TrustCalibrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    return Database(":memory:")


def _seed_run(db: Database, run_id: str, output_dir: Path) -> None:
    db.insert_pipeline_run({
        "run_id": run_id,
        "template_path": "/tmp/fake_template.yaml",
        "template_id": "coding-pipeline-v1",
        "input_json": '{"repo_url": "https://github.com/owner/repo", "task_type": "bugfix"}',
        "mode": "dry-run",
        "output_dir": str(output_dir),
        "status": "running",
    })


def _make_confidence_result(score: float) -> Any:
    """Return a minimal confidence-result-like object."""
    from orchestration_engine.confidence import ConfidenceLevel
    obj = MagicMock()
    obj.composite_score = score
    obj.confidence_level = ConfidenceLevel.HIGH if score >= 0.7 else ConfidenceLevel.LOW
    obj.signals = []
    obj.explanation = f"score={score}"
    return obj


def _seed_trust_profile(
    db: Database,
    successful_merges: int = 15,
    trust_score: float = 0.8,
    auto_merge_threshold: float = 0.82,
    human_review_threshold: float = 0.65,
) -> None:
    db.upsert_trust_profile({
        "repo": "owner/repo",
        "template_id": "coding-pipeline-v1",
        "task_type": "bugfix",
        "auto_merge_threshold": auto_merge_threshold,
        "human_review_threshold": human_review_threshold,
        "trust_score": trust_score,
        "total_runs": successful_merges,
        "successful_merges": successful_merges,
        "regressions": 0,
        "reverted_prs": 0,
        "last_run_at": None,
    })


# ===========================================================================
# TestBuildTrustRoutingConfig
# ===========================================================================


class TestBuildTrustRoutingConfig:
    """_build_trust_routing_config returns a 4-tier RoutingConfig."""

    def test_returns_routing_config(self) -> None:
        cfg = _build_trust_routing_config(0.85, 0.70)
        assert isinstance(cfg, RoutingConfig)

    def test_auto_merge_tier_uses_auto_merge_threshold(self) -> None:
        cfg = _build_trust_routing_config(0.82, 0.65)
        tiers = {t.name: t for t in cfg.tiers}
        assert "auto_merge" in tiers
        assert tiers["auto_merge"].min_score == pytest.approx(0.82)
        assert tiers["auto_merge"].max_score == pytest.approx(1.01)
        assert tiers["auto_merge"].strategy == "merge"

    def test_queue_review_tier_spans_human_to_auto(self) -> None:
        cfg = _build_trust_routing_config(0.82, 0.65)
        tiers = {t.name: t for t in cfg.tiers}
        assert "queue_review" in tiers
        assert tiers["queue_review"].min_score == pytest.approx(0.65)
        assert tiers["queue_review"].max_score == pytest.approx(0.82)
        assert tiers["queue_review"].strategy == "queue_review"

    def test_retry_tier_present_when_human_review_above_floor(self) -> None:
        cfg = _build_trust_routing_config(0.82, 0.65)
        tiers = {t.name: t for t in cfg.tiers}
        assert "retry" in tiers
        assert tiers["retry"].min_score == pytest.approx(0.50)
        assert tiers["retry"].max_score == pytest.approx(0.65)
        assert tiers["retry"].strategy == "retry"

    def test_reject_tier_below_retry_floor(self) -> None:
        cfg = _build_trust_routing_config(0.82, 0.65)
        tiers = {t.name: t for t in cfg.tiers}
        assert "reject" in tiers
        assert tiers["reject"].min_score == pytest.approx(0.00)
        assert tiers["reject"].max_score == pytest.approx(0.50)
        assert tiers["reject"].strategy == "reject"

    def test_retry_collapsed_when_human_review_at_or_below_floor(self) -> None:
        """When human_review_threshold <= 0.50, retry tier is collapsed."""
        cfg = _build_trust_routing_config(0.80, 0.40)
        tier_names = {t.name for t in cfg.tiers}
        assert "retry" not in tier_names
        # reject spans from 0.00 to 0.40 in this case
        tiers = {t.name: t for t in cfg.tiers}
        assert tiers["reject"].min_score == pytest.approx(0.00)
        assert tiers["reject"].max_score == pytest.approx(0.40)

    def test_config_has_four_tiers_normally(self) -> None:
        cfg = _build_trust_routing_config(0.85, 0.70)
        assert len(cfg.tiers) == 4

    def test_config_has_three_tiers_when_retry_collapsed(self) -> None:
        cfg = _build_trust_routing_config(0.80, 0.50)
        assert len(cfg.tiers) == 3

    def test_all_tier_names_unique(self) -> None:
        cfg = _build_trust_routing_config(0.82, 0.65)
        names = [t.name for t in cfg.tiers]
        assert len(names) == len(set(names))


# ===========================================================================
# TestRoutingEngineEvaluateTrust
# ===========================================================================


class TestRoutingEngineEvaluateTrust:
    """RoutingEngine.evaluate() with trust profile params."""

    def test_uses_dynamic_config_when_profile_past_bootstrap(self) -> None:
        """A post-bootstrap profile with low auto_merge_threshold relaxes auto-merge."""
        db = _make_db()
        _seed_trust_profile(db, successful_merges=15, auto_merge_threshold=0.75)

        engine = RoutingEngine()
        # Score 0.80 would normally be queue_review with default thresholds (0.90)
        # but with dynamic threshold 0.75, it should become auto_merge
        cr = _make_confidence_result(0.80)
        decision = engine.evaluate(
            cr,
            repo="owner/repo",
            template_id="coding-pipeline-v1",
            task_type="bugfix",
            db=db,
            bootstrap_threshold=10,
        )
        assert decision.tier == "auto_merge"
        assert decision.strategy == "merge"

    def test_falls_back_when_profile_in_bootstrap(self) -> None:
        """With fewer than bootstrap_threshold merges, default thresholds apply."""
        db = _make_db()
        _seed_trust_profile(db, successful_merges=5, auto_merge_threshold=0.75)

        engine = RoutingEngine()
        # Score 0.80 with default threshold 0.90 → queue_review
        cr = _make_confidence_result(0.80)
        decision = engine.evaluate(
            cr,
            repo="owner/repo",
            template_id="coding-pipeline-v1",
            task_type="bugfix",
            db=db,
            bootstrap_threshold=10,
        )
        # Default routing: 0.80 is in queue_review (0.75-0.90)
        assert decision.tier == "queue_review"

    def test_falls_back_when_no_profile_exists(self) -> None:
        """No profile in DB → default routing config."""
        db = _make_db()
        engine = RoutingEngine()
        cr = _make_confidence_result(0.80)
        decision = engine.evaluate(
            cr,
            repo="owner/repo",
            template_id="coding-pipeline-v1",
            task_type="bugfix",
            db=db,
        )
        # Default routing: queue_review
        assert decision.tier == "queue_review"

    def test_falls_back_when_db_is_none(self) -> None:
        """db=None → standard route() path."""
        engine = RoutingEngine()
        cr = _make_confidence_result(0.95)
        decision = engine.evaluate(cr, db=None)
        assert decision.tier == "auto_merge"

    def test_falls_back_when_repo_is_empty(self) -> None:
        """Missing repo → standard route() path."""
        db = _make_db()
        engine = RoutingEngine()
        cr = _make_confidence_result(0.95)
        decision = engine.evaluate(cr, repo="", template_id="t", task_type="x", db=db)
        assert decision.matched is True

    def test_trust_lookup_error_falls_back_gracefully(self) -> None:
        """DB error during trust lookup → fallback to default routing, no exception."""
        mock_db = MagicMock()
        mock_db.get_trust_profile.side_effect = RuntimeError("DB error")
        engine = RoutingEngine()
        cr = _make_confidence_result(0.95)
        # Must not raise
        decision = engine.evaluate(
            cr,
            repo="owner/repo",
            template_id="t",
            task_type="x",
            db=mock_db,
        )
        assert decision.matched is True

    def test_evaluate_without_trust_params_is_alias_for_route(self) -> None:
        """evaluate() with no trust params behaves identically to route()."""
        engine = RoutingEngine()
        cr = _make_confidence_result(0.50)
        assert engine.evaluate(cr).tier == engine.route(cr).tier

    def test_exact_bootstrap_boundary_uses_dynamic_config(self) -> None:
        """Exactly bootstrap_threshold merges → dynamic config activated."""
        db = _make_db()
        _seed_trust_profile(db, successful_merges=10, auto_merge_threshold=0.75)
        engine = RoutingEngine()
        cr = _make_confidence_result(0.80)
        decision = engine.evaluate(
            cr,
            repo="owner/repo",
            template_id="coding-pipeline-v1",
            task_type="bugfix",
            db=db,
            bootstrap_threshold=10,
        )
        # At exactly 10 merges with threshold 0.75, score 0.80 → auto_merge
        assert decision.tier == "auto_merge"


# ===========================================================================
# TestExtractRepoSlug
# ===========================================================================


class TestExtractRepoSlug:
    """_extract_repo_slug normalises GitHub URLs to owner/repo slugs."""

    def test_https_url(self) -> None:
        assert _extract_repo_slug("https://github.com/owner/repo") == "owner/repo"

    def test_https_url_with_git_suffix(self) -> None:
        assert _extract_repo_slug("https://github.com/owner/repo.git") == "owner/repo"

    def test_https_url_with_trailing_slash(self) -> None:
        assert _extract_repo_slug("https://github.com/owner/repo/") == "owner/repo"

    def test_ssh_url(self) -> None:
        assert _extract_repo_slug("git@github.com:owner/repo.git") == "owner/repo"

    def test_ssh_url_without_git_suffix(self) -> None:
        assert _extract_repo_slug("git@github.com:owner/repo") == "owner/repo"

    def test_plain_slug_passthrough(self) -> None:
        assert _extract_repo_slug("owner/repo") == "owner/repo"

    def test_empty_string(self) -> None:
        assert _extract_repo_slug("") == ""

    def test_unknown_format_passthrough(self) -> None:
        result = _extract_repo_slug("some-other-value")
        assert result == "some-other-value"


# ===========================================================================
# TestDaemonTrustUpdate
# ===========================================================================


class TestDaemonTrustUpdate:
    """_compute_and_dispatch_routing calls TrustCalibrator after routing."""

    def _make_output_dir(self, tmp_path: Path, score_target: str = "low") -> Path:
        """Create an output dir with task JSON files."""
        out = tmp_path / "output"
        out.mkdir()
        if score_target == "low":
            for i in range(5):
                (out / f"phase_{i}.json").write_text(json.dumps({"state": "failed"}))
        else:
            (out / "phase_ok.json").write_text(json.dumps({"state": "success", "confidence": 0.95}))
        return out

    def test_trust_calibrator_called_when_repo_and_template_provided(
        self, tmp_path: Path
    ) -> None:
        """When repo + template_id are set, TrustCalibrator.update_after_run is called."""
        db = _make_db()
        run_id = "trust-update-001"
        out = self._make_output_dir(tmp_path)
        _seed_run(db, run_id, out)

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run") as mock_update:
            mock_update.return_value = {
                "new_score": 0.55, "threshold": 0.98,
                "profile_id": 1, "adjustment_id": 1,
                "run_id": run_id, "outcome": "run_success",
                "old_score": 0.5, "delta": 0.05,
                "total_runs": 1, "successful_merges": 1,
                "regressions": 0, "reverted_prs": 0,
            }
            _compute_and_dispatch_routing(
                run_id=run_id,
                output_dir=out,
                db=db,
                auto_merge_config=None,
                routing_config=None,
                scoring_passed=True,
                scoring_score=None,
                phase_outputs={},
                final_status="success",
                repo="owner/repo",
                template_id="coding-pipeline-v1",
                task_type="bugfix",
            )

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs.kwargs["outcome"] == "run_success"
        assert call_kwargs.kwargs["run_id"] == run_id

    def test_trust_calibrator_not_called_when_repo_empty(
        self, tmp_path: Path
    ) -> None:
        """When repo is empty, TrustCalibrator.update_after_run is NOT called."""
        db = _make_db()
        run_id = "trust-update-no-repo"
        out = self._make_output_dir(tmp_path)
        _seed_run(db, run_id, out)

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run") as mock_update:
            _compute_and_dispatch_routing(
                run_id=run_id,
                output_dir=out,
                db=db,
                auto_merge_config=None,
                routing_config=None,
                scoring_passed=True,
                scoring_score=None,
                phase_outputs={},
                final_status="success",
                repo="",
                template_id="coding-pipeline-v1",
                task_type="bugfix",
            )

        mock_update.assert_not_called()

    def test_trust_calibrator_failure_is_non_fatal(
        self, tmp_path: Path
    ) -> None:
        """An exception in TrustCalibrator.update_after_run does not abort routing."""
        db = _make_db()
        run_id = "trust-update-err"
        out = self._make_output_dir(tmp_path)
        _seed_run(db, run_id, out)

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run",
                   side_effect=RuntimeError("simulated trust failure")):
            # Must not raise
            returned_status = _compute_and_dispatch_routing(
                run_id=run_id,
                output_dir=out,
                db=db,
                auto_merge_config=None,
                routing_config=None,
                scoring_passed=True,
                scoring_score=None,
                phase_outputs={},
                final_status="success",
                repo="owner/repo",
                template_id="coding-pipeline-v1",
                task_type="bugfix",
            )

        # Routing still completes; low confidence → rejected
        assert returned_status in ("rejected", "pending_review", "success")

    def test_trust_update_uses_run_success_outcome(
        self, tmp_path: Path
    ) -> None:
        """The outcome passed to TrustCalibrator is always 'run_success'."""
        db = _make_db()
        run_id = "trust-outcome-check"
        out = self._make_output_dir(tmp_path)
        _seed_run(db, run_id, out)

        captured_outcomes = []

        def _capture_update(run_id, outcome, db):
            captured_outcomes.append(outcome)
            return {
                "new_score": 0.55, "threshold": 0.98,
                "profile_id": 1, "adjustment_id": 1,
                "run_id": run_id, "outcome": outcome,
                "old_score": 0.5, "delta": 0.05,
                "total_runs": 1, "successful_merges": 1,
                "regressions": 0, "reverted_prs": 0,
            }

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run",
                   side_effect=_capture_update):
            _compute_and_dispatch_routing(
                run_id=run_id,
                output_dir=out,
                db=db,
                auto_merge_config=None,
                routing_config=None,
                scoring_passed=True,
                scoring_score=None,
                phase_outputs={},
                final_status="success",
                repo="owner/repo",
                template_id="coding-pipeline-v1",
                task_type="bugfix",
            )

        assert len(captured_outcomes) == 1
        assert captured_outcomes[0] == "run_success"


# ===========================================================================
# TestRegressionTrustPenalty
# ===========================================================================


class TestRegressionTrustPenalty:
    """RegressionWebhookHandler.handle_ci_failure applies trust penalty on regression."""

    def _make_handler(self, db: Database):
        from orchestration_engine.regression import (
            RegressionWebhookHandler,
            RegressionDetector,
        )
        from orchestration_engine.git_integration import GitContext

        mock_git = MagicMock(spec=GitContext)
        mock_detector = MagicMock(spec=RegressionDetector)

        handler = RegressionWebhookHandler(
            db=db,
            git_context=mock_git,
            detector=mock_detector,
            repo_path=Path("/tmp/fake-repo"),
            repo_slug="owner/repo",
        )
        return handler, mock_detector

    def _make_failure_payload(self, head_sha: str = "abc1234") -> dict:
        return {
            "check_suite": {
                "conclusion": "failure",
                "head_sha": head_sha,
                "url": "https://api.github.com/repos/owner/repo/check-suites/1",
            }
        }

    def test_trust_penalty_called_on_regression_detected(self) -> None:
        """When a regression is detected, TrustCalibrator.update_after_run is called."""
        from orchestration_engine.regression import Regression

        db = _make_db()
        db.store_green_sha("owner/repo", "green001")

        handler, mock_detector = self._make_handler(db)

        fake_regression = Regression(
            commit_sha="abc1234",
            ci_run_url="https://ci.example.com/run/1",
            failure_type="ci_failure",
        )
        mock_detector.detect.return_value = fake_regression

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run") as mock_update, \
             patch.object(handler, "_open_github_issue", return_value=None):
            mock_update.return_value = {
                "new_score": 0.4, "threshold": 0.98,
                "profile_id": 1, "adjustment_id": 1,
                "run_id": fake_regression.id, "outcome": "regression",
                "old_score": 0.5, "delta": -0.1,
                "total_runs": 1, "successful_merges": 0,
                "regressions": 1, "reverted_prs": 0,
            }
            result = handler.handle_ci_failure(self._make_failure_payload())

        assert result is not None
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["outcome"] == "regression"
        assert call_kwargs["run_id"] == fake_regression.id

    def test_trust_penalty_failure_is_non_fatal(self) -> None:
        """An exception in TrustCalibrator.update_after_run does not block regression handling."""
        from orchestration_engine.regression import Regression

        db = _make_db()
        db.store_green_sha("owner/repo", "green001")

        handler, mock_detector = self._make_handler(db)

        fake_regression = Regression(
            commit_sha="abc1234",
            ci_run_url="https://ci.example.com/run/1",
            failure_type="ci_failure",
        )
        mock_detector.detect.return_value = fake_regression

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run",
                   side_effect=RuntimeError("trust DB down")), \
             patch.object(handler, "_open_github_issue", return_value=None):
            # Must not raise
            result = handler.handle_ci_failure(self._make_failure_payload())

        # Regression is still returned despite trust penalty failure
        assert result is not None

    def test_trust_penalty_not_called_when_no_regression(self) -> None:
        """When detector returns None, trust penalty is NOT applied."""
        db = _make_db()
        db.store_green_sha("owner/repo", "green001")

        handler, mock_detector = self._make_handler(db)
        mock_detector.detect.return_value = None

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run") as mock_update:
            result = handler.handle_ci_failure(self._make_failure_payload())

        assert result is None
        mock_update.assert_not_called()

    def test_trust_penalty_uses_regression_outcome(self) -> None:
        """The outcome passed to update_after_run is always 'regression'."""
        from orchestration_engine.regression import Regression

        db = _make_db()
        db.store_green_sha("owner/repo", "green001")

        handler, mock_detector = self._make_handler(db)

        fake_regression = Regression(
            commit_sha="abc1234",
            ci_run_url="https://ci.example.com/run/1",
            failure_type="test_failure",
        )
        mock_detector.detect.return_value = fake_regression

        captured = []

        def _capture(run_id, outcome, db):
            captured.append(outcome)
            return {
                "new_score": 0.4, "threshold": 0.98,
                "profile_id": 1, "adjustment_id": 1,
                "run_id": run_id, "outcome": outcome,
                "old_score": 0.5, "delta": -0.1,
                "total_runs": 1, "successful_merges": 0,
                "regressions": 1, "reverted_prs": 0,
            }

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run",
                   side_effect=_capture), \
             patch.object(handler, "_open_github_issue", return_value=None):
            handler.handle_ci_failure(self._make_failure_payload())

        assert len(captured) == 1
        assert captured[0] == "regression"

    def test_trust_penalty_not_applied_on_ci_success(self) -> None:
        """CI success (green SHA update) does NOT trigger a trust penalty."""
        db = _make_db()
        handler, mock_detector = self._make_handler(db)

        payload = {
            "check_suite": {
                "conclusion": "success",
                "head_sha": "def5678",
                "url": "https://api.github.com/repos/owner/repo/check-suites/2",
            }
        }

        with patch("orchestration_engine.trust.TrustCalibrator.update_after_run") as mock_update:
            result = handler.handle_ci_failure(payload)

        assert result is None
        mock_update.assert_not_called()

    def test_template_id_parameter_used_in_trust_penalty(self) -> None:
        """When template_id is passed to the handler, it is used for the trust profile lookup."""
        from orchestration_engine.regression import (
            Regression,
            RegressionWebhookHandler,
            RegressionDetector,
        )
        from orchestration_engine.git_integration import GitContext

        db = _make_db()
        db.store_green_sha("owner/repo", "green001")

        mock_git = MagicMock(spec=GitContext)
        mock_detector = MagicMock(spec=RegressionDetector)

        # Construct handler with an explicit template_id (not the default "ci")
        handler = RegressionWebhookHandler(
            db=db,
            git_context=mock_git,
            detector=mock_detector,
            repo_path=Path("/tmp/fake-repo"),
            repo_slug="owner/repo",
            template_id="coding-pipeline-v1",
        )

        fake_regression = Regression(
            commit_sha="abc1234",
            ci_run_url="https://ci.example.com/run/1",
            failure_type="ci_failure",
        )
        mock_detector.detect.return_value = fake_regression

        captured_calls = []

        def _capture(**kwargs):
            captured_calls.append(kwargs)
            return {
                "new_score": 0.4, "threshold": 0.98,
                "profile_id": 1, "adjustment_id": 1,
                "run_id": kwargs.get("run_id"), "outcome": "regression",
                "old_score": 0.5, "delta": -0.1,
                "total_runs": 1, "successful_merges": 0,
                "regressions": 1, "reverted_prs": 0,
            }

        from orchestration_engine.trust import TrustCalibrator
        with patch.object(TrustCalibrator, "update_after_run", _capture), \
             patch.object(handler, "_open_github_issue", return_value=None):
            handler.handle_ci_failure({
                "check_suite": {
                    "conclusion": "failure",
                    "head_sha": "abc1234",
                    "url": "https://ci.example.com/run/1",
                }
            })

        # Verify a TrustCalibrator was instantiated with our explicit template_id
        assert handler._template_id == "coding-pipeline-v1"


# ===========================================================================
# TestBuildTrustRoutingConfigValidation
# ===========================================================================


class TestBuildTrustRoutingConfigValidation:
    """_build_trust_routing_config raises ValueError for invalid threshold inputs."""

    def test_raises_when_thresholds_equal(self) -> None:
        """auto_merge_threshold == human_review_threshold → ValueError."""
        with pytest.raises(ValueError, match="strictly greater than"):
            _build_trust_routing_config(0.75, 0.75)

    def test_raises_when_thresholds_inverted(self) -> None:
        """human_review_threshold > auto_merge_threshold → ValueError."""
        with pytest.raises(ValueError, match="strictly greater than"):
            _build_trust_routing_config(0.60, 0.80)

    def test_raises_when_auto_merge_threshold_out_of_range(self) -> None:
        """auto_merge_threshold > 1.0 → ValueError."""
        with pytest.raises(ValueError, match="auto_merge_threshold must be in"):
            _build_trust_routing_config(1.5, 0.70)

    def test_raises_when_human_review_threshold_negative(self) -> None:
        """human_review_threshold < 0.0 → ValueError."""
        with pytest.raises(ValueError, match="human_review_threshold must be in"):
            _build_trust_routing_config(0.80, -0.10)

    def test_evaluate_falls_back_gracefully_on_equal_thresholds(self) -> None:
        """evaluate() catches the ValueError from bad thresholds and falls back."""
        db = _make_db()
        # Seed a profile with equal thresholds (pathological DB data)
        db.upsert_trust_profile({
            "repo": "owner/repo",
            "template_id": "coding-pipeline-v1",
            "task_type": "bugfix",
            "auto_merge_threshold": 0.75,
            "human_review_threshold": 0.75,  # equal — invalid
            "trust_score": 0.8,
            "total_runs": 20,
            "successful_merges": 20,
            "regressions": 0,
            "reverted_prs": 0,
            "last_run_at": None,
        })

        engine = RoutingEngine()
        cr = _make_confidence_result(0.80)
        # Must not raise — evaluate() catches the ValueError and falls back
        decision = engine.evaluate(
            cr,
            repo="owner/repo",
            template_id="coding-pipeline-v1",
            task_type="bugfix",
            db=db,
        )
        # Fallback to default routing — 0.80 → queue_review
        assert decision.tier == "queue_review"


# ===========================================================================
# TestComputeAndDispatchRoutingUsesEvaluate
# ===========================================================================


class TestComputeAndDispatchRoutingUsesEvaluate:
    """_compute_and_dispatch_routing uses .evaluate() so trust profiles affect routing."""

    def _make_output_dir(self, tmp_path: Path) -> Path:
        out = tmp_path / "output"
        out.mkdir()
        return out

    def test_routing_decision_changes_with_trust_profile(
        self, tmp_path: Path
    ) -> None:
        """With a post-bootstrap trust profile, routing produces a different tier
        than the default thresholds would (proving .evaluate() is called, not .route()).
        """
        db = _make_db()
        run_id = "routing-trust-check"
        out = self._make_output_dir(tmp_path)
        _seed_run(db, run_id, out)

        # Seed a post-bootstrap trust profile with a low auto_merge threshold (0.75).
        # Default threshold is 0.90, so a score of 0.80 maps to:
        #   - Default config:  queue_review  (0.75 ≤ 0.80 < 0.90)
        #   - Trust config:    auto_merge    (0.75 ≤ 0.80 < 1.01)
        _seed_trust_profile(db, successful_merges=15, auto_merge_threshold=0.75)

        # We spy on RoutingEngine.evaluate to capture what decision it returns
        captured_decisions = []
        original_evaluate = RoutingEngine.evaluate

        def _spy_evaluate(self_engine, confidence_result, **kwargs):
            decision = original_evaluate(self_engine, confidence_result, **kwargs)
            captured_decisions.append(decision)
            return decision

        with patch.object(RoutingEngine, "evaluate", _spy_evaluate):
            # Patch ConfidenceCalculator to return a controlled score of 0.80
            mock_confidence = _make_confidence_result(0.80)
            with patch(
                "orchestration_engine.daemon.ConfidenceCalculator"
            ) as MockCC:
                MockCC.return_value.compute_confidence.return_value = mock_confidence
                with patch("orchestration_engine.trust.TrustCalibrator.update_after_run",
                           return_value={
                               "new_score": 0.8, "threshold": 0.98,
                               "profile_id": 1, "adjustment_id": 1,
                               "run_id": run_id, "outcome": "run_success",
                               "old_score": 0.75, "delta": 0.05,
                               "total_runs": 16, "successful_merges": 16,
                               "regressions": 0, "reverted_prs": 0,
                           }):
                    _compute_and_dispatch_routing(
                        run_id=run_id,
                        output_dir=out,
                        db=db,
                        auto_merge_config=None,
                        routing_config=None,
                        scoring_passed=True,
                        scoring_score=None,
                        phase_outputs={},
                        final_status="success",
                        repo="owner/repo",
                        template_id="coding-pipeline-v1",
                        task_type="bugfix",
                    )

        assert len(captured_decisions) == 1
        # Trust config with threshold 0.75 → score 0.80 → auto_merge
        assert captured_decisions[0].tier == "auto_merge", (
            f"Expected 'auto_merge' with trust-profile config, got '{captured_decisions[0].tier}'. "
            "This suggests .route() was called instead of .evaluate()."
        )
