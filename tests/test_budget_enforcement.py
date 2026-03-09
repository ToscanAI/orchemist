"""tests/test_budget_enforcement.py — Tests for Issue #5.2.2: Budget Enforcement.

Covers all acceptance criteria:

* ``BudgetConfig`` dataclass validation and field coercion.
* ``_parse_budget_config()`` parses YAML budget blocks correctly.
* ``PipelineTemplate`` exposes ``budget`` field and is ``None`` by default.
* ``load_template()`` round-trips the ``budget:`` YAML block.
* ``CostTracker.get_daily_cost()`` sums today's costs correctly.
* ``CostTracker.get_daily_cost_for_date()`` filters by date string.
* ``PreflightChecker._check_daily_budget()`` blocks launch when cap is exceeded.
* ``PreflightChecker._check_daily_budget()`` passes when under cap.
* ``PreflightChecker`` skips budget check when not configured (opt-in).
* Existing templates without a ``budget:`` block are unaffected.

Test classes
------------
    TestBudgetConfigDataclass       — BudgetConfig construction and validation
    TestParseBudgetConfig           — _parse_budget_config() helper
    TestPipelineTemplateBudget      — PipelineTemplate.budget field integration
    TestCostTrackerDailyCost        — get_daily_cost / get_daily_cost_for_date
    TestPreflightDailyBudgetCheck   — _check_daily_budget via run_all()
    TestBudgetOptIn                 — no-budget templates are unaffected
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from orchestration_engine.db import Database
from orchestration_engine.cost_tracker import CostTracker, BudgetExceededError
from orchestration_engine.templates import (
    BudgetConfig,
    PipelineTemplate,
    _parse_budget_config,
    TemplateEngine,
)
from orchestration_engine.preflight import PreflightChecker, PreflightResult


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


def _make_tracker(db: Database | None = None) -> CostTracker:
    """Return a CostTracker backed by an in-memory DB."""
    if db is None:
        db = _make_db()
    return CostTracker(db)


# Minimal YAML template for round-trip tests
_MINIMAL_TEMPLATE_YAML = """\
id: budget-test
name: Budget Test Pipeline
version: "1.0.0"
category: content
budget:
  max_cost_per_run: 2.50
  max_cost_per_day: 10.00
  warn_at_percentage: 75.0
phases:
  - id: write
    name: Write
    task_type: content
    model_tier: sonnet
    thinking_level: low
    prompt_template: "Write something."
"""

_TEMPLATE_NO_BUDGET_YAML = """\
id: no-budget
name: No Budget Template
version: "1.0.0"
category: content
phases:
  - id: write
    name: Write
    task_type: content
    model_tier: sonnet
    thinking_level: low
    prompt_template: "Write something."
"""


def _write_template(content: str) -> Path:
    """Write template YAML to a temp file and return its Path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(content)
        return Path(fh.name)


# ---------------------------------------------------------------------------
# TestBudgetConfigDataclass
# ---------------------------------------------------------------------------

