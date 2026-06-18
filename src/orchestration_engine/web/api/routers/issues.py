"""Direct issue-launch route group for the REST API (Issue #942, sub-issue 952d).

Holds the single direct issue-launch REST route, extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``POST /api/v1/issues/launch``   (programmatic issue pipeline launch, Issue #632)

``IssueLaunchRequest`` (the request body, used only by this route) moves with the
route into ``register_issue_routes``; it is defined inside the register function
with a lazy ``from pydantic import BaseModel`` so importing this module does NOT
eagerly pull ``pydantic`` (preserving the optional ``[api]`` extra contract),
exactly as the inline definition did inside ``create_api_app``.

The ``os`` module the closure references is imported at module scope here (the
same standard-library module ``_app`` imported), and ``generate_pipeline_input``
is imported absolutely from :mod:`orchestration_engine.issue_automation` — the
same symbol ``_app`` imported at the top of the factory (no test patches it, so a
module-level import is behaviour-identical). ``JSONResponse``, ``HTTPException``
and the shared ``_resolve_template`` helper are received as keyword arguments —
the same objects the inline closure used.
"""

import os
from typing import Any

from orchestration_engine.issue_automation import generate_pipeline_input


def register_issue_routes(
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    _resolve_template: Any,
) -> None:
    """Register the direct issue-launch route group onto *app*.

    Mirrors the inline definition that previously lived in ``create_api_app``
    — identical path, verb, status code, template resolution, branch-name
    derivation, response shape and error handling.

    ``IssueLaunchRequest`` is defined here (not at module scope) with a lazy
    ``from pydantic import BaseModel`` so importing this module does NOT eagerly
    pull ``pydantic`` — preserving the optional ``[api]`` extra contract, exactly
    as the inline definition did inside ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class IssueLaunchRequest(BaseModel):
        """Request body for POST /api/v1/issues/launch."""

        issue_number: int
        repo: str
        title: str = ""
        body: str = ""

    @app.post("/api/v1/issues/launch", status_code=201)
    async def launch_issue_pipeline(req: IssueLaunchRequest) -> JSONResponse:
        """Launch a pipeline for a GitHub issue via direct REST call.

        This endpoint provides programmatic issue pipeline launch without
        requiring a GitHub webhook. It respects the ``ORCH_DEFAULT_TEMPLATE``
        environment variable for template selection.

        Request body (JSON):
            issue_number (int): GitHub issue number.
            repo (str): Repository full name (e.g. ``owner/repo``).
            title (str): Issue title (optional).
            body (str): Issue body (optional).

        Returns:
            201 with run ID and branch name on success.
            400 when the configured default template cannot be resolved.
        """
        # Resolve template — read env var at call time (not import time) so
        # that monkeypatch.setenv in tests takes effect.
        _default_tpl = os.environ.get("ORCH_DEFAULT_TEMPLATE") or "coding-pipeline-standard"
        try:
            _resolve_template(_default_tpl)
        except HTTPException:
            raise HTTPException(
                status_code=400,
                detail=f"Default template '{_default_tpl}' not found. "
                f"Set ORCH_DEFAULT_TEMPLATE to an available template name.",
            )

        pipeline_input = generate_pipeline_input(
            issue_number=req.issue_number,
            title=req.title,
            body=req.body,
            repo=req.repo,
        )
        branch_name = pipeline_input["branch_name"]

        return JSONResponse(
            {
                "status": "accepted",
                "branch_name": branch_name,
                "template_id": _default_tpl,
            },
            status_code=201,
        )
