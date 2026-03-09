"""tests/test_cost_tracker.py — Tests for Issue #5.2.1: PricingTable + CostTracker.

Covers all acceptance criteria:

* ``PricingTable`` loads ``pricing.yaml``, returns correct pricing dicts,
  computes costs, falls back to ``default`` for unknown models, raises on
  missing default, validates token counts.

* ``CostTracker`` inserts rows into ``cost_tracking`` via ``record_phase``,
  aggregates with ``get_run_cost``, returns per-phase list with
  ``get_run_phases``, enforces budgets with ``check_budget``.

* DB table ``cost_tracking`` is created by fresh ``Database(":memory:")``
  and by migration 018.

* Module exports ``PricingTable``, ``CostTracker``, ``BudgetExceededError``
  via ``orchestration_engine.__init__``.

Test classes
------------
    TestPricingTableLoad          — YAML loading, validation, error cases
    TestPricingTableComputeCost   — cost calculation accuracy
    TestPricingTableHelpers       — known_models, has_model
    TestCostTrackerRecordPhase    — DB insert, return dict
    TestCostTrackerGetRunCost     — aggregation, empty run
    TestCostTrackerGetRunPhases   — per-phase list ordering
    TestCostTrackerCheckBudget    — under/over budget, zero budget
    TestCostTrackingDBTable       — table exists in fresh DB, schema columns
    TestMigration018              — migration 018 creates table idempotently
    TestModuleExports             — __init__.py exports all three names
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from orchestration_engine.db import Database
from orchestration_engine.cost_tracker import (
    BudgetExceededError,
    CostTracker,
    PricingTable,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> Database:
    """Fresh in-memory Database with schema + migrations applied."""
    return Database(":memory:")


def _seed_run(db: Database, run_id: str) -> None:
    """Insert a minimal pipeline_runs row so FK constraints pass in tests."""
    db.execute(
        """
        INSERT OR IGNORE INTO pipeline_runs
            (run_id, template_path, template_id, input_json, mode, output_dir, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, "t.yaml", "t", "{}", "openclaw", "/tmp/out", "running"),
    )


def _make_pricing(yaml_content: str) -> PricingTable:
    """Write *yaml_content* to a temp file and return a PricingTable for it."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(yaml_content)
        path = Path(fh.name)
    return PricingTable(pricing_path=path)


_MINIMAL_YAML = """\
models:
  test-model:
    input_per_million: 2.0
    output_per_million: 10.0
  default:
    input_per_million: 3.0
    output_per_million: 15.0
"""

_NO_DEFAULT_YAML = """\
models:
  known-model:
    input_per_million: 1.0
    output_per_million: 5.0
"""

_HAIKU_YAML = """\
models:
  anthropic/claude-haiku-4-5-20241022:
    input_per_million: 0.25
    output_per_million: 1.25
  default:
    input_per_million: 3.0
    output_per_million: 15.0
"""


# ---------------------------------------------------------------------------
# TestPricingTableLoad
# ---------------------------------------------------------------------------

class TestPricingTableLoad:
    """YAML loading, validation, and error cases."""

    def test_loads_default_pricing_yaml(self):
        """Default PricingTable (no path override) loads without error."""
        pt = PricingTable()
        # At minimum the 'default' entry must exist
        assert pt.has_model("default") is False  # 'default' is not a real model
        pricing = pt.get_pricing("default")
        assert pricing["input_per_million"] > 0
        assert pricing["output_per_million"] > 0

    def test_loads_custom_yaml(self):
        pt = _make_pricing(_MINIMAL_YAML)
        assert pt.has_model("test-model")
        pricing = pt.get_pricing("test-model")
        assert pricing["input_per_million"] == 2.0
        assert pricing["output_per_million"] == 10.0

    def test_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="Pricing YAML not found"):
            PricingTable(pricing_path=Path("/nonexistent/pricing.yaml"))

    def test_raises_missing_models_key(self):
        yaml_no_models = "something_else: {}\n"
        with pytest.raises(ValueError, match="must contain a top-level 'models' key"):
            _make_pricing(yaml_no_models)

    def test_raises_invalid_entry_missing_key(self):
        bad_yaml = """\
