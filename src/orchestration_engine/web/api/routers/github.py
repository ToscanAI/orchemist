"""GitHub Issues webhook route group for the REST API (Issue #942, sub-issue 952d).

Holds the two GitHub ``issues`` webhook routes, extracted *verbatim* from
``create_api_app`` via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``POST /api/v1/github/issues``                 (orchemist trigger-label automation, Issue #5.1.3)
* ``POST /api/v1/github/issues/pipeline-ready``  (pipeline-ready label trigger, Issue #511)

The ``json`` / ``os`` modules and ``generate_pipeline_input`` the closures
reference are imported at module scope here — the same standard-library modules
``_app`` imported and the same ``from orchestration_engine.issue_automation import
generate_pipeline_input`` symbol it imported at the top of the factory module (no
test patches that symbol, so a module-level import is behaviour-identical). Every
*other* lazy in-closure import (``IssueClassifier``, ``TemplateSelector``,
``InputExtractor``, ``IssueAutomation``, ``post_github_comment``,
``remove_github_label``, ``NotificationDispatcher``, ``get_global_config``) is
preserved exactly where it was, spelled absolutely against the identical module,
so ``patch("orchestration_engine.<mod>.<X>")`` still intercepts at call time.
``JSONResponse``, ``HTTPException``, ``Request``, the ``Database`` /
``TemplateEngine`` classes, the per-call ``effective_db_path`` value, the shared
``_verify_github_signature`` / ``_resolve_template`` /
``_launch_pipeline_from_trigger`` helpers, and the module ``logger`` are received
as keyword arguments — the same objects the inline closures used.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from orchestration_engine.issue_automation import generate_pipeline_input


def register_github_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Request: Any,  # noqa: N803
    Database: Any,  # noqa: N803
    TemplateEngine: Any,  # noqa: N803
    effective_db_path: str,
    _verify_github_signature: Any,
    _resolve_template: Any,
    _launch_pipeline_from_trigger: Any,
    logger: Any,
) -> None:
    """Register the GitHub Issues webhook route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, status codes, event-type/action/label filtering,
    optional signature verification, deduplication, automation construction,
    daemon launch, comment posting, response shapes and error handling.
    """

    @app.post("/api/v1/github/issues", status_code=202)
    async def handle_github_issues(request: Request) -> JSONResponse:  # noqa: C901
        """Receive GitHub ``issues`` webhook events and launch pipelines automatically.

        Triggered when a GitHub issue is **opened** or **labeled** with the
        ``orchemist`` trigger label (configurable via the ``ISSUE_TRIGGER_LABEL``
        environment variable, default ``"orchemist"``).

        Flow:

        1. Validate that the ``X-GitHub-Event`` header is ``"issues"``.
        2. Filter for ``action == "opened"`` or ``action == "labeled"``.
        3. Check that the orchemist trigger label is present/applied.
        4. **Deduplication** — call
           :meth:`~orchestration_engine.db.Database.get_active_issue_run`;
           skip if an active pipeline run already exists for this issue.
        5. Classify, select template, extract inputs, and launch via
           :class:`~orchestration_engine.issue_automation.IssueAutomation`.
        6. Post a GitHub comment summarising the launched run via
           :func:`~orchestration_engine.issue_automation.post_github_comment`.

        Request headers:
            X-GitHub-Event (str): Must be ``"issues"`` (other values are ignored).

        Returns:
            - **200** when the event was ignored (wrong event type, wrong
              action, missing label, or duplicate run already active).
            - **202** when the pipeline was accepted and launched (or attempted).
            - **400** when the request body is not valid JSON or required
              payload fields are missing.
        """
        from orchestration_engine.issue_automation import (  # noqa: PLC0415
            InputExtractor,
            IssueAutomation,
            IssueClassifier,
            TemplateSelector,
            post_github_comment,
        )
        from orchestration_engine.notifications import NotificationDispatcher  # noqa: PLC0415

        # 1. Validate event type header
        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type != "issues":
            return JSONResponse(
                {"status": "ignored", "reason": "not_issues_event"},
                status_code=200,
            )

        # 1b. Read body bytes once — reused for signature verification and JSON parsing
        _body_bytes = await request.body()

        # 1c. GitHub App webhook signature verification (opt-in)
        from orchestration_engine.config import get_global_config  # noqa: PLC0415

        cfg = get_global_config()
        if cfg.github_app and cfg.github_app.webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(cfg.github_app.webhook_secret, _body_bytes, sig_header):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing X-Hub-Signature-256 header",
                )
        else:
            logger.warning(
                "GitHub App webhook_secret is not configured — "
                "POST /api/v1/github/issues is accepting unauthenticated requests."
            )

        # 2. Parse JSON body (reuse already-read bytes)
        try:
            payload: Dict[str, Any] = json.loads(_body_bytes) if _body_bytes else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in request body: {exc}",
            )

        action = payload.get("action", "")

        # 3. Filter for relevant actions
        if action not in ("opened", "labeled"):
            return JSONResponse(
                {"status": "ignored", "reason": f"action_{action}_not_relevant"},
                status_code=200,
            )

        # 4. Extract issue and repo from payload
        issue = payload.get("issue", {}) or {}
        issue_number = issue.get("number")
        if not issue_number:
            raise HTTPException(
                status_code=400,
                detail="Missing issue.number in webhook payload",
            )

        repo_data = payload.get("repository", {}) or {}
        repo = repo_data.get("full_name", "")
        if not repo:
            raise HTTPException(
                status_code=400,
                detail="Missing repository.full_name in webhook payload",
            )

        # 5. Check for orchemist trigger label
        trigger_label = os.environ.get("ISSUE_TRIGGER_LABEL", "orchemist")

        if action == "labeled":
            # For "labeled" action, the label that was just applied is in payload["label"]
            applied_label = (payload.get("label") or {}).get("name", "")
            if applied_label != trigger_label:
                return JSONResponse(
                    {"status": "ignored", "reason": "label_not_trigger"},
                    status_code=200,
                )
        elif action == "opened":
            # For "opened" action, check the issue already carries the trigger label
            issue_labels = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]
            if trigger_label not in issue_labels:
                return JSONResponse(
                    {"status": "ignored", "reason": "trigger_label_absent"},
                    status_code=200,
                )

        # 6. Deduplication — skip if there is already an active pipeline for this issue
        db = Database(Path(effective_db_path))
        active_run = db.get_active_issue_run(issue_number, repo)
        if active_run is not None:
            return JSONResponse(
                {
                    "status": "skipped",
                    "reason": "active_run_exists",
                    "run_id": active_run.get("run_id"),
                },
                status_code=200,
            )

        # 7. Build automation and process
        classifier = IssueClassifier()  # stub mode; replace executor via subclass/config
        selector = TemplateSelector()
        extractor = InputExtractor()
        try:
            confidence_threshold = float(
                os.environ.get("ISSUE_CLASSIFY_CONFIDENCE_THRESHOLD", "0.70")
            )
        except (TypeError, ValueError):
            confidence_threshold = 0.70
        dispatcher = NotificationDispatcher.from_env()
        automation = IssueAutomation(
            classifier=classifier,
            selector=selector,
            extractor=extractor,
            confidence_threshold=confidence_threshold,
            notification_dispatcher=dispatcher,
        )

        title = issue.get("title", "") or ""
        body_text = issue.get("body", "") or ""
        issue_label_names = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]

        engine_instance = TemplateEngine()
        gw_url = os.environ.get("OPENCLAW_GATEWAY_URL")

        result = automation.process(
            issue_number=issue_number,
            repo=repo,
            title=title,
            body=body_text,
            labels=issue_label_names,
            db=db,
            launcher=_launch_pipeline_from_trigger,
            template_resolver=_resolve_template,
            template_engine=engine_instance,
            mode="standalone",
            gateway_url=gw_url,
        )

        # 8. Post GitHub comment (best-effort — errors are logged, not raised)
        comment_body = result.get("comment_body", "")
        comment_url: Optional[str] = None
        if comment_body:
            comment_url = post_github_comment(
                repo=repo,
                issue_number=issue_number,
                body=comment_body,
            )
        result["comment_url"] = comment_url

        return JSONResponse(result, status_code=202)

    @app.post("/api/v1/github/issues/pipeline-ready", status_code=202)
    async def handle_github_issues_pipeline_ready(request: Request) -> JSONResponse:  # noqa: C901
        """Receive GitHub ``issues`` webhook events for the ``pipeline-ready`` label.

        Triggered when a GitHub issue is labeled with ``pipeline-ready``.

        Flow:

        1. Validate that ``X-GitHub-Event`` is ``"issues"``.
        2. Filter for ``action == "labeled"`` and label == ``"pipeline-ready"``.
        3. Dedup — skip if an active pipeline run already exists for this issue.
        4. Launch ``coding-pipeline-v1`` via daemon infrastructure.
        5. Remove the ``pipeline-ready`` label (best-effort).
        6. Post a comment with the run ID.
        7. Return 202 with ``run_id`` and ``branch_name``.

        Returns:
            - **200** when the event was ignored (wrong type, action, or label,
              or a duplicate run already exists).
            - **202** when the pipeline was launched.
            - **400** when the request body is invalid.
        """
        from orchestration_engine.issue_automation import (  # noqa: PLC0415
            post_github_comment,
            remove_github_label,
        )

        # 1. Validate event type header
        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type != "issues":
            return JSONResponse(
                {"status": "ignored", "reason": "not_issues_event"},
                status_code=200,
            )

        # 2. Read body
        _body_bytes = await request.body()

        # Signature verification (opt-in, same as handle_github_issues)
        from orchestration_engine.config import get_global_config  # noqa: PLC0415

        _cfg = get_global_config()
        if _cfg.github_app and _cfg.github_app.webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(
                _cfg.github_app.webhook_secret, _body_bytes, sig_header
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing X-Hub-Signature-256 header",
                )

        # 3. Parse JSON
        try:
            payload: Dict[str, Any] = json.loads(_body_bytes) if _body_bytes else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in request body: {exc}",
            )

        action = payload.get("action", "")

        # 4. Only process labeled events
        if action != "labeled":
            return JSONResponse(
                {"status": "ignored", "reason": f"action_{action}_not_relevant"},
                status_code=200,
            )

        # 5. Check label is pipeline-ready
        applied_label = (payload.get("label") or {}).get("name", "")
        if applied_label != "pipeline-ready":
            return JSONResponse(
                {"status": "ignored", "reason": "label_not_pipeline_ready"},
                status_code=200,
            )

        # 6. Extract issue and repo
        issue = payload.get("issue", {}) or {}
        issue_number = issue.get("number")
        if not issue_number:
            raise HTTPException(
                status_code=400,
                detail="Missing issue.number in webhook payload",
            )

        repo_data = payload.get("repository", {}) or {}
        repo = repo_data.get("full_name", "")
        if not repo:
            raise HTTPException(
                status_code=400,
                detail="Missing repository.full_name in webhook payload",
            )

        # 7. Dedup — skip if active run exists for this issue
        db = Database(Path(effective_db_path))
        active_run = db.get_active_issue_run(issue_number, repo)
        if active_run is not None:
            return JSONResponse(
                {
                    "status": "skipped",
                    "reason": "active_run_exists",
                    "run_id": active_run.get("run_id"),
                },
                status_code=200,
            )

        # 8. Build pipeline input
        title = issue.get("title", "") or ""
        body_text = issue.get("body", "") or ""
        pipeline_input = generate_pipeline_input(
            issue_number=issue_number,
            title=title,
            body=body_text,
            repo=repo,
        )
        branch_name = pipeline_input["branch_name"]

        # 9. Resolve and load template
        _default_tpl = os.environ.get("ORCH_DEFAULT_TEMPLATE") or "coding-pipeline-standard"
        try:
            template_file = _resolve_template(_default_tpl)
        except HTTPException:
            raise HTTPException(
                status_code=400,
                detail=f"Default template '{_default_tpl}' not found. "
                f"Set ORCH_DEFAULT_TEMPLATE to an available template name.",
            )

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        # 10. Launch pipeline
        gw_url = os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=pipeline_input,
            mode="standalone",
            gateway_url=gw_url,
            db=db,
        )
        run_id = run_dict["run_id"]

        # 11. Remove pipeline-ready label (best-effort)
        remove_github_label(repo, issue_number, "pipeline-ready")

        # 12. Post comment with run ID (best-effort)
        comment_body = (
            f"🤖 **Orchemist** detected `pipeline-ready` label and launched the coding pipeline.\n\n"  # noqa: E501
            f"**Branch:** `{branch_name}`\n"
            f"**Run ID:** `{run_id}`\n\n"
            f"Progress can be tracked via `orch status {run_id}`."
        )
        post_github_comment(repo=repo, issue_number=issue_number, body=comment_body)

        return JSONResponse(
            {"status": "accepted", "run_id": run_id, "branch_name": branch_name},
            status_code=202,
        )