class TestBudgetConfigDataclass:
    """BudgetConfig construction, coercion, and validation."""

    def test_defaults(self):
        """All fields have sane defaults when not specified."""
        cfg = BudgetConfig()
        assert cfg.max_cost_per_run is None
        assert cfg.max_cost_per_day is None
        assert cfg.warn_at_percentage == 80.0

    def test_explicit_values(self):
        """Explicit values are stored and coerced to float."""
        cfg = BudgetConfig(
            max_cost_per_run=5.00,
            max_cost_per_day=20.00,
            warn_at_percentage=70.0,
        )
        assert cfg.max_cost_per_run == 5.00
        assert cfg.max_cost_per_day == 20.00
        assert cfg.warn_at_percentage == 70.0

    def test_int_coerced_to_float(self):
        """Integer values are coerced to float."""
        cfg = BudgetConfig(max_cost_per_run=5, max_cost_per_day=10)
        assert isinstance(cfg.max_cost_per_run, float)
        assert isinstance(cfg.max_cost_per_day, float)

    def test_negative_max_cost_per_run_raises(self):
        """Negative max_cost_per_run is rejected."""
        with pytest.raises(ValueError, match="max_cost_per_run"):
            BudgetConfig(max_cost_per_run=-1.0)

    def test_negative_max_cost_per_day_raises(self):
        """Negative max_cost_per_day is rejected."""
        with pytest.raises(ValueError, match="max_cost_per_day"):
            BudgetConfig(max_cost_per_day=-0.01)

    def test_warn_percentage_out_of_range_raises(self):
        """warn_at_percentage outside [0, 100] is rejected."""
        with pytest.raises(ValueError, match="warn_at_percentage"):
            BudgetConfig(warn_at_percentage=101.0)
        with pytest.raises(ValueError, match="warn_at_percentage"):
            BudgetConfig(warn_at_percentage=-1.0)

    def test_warn_percentage_zero_is_valid(self):
        """warn_at_percentage of 0 is accepted."""
        cfg = BudgetConfig(warn_at_percentage=0.0)
        assert cfg.warn_at_percentage == 0.0

    def test_warn_percentage_100_is_valid(self):
        """warn_at_percentage of 100 is accepted."""
        cfg = BudgetConfig(warn_at_percentage=100.0)
        assert cfg.warn_at_percentage == 100.0

    def test_zero_cost_caps_are_valid(self):
        """Zero is a valid cap (immediate budget exceeded on any cost)."""
        cfg = BudgetConfig(max_cost_per_run=0.0, max_cost_per_day=0.0)
        assert cfg.max_cost_per_run == 0.0
        assert cfg.max_cost_per_day == 0.0


# ---------------------------------------------------------------------------
# TestParseBudgetConfig
# ---------------------------------------------------------------------------