models:
  bad-model:
    input_per_million: 1.0
"""
        with pytest.raises(ValueError, match="Invalid pricing entry"):
            _make_pricing(bad_yaml)

    def test_raises_invalid_models_type(self):
        bad_yaml = "models: a_string\n"
        with pytest.raises(ValueError, match="must be a mapping"):
            _make_pricing(bad_yaml)


# ---------------------------------------------------------------------------
# TestPricingTableComputeCost
# ---------------------------------------------------------------------------

class TestPricingTableComputeCost:
    """Cost calculation accuracy."""

    def test_zero_tokens(self):
        pt = _make_pricing(_MINIMAL_YAML)
        assert pt.compute_cost("test-model", 0, 0) == 0.0

    def test_input_only(self):
        pt = _make_pricing(_MINIMAL_YAML)
        # 1_000_000 input tokens * $2.0 / 1M = $2.0
        cost = pt.compute_cost("test-model", 1_000_000, 0)
        assert abs(cost - 2.0) < 1e-9

    def test_output_only(self):
        pt = _make_pricing(_MINIMAL_YAML)
        # 1_000_000 output tokens * $10.0 / 1M = $10.0
        cost = pt.compute_cost("test-model", 0, 1_000_000)
        assert abs(cost - 10.0) < 1e-9

    def test_mixed_tokens(self):
        pt = _make_pricing(_MINIMAL_YAML)
        # 500k input @ $2/M = $1.00; 200k output @ $10/M = $2.00 → $3.00
        cost = pt.compute_cost("test-model", 500_000, 200_000)
        assert abs(cost - 3.0) < 1e-9

    def test_haiku_small_call(self):
        pt = _make_pricing(_HAIKU_YAML)
        # 1000 input, 500 output: 0.001*0.25 + 0.0005*1.25 = 0.00025+0.000625 = 0.000875
        cost = pt.compute_cost("anthropic/claude-haiku-4-5-20241022", 1000, 500)
        assert abs(cost - 0.000875) < 1e-9

    def test_negative_input_tokens_raises(self):
        pt = _make_pricing(_MINIMAL_YAML)
        with pytest.raises(ValueError, match="input_tokens must be >= 0"):
            pt.compute_cost("test-model", -1, 0)

    def test_negative_output_tokens_raises(self):
        pt = _make_pricing(_MINIMAL_YAML)
        with pytest.raises(ValueError, match="output_tokens must be >= 0"):
            pt.compute_cost("test-model", 0, -5)

    def test_unknown_model_uses_default(self):
        pt = _make_pricing(_MINIMAL_YAML)
        # 'unknown-model' → falls back to default: $3/M in, $15/M out
        cost = pt.compute_cost("unknown-model", 1_000_000, 0)
        assert abs(cost - 3.0) < 1e-9

    def test_unknown_model_no_default_raises(self):
        pt = _make_pricing(_NO_DEFAULT_YAML)
        with pytest.raises(KeyError, match="unknown-xyz"):
            pt.compute_cost("unknown-xyz", 100, 100)


# ---------------------------------------------------------------------------
# TestPricingTableHelpers
# ---------------------------------------------------------------------------

class TestPricingTableHelpers:
    """known_models and has_model helpers."""

    def test_known_models_excludes_default(self):
        pt = _make_pricing(_MINIMAL_YAML)
        models = pt.known_models
        assert "test-model" in models
        assert "default" not in models

    def test_has_model_true_for_known(self):
        pt = _make_pricing(_MINIMAL_YAML)
        assert pt.has_model("test-model") is True

    def test_has_model_false_for_unknown(self):
        pt = _make_pricing(_MINIMAL_YAML)
        assert pt.has_model("not-in-table") is False

    def test_has_model_false_for_default(self):
        pt = _make_pricing(_MINIMAL_YAML)
        assert pt.has_model("default") is False

    def test_default_pricing_has_known_models(self):
        """The bundled pricing.yaml contains at least the canonical model names."""
        pt = PricingTable()
        expected = [
            "anthropic/claude-haiku-4-5-20241022",
            "anthropic/claude-sonnet-4-20250514",
            "anthropic/claude-opus-4-6",
        ]
        for model in expected:
            assert pt.has_model(model), f"Missing model: {model}"


# ---------------------------------------------------------------------------
# TestCostTrackerRecordPhase
# ---------------------------------------------------------------------------

class TestCostTrackerRecordPhase:
    """record_phase inserts a DB row and returns the correct dict."""

    def test_returns_dict_with_cost(self):
        db = _make_db()
        _seed_run(db, "run-001")
        tracker = CostTracker(db)
        result = tracker.record_phase(
            run_id="run-001",
            phase_id="spec",
            model="anthropic/claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=500,
        )
        assert result["run_id"] == "run-001"
        assert result["phase_id"] == "spec"
        assert result["model"] == "anthropic/claude-sonnet-4-20250514"
        assert result["input_tokens"] == 1000
        assert result["output_tokens"] == 500
        assert result["cost_usd"] > 0

    def test_cost_matches_expected_formula(self):
        db = _make_db()
        _seed_run(db, "run-002")
        # Use a custom pricing table to control exact values
        pricing_yaml = """\
