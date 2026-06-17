"""Phases route group for the REST API (Issue #942, sub-issue 952b).

Holds the ``GET /api/v1/phases`` route, extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`).  The closure references the
following objects from the factory scope, all received as keyword arguments:

* ``JSONResponse`` — ``fastapi.responses.JSONResponse``
* ``HTTPException`` — ``fastapi.HTTPException``
* ``TemplateNotFoundError`` — ``orchestration_engine.templates.TemplateNotFoundError``
* ``_make_engine`` — the factory-local ``TemplateEngine`` builder closure (closes
  over the factory's ``user_templates_dir`` override)
"""

from pathlib import Path
from typing import Any


def register_phase_routes(
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    TemplateNotFoundError: Any,  # noqa: N803
    _make_engine: Any,
) -> None:
    """Register the phases route group onto *app*.

    Mirrors the inline definition that previously lived in
    ``create_api_app`` — same path, verb, query params, validation, response
    shape and error handling.
    """

    @app.get("/api/v1/phases")
    async def list_phases_api(
        pipeline: str = "coding-pipeline-standard",
    ) -> JSONResponse:
        """Return the ordered phase list for a pipeline template.

        Used by the frontend Phase Rail (`/runs/<id>`) and Skills Pack Mode
        (`/skills`) to hydrate phase metadata at boot — eliminates the
        hardcoded `PHASES` / `PHASE_CARDS` arrays that drifted from the
        canonical YAML (see DUPLICATES_REFRESHED.md NEW Group A).

        Query params:
            pipeline (str): Pipeline template id. Default
                ``coding-pipeline-standard``.

        Response body::

            {
                "pipeline": "coding-pipeline-standard",
                "version": "2.1.0",
                "phases": [
                    {
                        "id": "existing_symbols_inventory",
                        "name": "Existing-symbols inventory (sub-check 7d pre-flight)",
                        "model_tier": "sonnet",
                        "task_type": null,
                        "depends_on": [],
                        "order": 0
                    },
                    ...
                ]
            }

        ``model_tier`` ∈ {``"haiku"``, ``"sonnet"``, ``"opus"``} per
        ``KNOWN_MODEL_TIERS`` in ``templates.py``; defaults to ``"sonnet"``
        so engine phases (e.g. ``acceptance_run``, ``test``) still carry
        a value. UI consumers should classify "is this an LLM phase?"
        from ``task_type``, not from ``model_tier``. ``order`` is the
        0-based position in ``template.phases`` (matches
        YAML declaration order).

        Raises:
            404 — pipeline template not found.
        """
        # Pre-validate the pipeline id to distinguish path-traversal
        # attempts (404 — unknown registered template) from corrupt YAML
        # discovered during load (which should surface as 500, not 404).
        # The TemplateEngine raises ValueError for both; we only want to
        # swallow the path-traversal case here.
        if "/" in pipeline or "\\" in pipeline or pipeline.startswith("."):
            raise HTTPException(
                status_code=404,
                detail=f"Pipeline template '{pipeline}' not found",
            )
        engine = _make_engine()
        template = None
        try:
            template_path = engine.resolve_template(pipeline)
            template = engine.load_template(template_path)
        except (TemplateNotFoundError, FileNotFoundError):
            for entry in engine.list_templates():
                if entry["id"] == pipeline:
                    try:
                        template = engine.load_template(Path(entry["path"]))
                    except Exception:  # noqa: BLE001
                        pass
                    break
        if template is None:
            raise HTTPException(
                status_code=404,
                detail=f"Pipeline template '{pipeline}' not found",
            )
        phases_data = [
            {
                "id": p.id,
                "name": p.name,
                "model_tier": p.model_tier,
                "task_type": p.task_type,
                "depends_on": p.depends_on,
                "order": idx,
            }
            for idx, p in enumerate(template.phases)
        ]
        return JSONResponse(
            {
                "pipeline": template.id,
                "version": template.version,
                "phases": phases_data,
            }
        )
