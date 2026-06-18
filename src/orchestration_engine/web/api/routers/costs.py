"""Cost API route group for the REST API (Issue #942, sub-issue 952c).

Holds the cost-reporting routes, extracted *verbatim* from ``create_api_app``
via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``GET /api/v1/costs/summary``        (aggregate by day/template/model)
* ``GET /api/v1/costs/run/{run_id}``   (per-phase breakdown for one run)

The ``_VALID_GROUP_BY`` set and ``_DATE_RE`` compiled regex are used only by
these routes, so they move into ``register_cost_routes`` as nested locals —
verbatim — keeping the same validation behaviour the inline closures had.
"""

import re
from pathlib import Path
from typing import Any, Optional


def register_cost_routes(
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    effective_db_path: str,
) -> None:
    """Register the cost API route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, query params, validation, response shapes and
    error handling.
    """
    _VALID_GROUP_BY = {"day", "template", "model"}  # noqa: N806
    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # noqa: N806

    @app.get("/api/v1/costs/summary")
    async def cost_summary(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """Return aggregated cost data grouped by day, template, or model.

        Query parameters:
            start_date: Optional ISO date ``YYYY-MM-DD`` (inclusive lower bound).
            end_date:   Optional ISO date ``YYYY-MM-DD`` (inclusive upper bound).
            group_by:   Grouping dimension — ``day`` (default), ``template``, or
                        ``model``.
            limit:      Page size (default 20, max 100).
            offset:     Number of items to skip (default 0).

        Returns:
            200 with ``{"items": [...], "total": int, "limit": int, "offset": int}``.
            400 when ``group_by`` is invalid or date strings are malformed.
        """
        if group_by not in _VALID_GROUP_BY:
            raise HTTPException(
                status_code=400,
                detail=f"group_by must be one of {sorted(_VALID_GROUP_BY)}, got {group_by!r}",
            )
        if start_date is not None and not _DATE_RE.match(start_date):
            raise HTTPException(
                status_code=400,
                detail=f"start_date must be YYYY-MM-DD, got {start_date!r}",
            )
        if end_date is not None and not _DATE_RE.match(end_date):
            raise HTTPException(
                status_code=400,
                detail=f"end_date must be YYYY-MM-DD, got {end_date!r}",
            )
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        db = Database(Path(effective_db_path))
        items = db.get_cost_summary(start_date, end_date, group_by, limit, offset)
        total = db.count_cost_summary(start_date, end_date, group_by)
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/costs/run/{run_id}")
    async def cost_run_breakdown(run_id: str) -> JSONResponse:
        """Return per-phase cost records for a specific pipeline run.

        Path parameters:
            run_id: The pipeline run identifier.

        Returns:
            200 with ``{"run_id": str, "items": [...], "total_cost": float,
            "total_input_tokens": int, "total_output_tokens": int}``.
            404 when no cost records exist for the given ``run_id``.
        """
        db = Database(Path(effective_db_path))
        items = db.get_run_costs(run_id)
        if not items:
            raise HTTPException(
                status_code=404,
                detail=f"No cost records found for run '{run_id}'",
            )
        total_cost = sum(row.get("cost_usd", 0.0) or 0.0 for row in items)
        total_input = sum(row.get("input_tokens", 0) or 0 for row in items)
        total_output = sum(row.get("output_tokens", 0) or 0 for row in items)
        return JSONResponse(
            {
                "run_id": run_id,
                "items": items,
                "total_cost": total_cost,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
            }
        )