models:
  my-model:
    input_per_million: 4.0
    output_per_million: 20.0
  default:
    input_per_million: 3.0
    output_per_million: 15.0
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(pricing_yaml)
            path = Path(fh.name)

        tracker = CostTracker(db, pricing_path=path)
        result = tracker.record_phase(
            run_id="run-002",
            phase_id="implement",
            model="my-model",
            input_tokens=2_000_000,
            output_tokens=500_000,
        )
        # 2M * $4/M + 0.5M * $20/M = $8.00 + $10.00 = $18.00
        assert abs(result["cost_usd"] - 18.0) < 1e-9

    def test_row_is_persisted_to_db(self):
        db = _make_db()
        _seed_run(db, "run-003")
        tracker = CostTracker(db)
        tracker.record_phase(
            run_id="run-003",
            phase_id="review",
            model="anthropic/claude-opus-4-6",
            input_tokens=5000,
            output_tokens=2000,
        )
        rows = db.fetch_all(
            "SELECT * FROM cost_tracking WHERE run_id = ?", ("run-003",)
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["phase_id"] == "review"
        assert row["model"] == "anthropic/claude-opus-4-6"
        assert row["input_tokens"] == 5000
        assert row["output_tokens"] == 2000
        assert row["cost_usd"] > 0

    def test_multiple_phases_for_same_run(self):
        db = _make_db()
        _seed_run(db, "run-X")
        tracker = CostTracker(db)
        tracker.record_phase("run-X", "spec", "anthropic/claude-haiku-4-5-20241022", 100, 50)
        tracker.record_phase("run-X", "implement", "anthropic/claude-sonnet-4-20250514", 500, 300)
        rows = db.fetch_all("SELECT * FROM cost_tracking WHERE run_id = ?", ("run-X",))
        assert len(rows) == 2

    def test_unknown_model_falls_back_to_default(self):
        db = _make_db()
        _seed_run(db, "run-99")
        tracker = CostTracker(db)
        # 'mystery-model' is not in the bundled pricing.yaml → uses 'default'
        result = tracker.record_phase("run-99", "qa", "mystery-model", 1000, 500)
        assert result["cost_usd"] > 0  # fallback to default pricing


# ---------------------------------------------------------------------------
# TestCostTrackerGetRunCost
# ---------------------------------------------------------------------------

class TestCostTrackerGetRunCost:
    """get_run_cost aggregates all phases for a run."""

    def test_returns_zero_for_empty_run(self):
        db = _make_db()
        tracker = CostTracker(db)
        assert tracker.get_run_cost("nonexistent-run") == 0.0

    def test_sums_single_phase(self):
        db = _make_db()
        _seed_run(db, "run-A")
        tracker = CostTracker(db)
        result = tracker.record_phase("run-A", "spec", "anthropic/claude-opus-4-6", 1_000_000, 0)
        total = tracker.get_run_cost("run-A")
        assert abs(total - result["cost_usd"]) < 1e-9

    def test_sums_multiple_phases(self):
        db = _make_db()
        _seed_run(db, "run-B")
        tracker = CostTracker(db)
        r1 = tracker.record_phase("run-B", "spec", "anthropic/claude-haiku-4-5-20241022", 1000, 500)
        r2 = tracker.record_phase("run-B", "implement", "anthropic/claude-sonnet-4-20250514", 2000, 800)
        expected = r1["cost_usd"] + r2["cost_usd"]
        total = tracker.get_run_cost("run-B")
        assert abs(total - expected) < 1e-9

    def test_isolates_runs(self):
        db = _make_db()
        _seed_run(db, "run-C")
        _seed_run(db, "run-D")
        tracker = CostTracker(db)
        tracker.record_phase("run-C", "spec", "anthropic/claude-sonnet-4-20250514", 1000, 500)
        tracker.record_phase("run-D", "spec", "anthropic/claude-opus-4-6", 1000, 500)
        cost_c = tracker.get_run_cost("run-C")
        cost_d = tracker.get_run_cost("run-D")
        # Opus is more expensive than Sonnet → costs should differ
        assert cost_d > cost_c


# ---------------------------------------------------------------------------
# TestCostTrackerGetRunPhases
# ---------------------------------------------------------------------------

class TestCostTrackerGetRunPhases:
    """get_run_phases returns an ordered list of phase records."""

    def test_empty_run_returns_empty_list(self):
        db = _make_db()
        tracker = CostTracker(db)
        assert tracker.get_run_phases("nonexistent") == []

    def test_returns_phases_in_insertion_order(self):
        db = _make_db()
        _seed_run(db, "run-E")
        tracker = CostTracker(db)
        phases = ["spec", "implement", "review", "qa"]
        for ph in phases:
            tracker.record_phase("run-E", ph, "anthropic/claude-haiku-4-5-20241022", 100, 50)

        result = tracker.get_run_phases("run-E")
        assert len(result) == 4
        for i, ph in enumerate(phases):
            assert result[i]["phase_id"] == ph

    def test_records_contain_expected_keys(self):
        db = _make_db()
        _seed_run(db, "run-F")
        tracker = CostTracker(db)
        tracker.record_phase("run-F", "spec", "anthropic/claude-sonnet-4-20250514", 500, 200)
        rows = tracker.get_run_phases("run-F")
        assert len(rows) == 1
        row = rows[0]
        for key in ("id", "run_id", "phase_id", "model", "input_tokens", "output_tokens", "cost_usd"):
            assert key in row, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# TestCostTrackerCheckBudget
# ---------------------------------------------------------------------------

class TestCostTrackerCheckBudget:
    """check_budget enforces the USD ceiling."""

    def test_under_budget_returns_actual_cost(self):
        db = _make_db()
        _seed_run(db, "run-G")
        tracker = CostTracker(db)
        tracker.record_phase("run-G", "spec", "anthropic/claude-haiku-4-5-20241022", 100, 50)
        actual = tracker.check_budget("run-G", budget_usd=100.0)
        assert actual > 0
        assert actual < 100.0

    def test_empty_run_is_under_any_positive_budget(self):
        db = _make_db()
        tracker = CostTracker(db)
        actual = tracker.check_budget("run-H", budget_usd=0.01)
        assert actual == 0.0

    def test_zero_budget_raises_when_cost_is_nonzero(self):
        db = _make_db()
        _seed_run(db, "run-I")
        tracker = CostTracker(db)
        tracker.record_phase("run-I", "spec", "anthropic/claude-haiku-4-5-20241022", 1000, 500)
        with pytest.raises(BudgetExceededError) as exc_info:
            tracker.check_budget("run-I", budget_usd=0.0)
        err = exc_info.value
        assert err.run_id == "run-I"
        assert err.budget_usd == 0.0
        assert err.actual_usd > 0

    def test_negative_budget_raises_value_error(self):
        db = _make_db()
        tracker = CostTracker(db)
        with pytest.raises(ValueError, match="budget_usd must be >= 0"):
            tracker.check_budget("run-J", budget_usd=-1.0)

    def test_over_budget_raises_budget_exceeded_error(self):
        db = _make_db()
        _seed_run(db, "run-K")
        tracker = CostTracker(db)
        # Record many tokens to drive up cost
        tracker.record_phase("run-K", "implement", "anthropic/claude-opus-4-6", 10_000_000, 5_000_000)
        with pytest.raises(BudgetExceededError) as exc_info:
            tracker.check_budget("run-K", budget_usd=0.001)
        err = exc_info.value
        assert err.run_id == "run-K"
        assert err.actual_usd > err.budget_usd

    def test_budget_exceeded_error_message(self):
        db = _make_db()
        _seed_run(db, "run-L")
        tracker = CostTracker(db)
        tracker.record_phase("run-L", "spec", "anthropic/claude-opus-4-6", 1_000_000, 1_000_000)
        with pytest.raises(BudgetExceededError, match="run-L"):
            tracker.check_budget("run-L", budget_usd=0.001)

    def test_exactly_at_budget_does_not_raise(self):
        """A run that costs exactly the budget should NOT raise."""
        db = _make_db()
        tracker = CostTracker(db)
        # 0 tokens → $0.00 cost; budget $0.00 → exactly at limit
        actual = tracker.check_budget("run-M", budget_usd=0.0)
        assert actual == 0.0


# ---------------------------------------------------------------------------
# TestCostTrackingDBTable
# ---------------------------------------------------------------------------

class TestCostTrackingDBTable:
    """cost_tracking table is created by a fresh Database instance."""

    def test_table_exists_in_fresh_db(self):
        db = _make_db()
        rows = db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cost_tracking'"
        )
        assert len(rows) == 1, "cost_tracking table not found in schema"

    def test_table_has_expected_columns(self):
        db = _make_db()
        rows = db.fetch_all("PRAGMA table_info(cost_tracking)")
        col_names = {row["name"] for row in rows}
        expected = {"id", "run_id", "phase_id", "model", "input_tokens", "output_tokens", "cost_usd", "created_at"}
        assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"

    def test_index_exists(self):
        db = _make_db()
        rows = db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_cost_tracking_run_id'"
        )
        assert len(rows) == 1, "idx_cost_tracking_run_id index not found"


