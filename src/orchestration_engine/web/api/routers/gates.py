"""Merge-gate API route group for the REST API (Issue #942, sub-issue 952c).

Holds the merge-gate routes (#743), extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``GET  /api/v1/gates``                  (list, optional status filter)
* ``GET  /api/v1/gates/{run_id}``         (detail)
* ``POST /api/v1/gates/{run_id}/approve`` (approve)
* ``POST /api/v1/gates/{run_id}/reject``  (reject)

``GateApproveRequest`` / ``GateRejectRequest`` (used only by these routes) move
with the routes into ``register_gate_routes``; they are defined inside the
register function with a lazy ``from pydantic import BaseModel`` so importing this
module does NOT eagerly pull ``pydantic`` (preserving the optional ``[api]`` extra
contract), exactly as the inline definitions did inside ``create_api_app``.

The lazy in-closure imports of ``GitContext`` / ``GitError`` are preserved
(spelled as the absolute ``from orchestration_engine.git_integration import ...``
— the same module the inline ``from ...git_integration import ...`` resolved to
from ``_app``).  These routes read/write gate state via ``GitContext`` and do not
touch the per-call ``effective_db_path`` Database, so this register function takes
no DB argument.
"""

from typing import Any, Optional


def register_gate_routes(  # noqa: C901
    app: Any,
    *,
    HTTPException: Any,  # noqa: N803 (matches the framework class name captured by the closure)
) -> None:
    """Register the merge-gate API route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, params, status-transition guards, force-override
    behaviour, response shapes and error handling.

    ``GateApproveRequest`` / ``GateRejectRequest`` are defined here (not at module
    scope) with a lazy ``from pydantic import BaseModel`` so importing this module
    does NOT eagerly pull ``pydantic`` — preserving the optional ``[api]`` extra
    contract, exactly as the inline definitions did inside ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class GateApproveRequest(BaseModel):
        message: Optional[str] = None
        force: bool = False

    class GateRejectRequest(BaseModel):
        reason: Optional[str] = None

    @app.get("/api/v1/gates")
    async def list_gates(
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """List all merge gates with optional status filter and pagination."""
        from orchestration_engine.git_integration import GitContext  # noqa: PLC0415

        all_gates = GitContext.list_gates()
        if status:
            all_gates = [g for g in all_gates if g.get("status") == status]
        total = len(all_gates)
        items = all_gates[offset : offset + limit]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/gates/{run_id}")
    async def get_gate(run_id: str):
        """Get a single gate by run ID."""
        from orchestration_engine.git_integration import GitContext  # noqa: PLC0415

        gate = GitContext.load_gate(run_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"No gate found for run ID '{run_id}'")
        return gate

    @app.post("/api/v1/gates/{run_id}/approve")
    async def approve_gate(run_id: str, req: GateApproveRequest = GateApproveRequest()):
        """Approve a merge gate."""
        from orchestration_engine.git_integration import GitContext, GitError  # noqa: PLC0415

        gate = GitContext.load_gate(run_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"No gate found for run ID '{run_id}'")

        current_status = gate.get("status")
        if current_status != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Gate is in status '{current_status}', can only approve 'awaiting_approval' gates",  # noqa: E501
            )

        scoring_status = gate.get("scoring_status")
        if scoring_status == "failed" and not req.force:
            raise HTTPException(
                status_code=409,
                detail="Score gate FAILED \u2014 approval blocked. Use force=true to override.",
            )

        try:
            updated = GitContext.update_gate_status(
                run_id, "approved", message=req.message or "Approved via API"
            )
        except GitError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        return updated

    @app.post("/api/v1/gates/{run_id}/reject")
    async def reject_gate(run_id: str, req: GateRejectRequest = GateRejectRequest()):
        """Reject a merge gate."""
        from orchestration_engine.git_integration import GitContext, GitError  # noqa: PLC0415

        gate = GitContext.load_gate(run_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"No gate found for run ID '{run_id}'")

        current_status = gate.get("status")
        if current_status != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Gate is in status '{current_status}', can only reject 'awaiting_approval' gates",  # noqa: E501
            )

        try:
            updated = GitContext.update_gate_status(
                run_id, "rejected", message=req.reason or "Rejected via API"
            )
        except GitError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        return updated
