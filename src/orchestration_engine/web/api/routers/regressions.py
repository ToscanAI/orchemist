"""Harness aggregate read-endpoint route group for the REST API (Issue #942, sub-issue 952d).

Holds the four harness "Fleet Dashboard / Trust & Gates side-panel" read
aggregates (items 4, 6, 7 from the post-0.10 audit), extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``GET /api/v1/regressions``      (regression queue, newest first)
* ``GET /api/v1/stale-findings``   (stale-detection findings — empty until scanner ships)
* ``GET /api/v1/trust-profiles``   (all trust calibration profiles)
* ``GET /api/v1/decisions``        (recent review outcomes / decisions)

These are pure DB reads with reasonable pagination caps (no writes). They form a
cohesive group: each opens its own :class:`~orchestration_engine.db.Database` on
``effective_db_path``, three of the four normalise timestamps via the shared
``normalize_row`` helper, and all back the harness read-side cards. The
``_normalize_row`` helper is used *only* by these routes, so it is imported here
directly from :mod:`orchestration_engine.timestamps` — the same module the inline
closures referenced it from (``_app`` aliased ``normalize_row as _normalize_row``
at the top of the factory). ``Database`` is received as a keyword argument — the
same object the inline closures used.
"""

from pathlib import Path
from typing import Any, List, Optional

from orchestration_engine.timestamps import normalize_row as _normalize_row


def register_regression_routes(
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    Database: Any,  # noqa: N803
    effective_db_path: str,
) -> None:
    """Register the harness aggregate read-endpoint route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, query params, clamping, BEGIN DEFERRED snapshot
    semantics, ordering, response shapes and error handling.
    """

    @app.get("/api/v1/regressions")
    async def list_regressions_endpoint(
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        """List regression records (from `regressions` table, newest first).

        Backs the Fleet Dashboard "Regression queue" card. Returns the
        canonical engine shape with timestamps normalised to UTC `Z` strings.

        Optional ``status`` filter (e.g. ``'detected'``, ``'fixing'``,
        ``'resolved'``). Limit clamped to [1, 200].
        """
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        # NOTE on consistency: `_locked()` is a no-op for file-based DBs
        # (production); SQLite's default isolation gives us autocommit
        # snapshot semantics per statement. To make items + total truly
        # consistent we wrap both in an explicit BEGIN DEFERRED ... COMMIT
        # transaction so they share one snapshot. The transaction is
        # read-only, so contention with writers is minimal.
        base = "SELECT * FROM regressions"
        where: List[str] = []
        params: List[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        wclause = (" WHERE " + " AND ".join(where)) if where else ""
        # Stable secondary sort by id — `created_at` has 1-second resolution
        # and adjacent rows often share a timestamp, so without a tiebreaker
        # offset-based pagination skips or repeats rows.
        list_q = base + wclause + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        count_q = "SELECT COUNT(*) FROM regressions" + wclause
        with db._locked():
            conn = db.get_connection()
            try:
                conn.execute("BEGIN DEFERRED")
                rows = conn.execute(list_q, params + [limit, offset]).fetchall()
                total = conn.execute(count_q, params).fetchone()[0]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        items = [_normalize_row(db._row_to_dict(r)) for r in rows]
        return JSONResponse(
            {
                "items": items,
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/stale-findings")
    async def list_stale_findings_endpoint() -> JSONResponse:
        """List stale-detection findings (ROADMAP §3.5).

        Returns an empty list until the stale scanner ships (it's the
        last open Phase-3 item in ROADMAP.md). The endpoint exists today
        so the harness Fleet Dashboard stale card can hit a real URL and
        get an empty list with a status marker, instead of hardcoding
        demo data forever.

        Response shape mirrors `/api/v1/regressions` for consistency.
        """
        return JSONResponse(
            {
                "items": [],
                "total": 0,
                "scan_status": "no_scanner_yet",
                "next_scan_at": None,
            }
        )

    @app.get("/api/v1/trust-profiles")
    async def list_trust_profiles_endpoint() -> JSONResponse:
        """Return all trust calibration profiles.

        Backs the Trust & Gates side panel. Profiles are keyed by
        (repo, template_id, task_type) per `trust.py` — the harness
        renders the key as a single composed label and the confidence
        as a bar relative to the threshold.

        Ordered ``last_run_at DESC NULLS LAST`` so the most-recently-active
        profiles surface first when the side panel slices the top N.

        No pagination — the active profile set is bounded by the number
        of (repo, template, task) tuples in use, typically O(10s).
        """
        db = Database(Path(effective_db_path))
        with db._locked():
            conn = db.get_connection()
            rows = conn.execute(
                "SELECT * FROM trust_profiles "
                "ORDER BY (last_run_at IS NULL), last_run_at DESC, id ASC"
            ).fetchall()
        items = [_normalize_row(db._row_to_dict(r)) for r in rows]
        return JSONResponse({"items": items, "total": len(items)})

    @app.get("/api/v1/decisions")
    async def list_decisions_endpoint(limit: int = 50, offset: int = 0) -> JSONResponse:
        """List recent review outcomes ('decisions') for the audit trail.

        Backs the Trust & Gates "Recent decisions" card. Each row is one
        APPROVE / REQUEST_CHANGES verdict on one run, recorded in the
        `review_outcomes` table by the engine when a reviewer phase
        completes.

        Returns the canonical `review_outcomes` row shape: ``review_id``,
        ``run_id``, ``phase_id``, ``reviewer_model``, ``verdict``,
        ``issues_found`` (list of dicts), ``fix_verified`` (0/1), and
        ``created_at`` (UTC `Z` string). Limit clamped to [1, 100].

        Ordered by ``created_at DESC, review_id DESC`` for stable pagination.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        # See `/regressions` for the same BEGIN DEFERRED rationale —
        # share one read snapshot across items + total.
        with db._locked():
            conn = db.get_connection()
            try:
                conn.execute("BEGIN DEFERRED")
                rows = conn.execute(
                    "SELECT * FROM review_outcomes "
                    "ORDER BY created_at DESC, review_id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM review_outcomes").fetchone()[0]
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        items = [_normalize_row(db._row_to_dict(r)) for r in rows]
        return JSONResponse(
            {
                "items": items,
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }
        )
