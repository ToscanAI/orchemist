"""Review-queue route group for the REST API (Issue #942, sub-issue 952c).

Holds the human-review queue routes, extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``GET  /api/v1/reviews``                     (list pending reviews)
* ``POST /api/v1/reviews/{run_id}/approve``    (approve)
* ``POST /api/v1/reviews/{run_id}/reject``     (reject)

``ApproveRequest`` / ``RejectRequest`` (used only by these routes) move with the
routes into ``register_review_routes``; they are defined inside the register
function with a lazy ``from pydantic import BaseModel`` so importing this module
does NOT eagerly pull ``pydantic`` (preserving the optional ``[api]`` extra
contract), exactly as the inline definitions did inside ``create_api_app``.

``_review_row_to_dict`` is used only by these routes, so it moves into
``register_review_routes`` as a nested local — verbatim.  The shared factory-local
helper ``_run_to_dict`` is also used by routes that remain in ``_app.py`` (and
``routers/runs.py``), so it stays there and is received here as a keyword argument
— the same object the inline closures used.
"""

from pathlib import Path
from typing import Any, Dict, Optional


def register_review_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    effective_db_path: str,
    _run_to_dict: Any,
) -> None:
    """Register the review-queue route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, status codes, params, validation, response shapes
    and error handling.

    ``ApproveRequest`` / ``RejectRequest`` are defined here (not at module scope)
    with a lazy ``from pydantic import BaseModel`` so importing this module does
    NOT eagerly pull ``pydantic`` — preserving the optional ``[api]`` extra
    contract, exactly as the inline definitions did inside ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class ApproveRequest(BaseModel):
        """Body for POST /api/v1/reviews/{run_id}/approve."""

        reviewed_by: Optional[str] = None
        """Optional operator identifier stored with the review record."""

        note: Optional[str] = None
        """Optional approval note stored in ``review_reason``."""

    class RejectRequest(BaseModel):
        """Body for POST /api/v1/reviews/{run_id}/reject."""

        reason: str
        """Mandatory rejection reason stored in ``review_reason``."""

        reviewed_by: Optional[str] = None
        """Optional operator identifier stored with the review record."""

    # ------------------------------------------------------------------
    # Helper — build ReviewResponse dict from an enriched pending-review row
    # ------------------------------------------------------------------

    def _review_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert an enriched pending-review DB row to a ReviewResponse dict."""
        return {
            "run_id": row.get("run_id", ""),
            "template_id": row.get("template_id", ""),
            "status": row.get("status", ""),
            "created_at": row.get("created_at"),
            "completed_at": row.get("completed_at"),
            "review_reason": row.get("review_reason"),
            "reviewed_at": row.get("reviewed_at"),
            "reviewed_by": row.get("reviewed_by"),
            "confidence_score": row.get("confidence_score"),
            "tier_name": row.get("tier_name"),
            "action": row.get("action"),
            "justification": row.get("justification"),
        }

    @app.get("/api/v1/reviews")
    async def list_reviews(
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """List pipeline runs currently awaiting human review.

        Returns enriched run records that include confidence score, tier,
        action, and justification from the associated routing decision.

        Query parameters:
            limit:  Maximum number of results (default 20, max 100).
            offset: Number of results to skip (for pagination).

        Returns:
            JSON object with ``items`` array, ``total`` count, ``limit``,
            and ``offset`` fields.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        items = db.list_pending_reviews(limit=limit, offset=offset)
        total = db.count_pending_reviews()
        return JSONResponse(
            {
                "items": [_review_row_to_dict(r) for r in items],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.post("/api/v1/reviews/{run_id}/approve", status_code=200)
    async def approve_review(run_id: str, body: ApproveRequest) -> JSONResponse:
        """Approve a pending-review pipeline run.

        Transitions the run from ``pending_review`` to ``success`` and records
        the reviewer identity and optional note.

        Path parameter:
            run_id: Pipeline run identifier.

        Request body (JSON):
            reviewed_by (str, optional): Operator identifier.
            note (str, optional): Approval note.

        Returns:
            - **200** with the updated run dict on success.
            - **404** when the run is not found.
            - **409** when the run is not in ``pending_review`` status.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )
        if run.get("status") != "pending_review":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is in status '{run.get('status')}', "
                    "not 'pending_review'. Only pending_review runs can be approved."
                ),
            )
        ok = db.approve_pipeline_run(
            run_id=run_id,
            reviewed_by=body.reviewed_by,
            note=body.note,
        )
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"Could not approve run '{run_id}'",
            )
        run = db.get_pipeline_run(run_id)
        return JSONResponse(_run_to_dict(run))

    @app.post("/api/v1/reviews/{run_id}/reject", status_code=200)
    async def reject_review(run_id: str, body: RejectRequest) -> JSONResponse:
        """Reject a pending-review pipeline run.

        Transitions the run from ``pending_review`` to ``rejected`` and records
        the rejection reason and reviewer identity.

        Path parameter:
            run_id: Pipeline run identifier.

        Request body (JSON):
            reason (str): Mandatory rejection reason.
            reviewed_by (str, optional): Operator identifier.

        Returns:
            - **200** with the updated run dict on success.
            - **404** when the run is not found.
            - **409** when the run is not in ``pending_review`` status.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found",
            )
        if run.get("status") != "pending_review":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is in status '{run.get('status')}', "
                    "not 'pending_review'. Only pending_review runs can be rejected."
                ),
            )
        ok = db.reject_pipeline_run(
            run_id=run_id,
            reason=body.reason,
            reviewed_by=body.reviewed_by,
        )
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=f"Could not reject run '{run_id}'",
            )
        run = db.get_pipeline_run(run_id)
        return JSONResponse(_run_to_dict(run))