# ---------------------------------------------------------------------------
# TestMigration018
# ---------------------------------------------------------------------------

class TestMigration018:
    """Migration 018 creates the cost_tracking table idempotently."""

    def test_migration_018_recorded(self):
        """Fresh DB should have migration 018 applied."""
        db = _make_db()
        rows = db.fetch_all(
            "SELECT name FROM migrations WHERE name = '018_add_cost_tracking_table'"
        )
        assert len(rows) == 1, "Migration 018 not recorded in migrations table"

    def test_table_created_by_migration(self):
        """Running _create_table_cost_tracking twice is idempotent."""
        db = _make_db()
        # Should not raise even though the table already exists
        with db.transaction() as conn:
            db._create_table_cost_tracking(conn)

        rows = db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cost_tracking'"
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# TestModuleExports
# ---------------------------------------------------------------------------

class TestModuleExports:
    """__init__.py exports PricingTable, CostTracker, BudgetExceededError."""

    def test_pricing_table_export(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "PricingTable")
        assert orchestration_engine.PricingTable is PricingTable

    def test_cost_tracker_export(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "CostTracker")
        assert orchestration_engine.CostTracker is CostTracker

    def test_budget_exceeded_error_export(self):
        import orchestration_engine
        assert hasattr(orchestration_engine, "BudgetExceededError")
        assert orchestration_engine.BudgetExceededError is BudgetExceededError

    def test_all_contains_cost_tracker_names(self):
        import orchestration_engine
        assert "PricingTable" in orchestration_engine.__all__
        assert "CostTracker" in orchestration_engine.__all__
        assert "BudgetExceededError" in orchestration_engine.__all__
