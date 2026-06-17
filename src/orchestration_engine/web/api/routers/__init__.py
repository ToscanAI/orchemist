"""Per-group route modules for the REST API (Issue #942, sub-issue 952b).

Each module here exposes a ``register_<group>_routes(app, ...)`` function whose
body contains the SAME ``@app.<verb>`` route closures that previously lived
inline inside :func:`orchestration_engine.web.api._app.create_api_app`, moved
*verbatim*.  ``create_api_app`` now calls each ``register_*`` function at the
point the inline routes used to occupy, passing every object the closures
referenced from the factory scope (helpers, framework classes, the per-call
``effective_db_path`` value, and the shared ``_SSE_LIMITER`` instance) as
explicit keyword arguments.

This is the **register-function pattern**: a deliberate, §0-safety-invariant
driven refinement of the plan's ``APIRouter`` suggestion (decomp plan §1c).
Converting the inner closures to ``APIRouter`` + ``Depends`` would change
dependency resolution (closure capture → request-time injection) and risks
silent behavioural drift; the register-function pattern keeps every route an
identical closure over the identical objects, so the transformation is provably
behaviour-neutral.  ``APIRouter`` polish is explicitly deferred.
"""