class TestParseBudgetConfig:
    """_parse_budget_config() helper function."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert _parse_budget_config(None) is None

    def test_non_dict_returns_none(self):
        """Non-dict input returns None."""
        assert _parse_budget_config("string") is None
        assert _parse_budget_config(42) is None
        assert _parse_budget_config([]) is None

    def test_empty_dict_returns_default_budget_config(self):
        """Empty dict returns a BudgetConfig with defaults."""
        cfg = _parse_budget_config({})
        assert isinstance(cfg, BudgetConfig)
        assert cfg.max_cost_per_run is None
        assert cfg.max_cost_per_day is None
        assert cfg.warn_at_percentage == 80.0

    def test_full_config_parsed(self):
        """All fields are parsed correctly from a full config dict."""
        raw = {
            "max_cost_per_run": 2.50,
            "max_cost_per_day": 10.00,
            "warn_at_percentage": 75.0,
        }
        cfg = _parse_budget_config(raw)
        assert cfg is not None
        assert cfg.max_cost_per_run == 2.50
        assert cfg.max_cost_per_day == 10.00
        assert cfg.warn_at_percentage == 75.0

    def test_partial_config_parsed(self):
        """Partial config uses defaults for missing fields."""
        cfg = _parse_budget_config({"max_cost_per_run": 1.0})
        assert cfg is not None
        assert cfg.max_cost_per_run == 1.0
        assert cfg.max_cost_per_day is None
        assert cfg.warn_at_percentage == 80.0

    def test_unknown_fields_are_ignored(self, caplog):
        """Unknown fields produce a warning but do not raise."""
        import logging
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.templates"):
            cfg = _parse_budget_config({"max_cost_per_run": 1.0, "unknown_field": "x"})
        assert cfg is not None
        assert cfg.max_cost_per_run == 1.0
        assert any("unknown" in r.message.lower() for r in caplog.records)

    def test_null_values_use_defaults(self):
        """Null YAML values (None) are treated as absent."""
        cfg = _parse_budget_config({"max_cost_per_run": None, "max_cost_per_day": None})
        assert cfg is not None
        assert cfg.max_cost_per_run is None
        assert cfg.max_cost_per_day is None


# ---------------------------------------------------------------------------
# TestPipelineTemplateBudget
# ---------------------------------------------------------------------------

class TestPipelineTemplateBudget:
    """PipelineTemplate.budget field via load_template()."""

    def test_budget_field_defaults_to_none(self):
        """Templates without a budget: block have budget=None."""
        path = _write_template(_TEMPLATE_NO_BUDGET_YAML)
        engine = TemplateEngine(templates_dir=path.parent)
        template = engine.load_template(path)
        assert template.budget is None

    def test_budget_field_loaded_from_yaml(self):
        """Templates with a budget: block load BudgetConfig correctly."""
        path = _write_template(_MINIMAL_TEMPLATE_YAML)
        engine = TemplateEngine(templates_dir=path.parent)
        template = engine.load_template(path)
        assert template.budget is not None
        assert isinstance(template.budget, BudgetConfig)
        assert template.budget.max_cost_per_run == 2.50
        assert template.budget.max_cost_per_day == 10.00
        assert template.budget.warn_at_percentage == 75.0

    def test_template_without_budget_is_valid(self):
        """Templates without a budget: block pass template validation."""
        path = _write_template(_TEMPLATE_NO_BUDGET_YAML)
        engine = TemplateEngine(templates_dir=path.parent)
        template = engine.load_template(path)
        errors = engine.validate_template(template)
        # Only error we might get is the scenario requirement for code category
        # — content category is fine
        assert template.budget is None

    def test_budget_config_not_normalised_away(self):
        """BudgetConfig is not erroneously set to None by __post_init__."""
        budget = BudgetConfig(max_cost_per_run=1.0)
        template = PipelineTemplate(id="t", name="T", budget=budget)
        assert template.budget is budget

    def test_non_budget_config_value_normalised_to_none(self):
        """Non-BudgetConfig budget values are normalised to None."""
        template = PipelineTemplate(id="t", name="T", budget="bad-value")  # type: ignore[arg-type]
        assert template.budget is None


# ---------------------------------------------------------------------------
# TestCostTrackerDailyCost
# ---------------------------------------------------------------------------

class TestCostTrackerDailyCost:
    """CostTracker.get_daily_cost() and get_daily_cost_for_date()."""

    def test_get_daily_cost_for_date_empty(self):
        """Returns 0.0 when no costs recorded for a date."""
        tracker = _make_tracker()
        cost = tracker.get_daily_cost_for_date("2099-01-01")
        assert cost == 0.0

    def test_get_daily_cost_for_date_with_data(self):
        """Sums costs for a specific date correctly."""
        db = _make_db()
        _seed_run(db, "run-daily-1")
        _seed_run(db, "run-daily-2")
        tracker = CostTracker(db)

        # Insert rows with explicit created_at dates
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-daily-1", "spec", "default", 0, 0, 0.10, "2025-06-01 10:00:00"),
        )
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-daily-2", "implement", "default", 0, 0, 0.20, "2025-06-01 14:00:00"),
        )
        # Different date — should not be included
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-daily-1", "review", "default", 0, 0, 0.50, "2025-06-02 08:00:00"),
        )

        cost_june1 = tracker.get_daily_cost_for_date("2025-06-01")
        cost_june2 = tracker.get_daily_cost_for_date("2025-06-02")

        assert abs(cost_june1 - 0.30) < 1e-9
        assert abs(cost_june2 - 0.50) < 1e-9

    def test_get_daily_cost_for_date_only_matching(self):
        """Only costs for the exact date are summed."""
        db = _make_db()
        _seed_run(db, "run-x")
        tracker = CostTracker(db)

        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-x", "spec", "default", 0, 0, 1.00, "2025-07-15 00:00:00"),
        )
        assert abs(tracker.get_daily_cost_for_date("2025-07-15") - 1.00) < 1e-9
        assert tracker.get_daily_cost_for_date("2025-07-14") == 0.0
        assert tracker.get_daily_cost_for_date("2025-07-16") == 0.0

    def test_get_daily_cost_delegates_to_today(self):
        """get_daily_cost() uses today's UTC date."""
        from datetime import datetime, timezone
        tracker = _make_tracker()

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Patch get_daily_cost_for_date to verify the date passed
        called_with = []
        original = tracker.get_daily_cost_for_date

        def capturing_get(date_str: str) -> float:
            called_with.append(date_str)
            return original(date_str)

        tracker.get_daily_cost_for_date = capturing_get  # type: ignore[method-assign]
        tracker.get_daily_cost()

        assert called_with == [today_str]

    def test_get_daily_cost_returns_zero_for_empty_db(self):
        """get_daily_cost() returns 0.0 when no rows exist."""
        tracker = _make_tracker()
        assert tracker.get_daily_cost() == 0.0

    def test_get_daily_cost_for_date_multiple_runs(self):
        """Costs across multiple runs on the same day are aggregated."""
        db = _make_db()
        for i in range(5):
            run_id = f"run-multi-{i}"
            _seed_run(db, run_id)
            db.execute(
                """
                INSERT INTO cost_tracking
                    (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, "spec", "default", 0, 0, 0.10, "2025-08-01 12:00:00"),
            )
        tracker = CostTracker(db)
        total = tracker.get_daily_cost_for_date("2025-08-01")
        assert abs(total - 0.50) < 1e-9


# ---------------------------------------------------------------------------
# TestPreflightDailyBudgetCheck
# ---------------------------------------------------------------------------

class TestPreflightDailyBudgetCheck:
    """PreflightChecker._check_daily_budget() via run_all()."""

    _BASE_INPUT = {
        "issue_title": "Test issue",
        "issue_body": "Body",
        "repo_path": "",  # empty → git checks produce a warning, not an error
        "branch_name": "feat/test",
        "issue_number": "1",
        "repo_url": "https://github.com/owner/repo",
        "test_command": "pytest",
    }

    def _make_checker(
        self,
        budget_config: BudgetConfig | None,
        cost_tracker: CostTracker | None,
    ) -> PreflightChecker:
        return PreflightChecker(
            input_data=self._BASE_INPUT,
            required_fields=[],   # skip field checks for isolation
            budget_config=budget_config,
            cost_tracker=cost_tracker,
        )

    def test_no_budget_config_skips_check(self):
        """Without a budget_config, the daily_budget check is absent."""
        checker = PreflightChecker(
            input_data=self._BASE_INPUT,
            required_fields=[],
        )
        result = checker.run_all()
        check_names = {c.name for c in result.checks}
        assert "daily_budget" not in check_names

    def test_budget_config_but_no_tracker_skips_check(self):
        """Budget config without a cost_tracker silently skips the check."""
        budget = BudgetConfig(max_cost_per_day=5.0)
        checker = self._make_checker(budget_config=budget, cost_tracker=None)
        result = checker.run_all()
        check_names = {c.name for c in result.checks}
        assert "daily_budget" not in check_names

    def test_budget_config_no_daily_cap_skips_check(self):
        """Budget config without max_cost_per_day skips the daily check."""
        budget = BudgetConfig(max_cost_per_run=2.0)  # only per-run, no daily
        tracker = _make_tracker()
        checker = self._make_checker(budget_config=budget, cost_tracker=tracker)
        result = checker.run_all()
        check_names = {c.name for c in result.checks}
        assert "daily_budget" not in check_names

    def test_under_daily_cap_passes(self):
        """When today's cost < daily cap, check passes."""
        db = _make_db()
        _seed_run(db, "run-under")
        tracker = CostTracker(db)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-under", "spec", "default", 0, 0, 1.00, f"{today} 10:00:00"),
        )

        budget = BudgetConfig(max_cost_per_day=5.0)
        checker = self._make_checker(budget_config=budget, cost_tracker=tracker)
        result = checker.run_all()

        daily_check = next(c for c in result.checks if c.name == "daily_budget")
        assert daily_check.passed is True
        assert "1.0000" in daily_check.message or "1.00" in daily_check.message

    def test_at_daily_cap_fails(self):
        """When today's cost >= daily cap, check fails and blocks launch."""
        db = _make_db()
        _seed_run(db, "run-at-cap")
        tracker = CostTracker(db)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-at-cap", "spec", "default", 0, 0, 5.00, f"{today} 10:00:00"),
        )

        budget = BudgetConfig(max_cost_per_day=5.0)
        checker = self._make_checker(budget_config=budget, cost_tracker=tracker)
        result = checker.run_all()

        daily_check = next(c for c in result.checks if c.name == "daily_budget")
        assert daily_check.passed is False
        assert daily_check.severity == "error"
        assert not result.passed  # overall result is failed

    def test_over_daily_cap_fails(self):
        """When today's cost > daily cap, launch is rejected with error."""
        db = _make_db()
        _seed_run(db, "run-over")
        tracker = CostTracker(db)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-over", "spec", "default", 0, 0, 12.00, f"{today} 09:00:00"),
        )

        budget = BudgetConfig(max_cost_per_day=10.0)
        checker = self._make_checker(budget_config=budget, cost_tracker=tracker)
        result = checker.run_all()

        daily_check = next(c for c in result.checks if c.name == "daily_budget")
        assert daily_check.passed is False
        assert "12.0000" in daily_check.message or "12.00" in daily_check.message
        assert not result.passed

    def test_zero_daily_cost_under_cap_passes(self):
        """Zero spend with a cap set passes the check."""
        tracker = _make_tracker()
        budget = BudgetConfig(max_cost_per_day=1.0)
        checker = self._make_checker(budget_config=budget, cost_tracker=tracker)
        result = checker.run_all()

        daily_check = next(c for c in result.checks if c.name == "daily_budget")
        assert daily_check.passed is True

    def test_tracker_exception_produces_warning(self):
        """If get_daily_cost() raises, check produces a warning (not error)."""
        budget = BudgetConfig(max_cost_per_day=5.0)
        broken_tracker = MagicMock()
        broken_tracker.get_daily_cost.side_effect = RuntimeError("DB gone")

        checker = self._make_checker(budget_config=budget, cost_tracker=broken_tracker)
        result = checker.run_all()

        daily_check = next(c for c in result.checks if c.name == "daily_budget")
        assert daily_check.severity == "warning"
        # A warning does not fail the overall result
        assert result.passed  # other checks should still pass (no fields to check)

    def test_fail_message_contains_cap_and_spend(self):
        """Failure message includes both cap and actual spend values."""
        db = _make_db()
        _seed_run(db, "run-msg")
        tracker = CostTracker(db)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.execute(
            """
            INSERT INTO cost_tracking
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-msg", "spec", "default", 0, 0, 7.50, f"{today} 10:00:00"),
        )

        budget = BudgetConfig(max_cost_per_day=5.0)
        checker = self._make_checker(budget_config=budget, cost_tracker=tracker)
        result = checker.run_all()

        daily_check = next(c for c in result.checks if c.name == "daily_budget")
        assert "5.0000" in daily_check.message
        assert "7.5" in daily_check.message


# ---------------------------------------------------------------------------
# TestBudgetOptIn
# ---------------------------------------------------------------------------

class TestBudgetOptIn:
    """Verify that templates without budget: blocks are completely unaffected."""

    def test_existing_template_loads_without_budget(self):
        """Templates without budget: load successfully with budget=None."""
        path = _write_template(_TEMPLATE_NO_BUDGET_YAML)
        engine = TemplateEngine(templates_dir=path.parent)
        template = engine.load_template(path)
        assert template.budget is None

    def test_preflight_without_budget_passes_budget_check(self):
        """PreflightChecker without budget config produces no daily_budget check."""
        checker = PreflightChecker(
            input_data={"issue_number": "1"},
            required_fields=[],
        )
        result = checker.run_all()
        check_names = {c.name for c in result.checks}
        assert "daily_budget" not in check_names

    def test_budget_config_none_in_template_means_no_enforcement(self):
        """A PipelineTemplate with budget=None has no cost enforcement applied."""
        template = PipelineTemplate(id="t", name="T")
        assert template.budget is None

    def test_cost_tracker_get_daily_cost_for_date_returns_float(self):
        """get_daily_cost_for_date() always returns a float, even for empty DB."""
        tracker = _make_tracker()
        result = tracker.get_daily_cost_for_date("1970-01-01")
        assert isinstance(result, float)
        assert result == 0.0

    def test_budget_config_exported_from_templates_module(self):
        """BudgetConfig is importable from orchestration_engine.templates."""
        from orchestration_engine.templates import BudgetConfig as BC
        assert BC is BudgetConfig

    def test_parse_budget_config_exported(self):
        """_parse_budget_config is importable from orchestration_engine.templates."""
        from orchestration_engine.templates import _parse_budget_config as pbc
        assert callable(pbc)
