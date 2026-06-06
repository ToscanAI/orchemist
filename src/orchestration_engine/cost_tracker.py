"""cost_tracker.py — Per-phase LLM cost tracking (Issue #5.2.1).

Provides two classes:

* :class:`PricingTable` — loads ``pricing.yaml`` from the package directory and
  exposes ``compute_cost()`` for calculating phase costs from token counts.

* :class:`CostTracker` — persists per-phase cost records to the ``cost_tracking``
  SQLite table and checks cumulative run costs against optional budgets.

Typical usage
-------------
::

    from orchestration_engine.db import Database
    from orchestration_engine.cost_tracker import CostTracker, PricingTable

    db = Database()
    tracker = CostTracker(db)

    # Record a phase execution
    record = tracker.record_phase(
        run_id="run-abc123",
        phase_id="spec",
        model="anthropic/claude-sonnet-4-6",
        input_tokens=1200,
        output_tokens=800,
    )
    print(record["cost_usd"])  # e.g. 0.01560

    # Enforce a budget ceiling
    tracker.check_budget(run_id="run-abc123", budget_usd=1.00)
    # Raises BudgetExceededError if cumulative cost > 1.00
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover — yaml is a project dependency
    raise ImportError(
        "PyYAML is required by cost_tracker. "
        "Install it with: pip install pyyaml"
    )

from .db import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default pricing YAML path (package-local)
# ---------------------------------------------------------------------------
_DEFAULT_PRICING_PATH = Path(__file__).parent / "pricing.yaml"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class BudgetExceededError(Exception):
    """Raised when a run's cumulative cost exceeds the allowed budget.

    Attributes:
        run_id:         The pipeline run identifier.
        budget_usd:     The enforced budget in USD.
        actual_usd:     The cumulative cost that triggered the check.
    """

    def __init__(self, run_id: str, budget_usd: float, actual_usd: float) -> None:
        self.run_id = run_id
        self.budget_usd = budget_usd
        self.actual_usd = actual_usd
        super().__init__(
            f"Run '{run_id}' has exceeded its budget of ${budget_usd:.4f} USD "
            f"(actual: ${actual_usd:.4f} USD)"
        )


# ---------------------------------------------------------------------------
# PricingTable
# ---------------------------------------------------------------------------


class PricingTable:
    """Loads and queries per-model token pricing from a YAML file.

    The YAML file maps model identifiers to ``input_per_million`` and
    ``output_per_million`` prices in USD.  A ``default`` entry is used as a
    fallback for unknown models.

    Args:
        pricing_path: Path to the YAML pricing file.  Defaults to the
            ``pricing.yaml`` shipped alongside this module.

    Example::

        table = PricingTable()
        cost = table.compute_cost(
            model="anthropic/claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )
        print(f"${cost:.6f}")  # $0.010500
    """

    def __init__(self, pricing_path: Optional[Path] = None) -> None:
        self._pricing_path = pricing_path or _DEFAULT_PRICING_PATH
        self._data: Dict[str, Dict[str, float]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load and validate the pricing YAML file."""
        path = Path(self._pricing_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Pricing YAML not found at: {path}"
            )
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        if not isinstance(raw, dict) or "models" not in raw:
            raise ValueError(
                f"Pricing YAML at {path} must contain a top-level 'models' key."
            )

        models = raw["models"]
        if not isinstance(models, dict):
            raise ValueError(
                f"'models' in {path} must be a mapping of model names to pricing entries."
            )

        parsed: Dict[str, Dict[str, float]] = {}
        for model_name, entry in models.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Pricing entry for '{model_name}' must be a dict with "
                    f"'input_per_million' and 'output_per_million' keys."
                )
            try:
                parsed[model_name] = {
                    "input_per_million": float(entry["input_per_million"]),
                    "output_per_million": float(entry["output_per_million"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid pricing entry for '{model_name}': {exc}"
                ) from exc

        self._data = parsed
        logger.debug(
            "PricingTable loaded %d model entries from %s",
            len(parsed),
            path,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_pricing(self, model: str) -> Dict[str, float]:
        """Return the pricing dict for *model*, falling back to ``default``.

        Args:
            model: Model identifier (e.g. ``"anthropic/claude-sonnet-4-6"``).

        Returns:
            Dict with keys ``input_per_million`` and ``output_per_million``.

        Raises:
            KeyError: If *model* is not found **and** no ``default`` entry exists.
        """
        if model in self._data:
            return self._data[model]
        if "default" in self._data:
            logger.debug(
                "Model '%s' not found in pricing table; using 'default' entry.",
                model,
            )
            return self._data["default"]
        raise KeyError(
            f"Model '{model}' not in pricing table and no 'default' fallback defined."
        )

    def compute_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Calculate the USD cost for a given model and token usage.

        Cost is computed as::

            cost = (input_tokens  / 1_000_000) * input_per_million
                 + (output_tokens / 1_000_000) * output_per_million

        Args:
            model:         Model identifier.
            input_tokens:  Number of prompt/input tokens consumed.
            output_tokens: Number of completion/output tokens generated.

        Returns:
            Computed cost in USD as a float.

        Raises:
            ValueError: If either token count is negative.
            KeyError:   If *model* has no entry and no ``default`` fallback.
        """
        if input_tokens < 0:
            raise ValueError(f"input_tokens must be >= 0, got {input_tokens}")
        if output_tokens < 0:
            raise ValueError(f"output_tokens must be >= 0, got {output_tokens}")

        pricing = self.get_pricing(model)
        cost = (
            (input_tokens / 1_000_000) * pricing["input_per_million"]
            + (output_tokens / 1_000_000) * pricing["output_per_million"]
        )
        return round(cost, 10)  # avoid floating-point drift in storage

    @property
    def known_models(self) -> List[str]:
        """List of all model identifiers explicitly defined in the pricing file."""
        return [k for k in self._data if k != "default"]

    def has_model(self, model: str) -> bool:
        """Return ``True`` if *model* has an explicit (non-default) pricing entry."""
        return model in self._data and model != "default"


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


class CostTracker:
    """Persists per-phase LLM cost records and enforces budget limits.

    Each call to :meth:`record_phase` inserts a row into the ``cost_tracking``
    table with the computed USD cost for the phase.  :meth:`get_run_cost`
    aggregates rows by ``run_id``, and :meth:`check_budget` raises
    :class:`BudgetExceededError` if the cumulative cost exceeds a ceiling.

    Args:
        db:           :class:`~orchestration_engine.db.Database` instance.
        pricing_path: Optional path to a custom pricing YAML.  Defaults to the
            package-bundled ``pricing.yaml``.

    Example::

        tracker = CostTracker(db)
        tracker.record_phase(
            run_id="run-abc",
            phase_id="implement",
            model="anthropic/claude-opus-4-6",
            input_tokens=2000,
            output_tokens=1000,
        )
        tracker.check_budget(run_id="run-abc", budget_usd=5.0)
    """

    def __init__(
        self,
        db: Database,
        pricing_path: Optional[Path] = None,
    ) -> None:
        self._db = db
        self._pricing = PricingTable(pricing_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_phase(
        self,
        run_id: str,
        phase_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Dict[str, Any]:
        """Record a phase execution cost and persist it to ``cost_tracking``.

        Computes the USD cost from the pricing table, inserts a row into
        ``cost_tracking``, and returns the full record dict.

        Args:
            run_id:        Pipeline run identifier (must exist in
                           ``pipeline_runs`` for the FK to be valid, though
                           this is not enforced in dry-run / test mode).
            phase_id:      Phase name/identifier (e.g. ``"spec"``, ``"implement"``).
            model:         Model used for this phase.
            input_tokens:  Input tokens consumed.
            output_tokens: Output tokens generated.

        Returns:
            Dict with keys: ``run_id``, ``phase_id``, ``model``,
            ``input_tokens``, ``output_tokens``, ``cost_usd``.

        Raises:
            ValueError: If token counts are negative.
            KeyError:   If model is unknown and pricing has no default.
        """
        cost_usd = self._pricing.compute_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cost_tracking
                    (run_id, phase_id, model, input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, phase_id, model, input_tokens, output_tokens, cost_usd),
            )

        record = {
            "run_id": run_id,
            "phase_id": phase_id,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
        logger.debug(
            "Recorded cost for run='%s' phase='%s': $%.6f (in=%d, out=%d, model=%s)",
            run_id,
            phase_id,
            cost_usd,
            input_tokens,
            output_tokens,
            model,
        )
        return record

    def get_run_cost(self, run_id: str) -> float:
        """Return the cumulative USD cost for all phases of *run_id*.

        Args:
            run_id: Pipeline run identifier.

        Returns:
            Sum of ``cost_usd`` for all rows matching *run_id*, or ``0.0``
            if no rows exist.
        """
        row = self._db.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM cost_tracking WHERE run_id = ?",
            (run_id,),
        )
        return float(row["total"]) if row else 0.0

    def get_run_phases(self, run_id: str) -> List[Dict[str, Any]]:
        """Return all cost records for *run_id* ordered by insertion time.

        Args:
            run_id: Pipeline run identifier.

        Returns:
            List of dicts with keys: ``id``, ``run_id``, ``phase_id``,
            ``model``, ``input_tokens``, ``output_tokens``, ``cost_usd``,
            ``created_at``.
        """
        return self._db.fetch_all(
            "SELECT * FROM cost_tracking WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        )

    def check_budget(self, run_id: str, budget_usd: float) -> float:
        """Raise :class:`BudgetExceededError` if run cost exceeds *budget_usd*.

        Args:
            run_id:     Pipeline run identifier.
            budget_usd: Maximum allowed cumulative cost in USD.

        Returns:
            Current cumulative cost if under budget.

        Raises:
            BudgetExceededError: If the run's cumulative cost exceeds *budget_usd*.
            ValueError:          If *budget_usd* is negative.
        """
        if budget_usd < 0:
            raise ValueError(f"budget_usd must be >= 0, got {budget_usd}")

        actual = self.get_run_cost(run_id)
        if actual > budget_usd:
            raise BudgetExceededError(
                run_id=run_id,
                budget_usd=budget_usd,
                actual_usd=actual,
            )
        return actual

    def get_daily_cost(self) -> float:
        """Return the total USD cost across all runs for today (UTC).

        Queries the ``cost_tracking`` table and sums ``cost_usd`` for all
        rows whose ``created_at`` timestamp falls on today's UTC date.

        Returns:
            Sum of ``cost_usd`` for today's rows, or ``0.0`` if none exist.
        """
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.get_daily_cost_for_date(today_str)

    def get_daily_cost_for_date(self, date_str: str) -> float:
        """Return the total USD cost across all runs for a specific UTC date.

        Useful for testing with a fixed date rather than relying on ``now()``.

        Args:
            date_str: Date in ``"YYYY-MM-DD"`` format (UTC).

        Returns:
            Sum of ``cost_usd`` for rows matching *date_str*, or ``0.0``.
        """
        row = self._db.fetch_one(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total
            FROM cost_tracking
            WHERE DATE(created_at) = ?
            """,
            (date_str,),
        )
        return float(row["total"]) if row else 0.0

    # ------------------------------------------------------------------
    # Convenience property
    # ------------------------------------------------------------------

    @property
    def pricing(self) -> PricingTable:
        """Expose the underlying :class:`PricingTable` for direct price lookups."""
        return self._pricing
