"""Health route group for the REST API (Issue #942, sub-issue 952d).

Holds the two server/health-probe routes, extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``GET /api/v1/health``           (API server health + version)
* ``GET /api/v1/health/webhook``   (regression CI webhook trigger health)

The ``webhook_health`` closure opens its own :class:`~orchestration_engine.db.Database`
on ``effective_db_path`` (no shared connection) and shells out to ``gh`` for a
best-effort GitHub-side hook check — both kept identical to the inline version.
The ``subprocess`` / ``json`` / ``os`` modules the closures reference are
imported at module scope here (the same standard-library modules ``_app``
imported); ``__version__`` and the ``Database`` class are received as keyword
arguments — the same objects the inline closures used.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional


def register_health_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    Database: Any,  # noqa: N803
    __version__: str,
    effective_db_path: str,
) -> None:
    """Register the health route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, query params, GitHub-probe behaviour, response
    shapes and error handling.
    """

    @app.get("/api/v1/health")
    async def health() -> JSONResponse:
        """Return API server health status."""
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/api/v1/health/webhook")
    async def webhook_health() -> JSONResponse:  # noqa: C901
        """Return health status for the regression CI webhook trigger.

        Checks two things:

        1. **DB presence** — whether the regression trigger row exists in the
           ``triggers`` table.
        2. **GitHub presence** — (best-effort) whether the GitHub-side webhook
           still exists by calling ``gh api repos/<repo>/hooks`` and filtering
           by payload URL.  If the ``gh`` CLI is unavailable or the call fails,
           only the DB information is reported.

        The trigger ID is read from the ``REGRESSION_TRIGGER_ID`` environment
        variable (default ``"regression-ci-trigger"``).  As a fallback, the
        endpoint scans for any trigger whose ``template_id`` matches a known
        regression template ID (``regression-pipeline-v1``).

        Returns:
            JSON object with the following fields:

            ``trigger_registered`` (bool)
                ``True`` when the trigger row exists in the DB.
            ``trigger_id`` (str | null)
                The trigger ID that was checked (or ``null`` when not found
                via fallback scan).
            ``github_webhook_id`` (int | null)
                The GitHub webhook ID, or ``null`` when the GitHub check was
                skipped or the hook was not found.
            ``github_webhook_active`` (bool | null)
                Whether the GitHub webhook is marked active, or ``null``
                when the GitHub check was skipped.
            ``status`` (str)
                ``"ok"`` — trigger registered and GitHub hook active.
                ``"degraded"`` — trigger registered but GitHub check skipped or
                    hook not found.
                ``"error"`` — trigger is not registered in the DB.
        """
        _KNOWN_REGRESSION_TEMPLATE_IDS = {  # noqa: N806
            "regression-pipeline-v1",
            "regression-fix-pipeline-v1",
        }
        _REGRESSION_WEBHOOK_PATH_SUFFIX = "/api/v1/webhooks/"  # noqa: N806

        # 1. Determine which trigger ID to look up
        trigger_id = os.environ.get("REGRESSION_TRIGGER_ID", "regression-ci-trigger")
        db = Database(Path(effective_db_path))
        trigger_row = db.get_trigger(trigger_id)

        # Fallback: scan for any trigger whose template_id matches a known regression template
        if trigger_row is None:
            all_triggers = db.list_triggers(limit=200)
            for row in all_triggers:
                if row.get("template_id") in _KNOWN_REGRESSION_TEMPLATE_IDS:
                    trigger_row = row
                    trigger_id = row["id"]
                    break

        trigger_registered = trigger_row is not None
        found_trigger_id: Optional[str] = trigger_id if trigger_registered else None

        # 2. Attempt GitHub-side check (best-effort, graceful failure)
        github_webhook_id: Optional[int] = None
        github_webhook_active: Optional[bool] = None

        if trigger_registered:
            try:
                gh_result = subprocess.run(
                    ["gh", "api", "repos/ToscanAI/orchestration-engine/hooks"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if gh_result.returncode == 0 and gh_result.stdout.strip():
                    hooks = json.loads(gh_result.stdout)
                    if isinstance(hooks, list):
                        for hook in hooks:
                            hook_url = (hook.get("config") or {}).get("url", "")
                            if (
                                _REGRESSION_WEBHOOK_PATH_SUFFIX in hook_url
                                and trigger_id in hook_url
                            ):
                                github_webhook_id = hook.get("id")
                                github_webhook_active = bool(hook.get("active"))
                                break
            except (
                FileNotFoundError,
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
                Exception,  # noqa: BLE001
            ):
                # gh unavailable or any other failure — skip GitHub check gracefully
                pass

        # 3. Determine overall status
        if not trigger_registered:
            overall_status = "error"
        elif github_webhook_id is not None and github_webhook_active:
            overall_status = "ok"
        else:
            # Trigger registered but GitHub check skipped or hook not found
            overall_status = "degraded"

        return JSONResponse(
            {
                "trigger_registered": trigger_registered,
                "trigger_id": found_trigger_id,
                "github_webhook_id": github_webhook_id,
                "github_webhook_active": github_webhook_active,
                "status": overall_status,
            }
        )
