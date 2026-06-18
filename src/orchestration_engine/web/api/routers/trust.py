"""Trust-profile API route group for the REST API (Issue #942, sub-issue 952c).

Holds the trust-profile read + manual-override + adjustment-log routes,
extracted *verbatim* from ``create_api_app`` via the register-function pattern
(see :mod:`orchestration_engine.web.api.routers`):

* ``GET /api/v1/trust/profiles``                (list)
* ``GET /api/v1/trust/profiles/{profile_id}``   (detail)
* ``PUT /api/v1/trust/profiles/{profile_id}``   (manual override)
* ``GET /api/v1/trust/adjustments``             (audit log for a profile)

``TrustOverrideRequest`` (used only by the override route) moves with the routes
into ``register_trust_routes``; it is defined inside the register function with a
lazy ``from pydantic import BaseModel`` so importing this module does NOT eagerly
pull ``pydantic`` (preserving the optional ``[api]`` extra contract), exactly as
the inline definition did inside ``create_api_app``.

The lazy in-closure import of ``TrustCalibrator`` is preserved (spelled as the
absolute ``from orchestration_engine.trust import TrustCalibrator`` — the same
module the inline ``from ...trust import TrustCalibrator`` resolved to from
``_app``).
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def register_trust_routes(
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    effective_db_path: str,
) -> None:
    """Register the trust-profile API route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, params, validation, threshold re-derivation,
    audit-log writes, response shapes and error handling.

    ``TrustOverrideRequest`` is defined here (not at module scope) with a lazy
    ``from pydantic import BaseModel`` so importing this module does NOT eagerly
    pull ``pydantic`` — preserving the optional ``[api]`` extra contract, exactly
    as the inline definition did inside ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class TrustOverrideRequest(BaseModel):
        """Body for PUT /api/v1/trust/profiles/{profile_id} — manual override."""

        trust_score: float
        """New trust score to set, in [0.0, 1.0]."""

        reason: str
        """Human-readable justification for the manual override."""

        reviewed_by: Optional[str] = None
        """Optional operator identifier stored in the audit log."""

    @app.get("/api/v1/trust/profiles")
    async def list_trust_profiles(
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        """List all trust profiles, ordered by id ASC.

        Query parameters:
            limit:  Maximum number of results (default 100, max 500).
            offset: Number of rows to skip for pagination (default 0).

        Returns:
            JSON object with ``items`` array, ``total`` count, ``limit``,
            and ``offset`` fields.
        """
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        all_profiles = db.list_trust_profiles()
        total = len(all_profiles)
        items = all_profiles[offset : offset + limit]
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/trust/profiles/{profile_id}")
    async def get_trust_profile_by_id(profile_id: int) -> JSONResponse:
        """Return a single trust profile by its integer primary key.

        Args:
            profile_id: Integer primary key of the trust profile row.

        Returns:
            200 with the trust profile dict.
            404 when no profile matches the given id.
        """
        db = Database(Path(effective_db_path))
        profile = db.get_trust_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trust profile '{profile_id}' not found",
            )
        return JSONResponse(profile)

    @app.put("/api/v1/trust/profiles/{profile_id}")
    async def override_trust_profile(
        profile_id: int,
        body: TrustOverrideRequest,
    ) -> JSONResponse:
        """Manually override the trust score for a profile.

        Validates that ``trust_score`` is in ``[0.0, 1.0]``, updates the DB
        row, re-derives the ``auto_merge_threshold``, and logs a
        ``trust_adjustments`` entry with reason ``"manual_override"``.

        Args:
            profile_id: Integer primary key of the trust profile to update.
            body:       ``TrustOverrideRequest`` with the new score and reason.

        Returns:
            200 with the updated trust profile dict.
            404 when no profile matches the given id.
            422 when ``trust_score`` is outside ``[0.0, 1.0]``.
        """
        if not (0.0 <= body.trust_score <= 1.0):
            raise HTTPException(
                status_code=422,
                detail=f"trust_score must be in [0.0, 1.0], got {body.trust_score!r}",
            )
        db = Database(Path(effective_db_path))
        profile = db.get_trust_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trust profile '{profile_id}' not found",
            )

        from orchestration_engine.trust import TrustCalibrator  # noqa: PLC0415

        old_score = float(profile["trust_score"])
        new_score = body.trust_score
        delta = new_score - old_score

        # Re-derive auto_merge_threshold
        calibrator = TrustCalibrator(
            repo=profile["repo"],
            template_id=profile["template_id"],
            task_type=profile["task_type"],
        )
        successful_merges = int(profile.get("successful_merges", 0))
        new_threshold = calibrator.compute_threshold(new_score, successful_merges)

        now_iso = datetime.now(timezone.utc).isoformat()
        updated: Dict[str, Any] = {
            "repo": profile["repo"],
            "template_id": profile["template_id"],
            "task_type": profile["task_type"],
            "auto_merge_threshold": new_threshold,
            "human_review_threshold": float(profile["human_review_threshold"]),
            "trust_score": new_score,
            "total_runs": int(profile["total_runs"]),
            "successful_merges": successful_merges,
            "regressions": int(profile["regressions"]),
            "reverted_prs": int(profile["reverted_prs"]),
            "last_run_at": profile.get("last_run_at"),
            "created_at": profile["created_at"],
            "updated_at": now_iso,
        }
        db.upsert_trust_profile(updated)

        # Build audit note (include reviewer if supplied)
        audit_reason = "manual_override"
        if body.reviewed_by:
            audit_reason = f"manual_override:{body.reviewed_by}"

        db.insert_trust_adjustment(
            {
                "profile_id": profile_id,
                "delta": delta,
                "reason": audit_reason,
                "run_id": None,
                "score_before": old_score,
                "score_after": new_score,
                "created_at": now_iso,
            }
        )

        refreshed = db.get_trust_profile_by_id(profile_id)
        return JSONResponse(refreshed)

    @app.get("/api/v1/trust/adjustments")
    async def list_trust_adjustments(
        profile_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        """Return the trust adjustment audit log for a profile.

        Query parameters:
            profile_id: **(required)** Integer primary key of the trust profile.
            limit:      Maximum number of results (default 100, max 500).
            offset:     Number of rows to skip for pagination (default 0).

        Returns:
            200 with ``{"items": [...], "total": int, "limit": int, "offset": int}``.
            404 when no profile matches ``profile_id``.
        """
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        profile = db.get_trust_profile_by_id(profile_id)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trust profile '{profile_id}' not found",
            )
        items = db.list_trust_adjustments(profile_id=profile_id, limit=limit, offset=offset)
        return JSONResponse(
            {
                "items": items,
                "total": len(items),
                "limit": limit,
                "offset": offset,
            }
        )
