"""Templates route group for the REST API (Issue #942, sub-issue 952b).

Holds the template CRUD routes, extracted *verbatim* from ``create_api_app``
via the register-function pattern (see
:mod:`orchestration_engine.web.api.routers`):

* ``GET    /api/v1/templates``
* ``GET    /api/v1/templates/{name}``
* ``POST   /api/v1/templates/validate``
* ``POST   /api/v1/templates``
* ``PUT    /api/v1/templates/{name}``
* ``DELETE /api/v1/templates/{name}``
* ``POST   /api/v1/templates/{name}/duplicate``

The Pydantic request/response models used *only* by these routes
(``TemplateCreateRequest``, ``TemplateValidateRequest``, ``TemplateWriteResponse``)
move with the routes into ``register_template_routes``.  They are defined inside
the register function (not at module scope) with a lazy ``from pydantic import
BaseModel`` so importing this module does NOT eagerly pull ``pydantic`` — this
preserves the optional ``[api]`` extra contract, matching the original inline
definitions that lived inside ``create_api_app`` after its lazy pydantic import.
``LaunchRequest`` and ``RunResponse`` live with the runs group / stay in
``_app.py`` respectively.

The three template-exclusive helper closures (``_template_source``,
``_writable_template_path``, ``_load_yaml_via_tempfile``) move into
``register_template_routes`` as nested functions — verbatim — so they keep
closing over the same ``HTTPException`` the routes use.

Free variables received from the factory scope as keyword arguments:
``JSONResponse``, ``HTTPException``, ``Response`` (fastapi),
``TemplateEngine``, ``TemplateNotFoundError`` (orchestration_engine.templates),
``_make_engine`` (factory-local TemplateEngine builder) and ``_resolve_template``
(factory-local resolver shared with the runs / webhook / issue routes, so it
stays in ``_app.py``).
"""

import re
import tempfile
from pathlib import Path
from typing import Any, List, Literal, Optional

import yaml


def register_template_routes(  # noqa: C901
    app: Any,
    *,
    JSONResponse: Any,  # noqa: N803 (matches the framework class name captured by the closure)
    HTTPException: Any,  # noqa: N803
    Response: Any,  # noqa: N803
    TemplateEngine: Any,  # noqa: N803
    TemplateNotFoundError: Any,  # noqa: N803
    _make_engine: Any,
    _resolve_template: Any,
) -> None:
    """Register the template CRUD route group onto *app*.

    Mirrors the inline definitions that previously lived in ``create_api_app``
    — identical paths, verbs, status codes, path/body params, validation,
    response shapes and error handling.

    The Pydantic models are defined here (not at module scope) so importing
    this module does NOT eagerly pull ``pydantic`` — preserving the optional
    ``[api]`` extra contract: ``pydantic`` is imported lazily, only when the
    factory wires the routes, exactly as the inline definitions did inside
    ``create_api_app``.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    class TemplateCreateRequest(BaseModel):
        """Body for POST /api/v1/templates — create a new template."""

        content: str
        """Raw YAML content of the template.  Must include id, name, and at
        least one phase."""

        source: Literal["user", "project"] = "user"
        """Where to write the template.  ``'user'`` targets ``~/.orch/templates/``
        (default); ``'project'`` targets ``./templates/`` in the server's CWD.
        Bundled templates can never be written via the API."""

        overwrite: bool = False
        """Allow overwriting an existing template with the same ID.  Defaults to
        ``False`` — the request will fail with 409 when the file already exists
        and this is not set."""

    class TemplateValidateRequest(BaseModel):
        """Body for POST /api/v1/templates/validate — dry-run validate only."""

        content: str
        """Raw YAML content to validate without writing to disk."""

        extended: bool = True
        """Also run ``validate_template_extended()`` for deeper linting warnings.
        Defaults to ``True``."""

    class TemplateWriteResponse(BaseModel):
        """Response body returned after a successful create or update."""

        id: str
        name: str
        version: str
        path: str
        source: str
        phases_count: int
        created: bool
        """``True`` when a new file was written; ``False`` when an existing file
        was overwritten (update)."""

    # ------------------------------------------------------------------
    # Helper — classify and resolve writable template paths
    # ------------------------------------------------------------------

    def _template_source(engine: "TemplateEngine", path: Path) -> str:  # type: ignore[name-defined]
        """Return the source label for an absolute template *path*.

        Compares *path* against each directory in ``engine.get_search_paths()``
        and returns the label of the first matching directory.  Falls back to
        ``"unknown"`` when the path does not belong to any search directory.

        Args:
            engine: A :class:`TemplateEngine` instance whose search paths are
                    used for comparison.
            path: Absolute path to the template file.

        Returns:
            One of ``"user"``, ``"project"``, ``"bundled"``, ``"custom"``, or
            ``"unknown"``.
        """
        resolved = path.resolve()
        for directory, label in engine.get_search_paths():
            try:
                resolved.relative_to(directory.resolve())
                return label
            except ValueError:  # noqa: PERF203
                continue
        return "unknown"

    def _writable_template_path(
        engine: "TemplateEngine",  # type: ignore[name-defined]
        template_id: str,
        source: str,
    ) -> Path:
        """Return the filesystem path where a template should be written.

        Only ``"user"`` and ``"project"`` sources are accepted.  Attempting to
        write to a ``"bundled"`` or ``"custom"`` source raises a 403.

        Args:
            engine: :class:`TemplateEngine` instance providing the directory
                    locations.
            template_id: The ``id`` field parsed from the template YAML.  Used
                         as the file stem (e.g. ``my-template`` →
                         ``my-template.yaml``).
            source: One of ``"user"`` or ``"project"``.

        Returns:
            :class:`Path` to ``<directory>/<template_id>.yaml``.  The parent
            directory is created if it does not exist.

        Raises:
            HTTPException(403): When *source* is ``"bundled"`` or ``"custom"``.
            HTTPException(400): When *source* is not a recognised value.
        """
        if source == "user":
            directory = engine._user_dir
        elif source == "project":
            directory = engine._project_dir
        else:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Source '{source}' is read-only via the API.  "
                    "Only 'user' and 'project' templates may be written."
                ),
            )

        # Sanitize template_id to prevent path traversal attacks.
        # IDs must start with an alphanumeric character and contain only
        # alphanumeric characters, hyphens, dots, and underscores.
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", template_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid template id '{template_id}'.  "
                    "IDs must contain only alphanumeric characters, hyphens, dots, and underscores."
                ),
            )

        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / f"{template_id}.yaml"
        # Double-check that the resolved destination is still inside the
        # target directory (defence-in-depth against symlink attacks).
        if dest.resolve().parent != directory.resolve():
            raise HTTPException(
                status_code=422,
                detail="Invalid template id (path traversal detected)",
            )
        return dest

    def _load_yaml_via_tempfile(engine: Any, content: str) -> Any:
        """Write *content* to a temp .yaml, load via ``engine.load_template``, clean up.

        Returns the loaded ``Template``.  Raises ``HTTPException(422)`` with
        the structured ``{"message": "Template load error", "errors": [...]}``
        detail on load failure.

        Callers that need a richer detail shape (e.g. the validate endpoint
        adds ``"warnings": []``) should catch the HTTPException and re-raise
        with the augmented detail — see the validate handler below.

        Extracted from three near-identical inlined blocks (validate /
        create / update endpoints) per #876.
        """
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            try:
                return engine.load_template(tmp_path)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=422,
                    detail={"message": "Template load error", "errors": [str(exc)]},
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    # ---- Templates ---------------------------------------------------

    @app.get("/api/v1/templates")
    async def list_templates_api() -> JSONResponse:
        """List all discoverable pipeline templates.

        Returns a JSON array of template summaries.
        """
        engine = TemplateEngine()
        raw = engine.list_templates()
        result = []
        for t in raw:
            try:
                tpl = engine.load_template(Path(t["path"]))
                phases_summary = [
                    {
                        "id": p.id,
                        "name": p.name,
                        "model_tier": p.model_tier,
                        "thinking_level": p.thinking_level,
                        "depends_on": p.depends_on,
                    }
                    for p in tpl.phases
                ]
                config_schema = tpl.config_schema or {}
                category = tpl.category or (tpl.phases[0].task_type if tpl.phases else "general")
                author = tpl.author or ""
            except Exception:  # noqa: BLE001
                phases_summary = []
                config_schema = {}
                category = "general"
                author = ""

            result.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "version": t["version"],
                    "phases_count": t["phases"],
                    "description": t.get("description", ""),
                    "source": t.get("source", ""),
                    "category": category,
                    "author": author,
                    "phases": phases_summary,
                    "config_schema": config_schema,
                }
            )
        return JSONResponse(result)

    @app.get("/api/v1/templates/{name}")
    async def get_template_api(name: str) -> JSONResponse:
        """Return detail for a single template by name or ID.

        Raises 404 when the template is not found.
        """
        engine = _make_engine()

        # Try by file stem, then by template id field.
        template = None
        template_path: Optional[Path] = None
        try:
            template_path = engine.resolve_template(name)
            template = engine.load_template(template_path)
        except (TemplateNotFoundError, FileNotFoundError):
            # Scan by id
            for entry in engine.list_templates():
                if entry["id"] == name:
                    try:
                        template_path = Path(entry["path"])
                        template = engine.load_template(template_path)
                    except Exception:  # noqa: BLE001
                        pass
                    break

        if template is None:
            raise HTTPException(status_code=404, detail=f"Template '{name}' not found")

        # Determine source label for this template
        source = _template_source(engine, template_path) if template_path else "unknown"

        # Read raw YAML content from disk
        yaml_content = ""
        if template_path and template_path.exists():
            yaml_content = template_path.read_text(encoding="utf-8")

        phases_data = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "model_tier": p.model_tier,
                "thinking_level": p.thinking_level,
                "depends_on": p.depends_on,
                "task_type": p.task_type,
            }
            for p in template.phases
        ]

        return JSONResponse(
            {
                "id": template.id,
                "name": template.name,
                "version": template.version,
                "description": template.description,
                "author": template.author,
                "tags": template.tags,
                "phases": phases_data,
                "example_input": template.example_input,
                "config_schema": template.config_schema or {},
                "source": source,
                "yaml_content": yaml_content,
            }
        )

    @app.post("/api/v1/templates/validate")
    async def validate_template_api(req: TemplateValidateRequest) -> JSONResponse:
        """Validate a template body without writing it to disk.

        Parses the submitted YAML and runs the engine's validation logic,
        returning a structured list of errors and (optionally) warnings.

        Request body (JSON):
            content (str): Raw YAML content to validate.
            extended (bool): Also run extended linting.  Default ``True``.

        Returns:
            200 with ``{"valid": true/false, "errors": [...], "warnings": [...]}``
            422 when the content cannot be parsed as YAML or is structurally
                invalid (missing required top-level fields).
        """
        engine = TemplateEngine()

        # 1. Parse YAML
        try:
            raw = yaml.safe_load(req.content)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail={"message": "YAML parse error", "errors": [str(exc)], "warnings": []},
            )

        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Template must be a YAML mapping",
                    "errors": ["Template content must be a YAML mapping (dict)"],
                    "warnings": [],
                },
            )

        # 2. Load via engine.  ``_load_yaml_via_tempfile`` raises
        # HTTPException(422) with the minimal {"message", "errors"} detail
        # on load failure; the validate endpoint augments it with the
        # ``"warnings": []`` field to keep its response shape consistent.
        try:
            template = _load_yaml_via_tempfile(engine, req.content)
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and "warnings" not in exc.detail:
                exc.detail = {**exc.detail, "warnings": []}
            raise

        # 3. Basic validation
        errors = engine.validate_template(template)

        # 4. Extended validation — returns (errors, warnings) tuple.
        # raw (parsed above) is passed as raw_data per the method signature.
        ext_errors: List[str] = []
        warnings: List[str] = []
        if req.extended:
            try:
                ext_errors, warnings = engine.validate_template_extended(template, raw)
            except Exception as exc:  # noqa: BLE001
                warnings = [f"Extended validation error: {exc}"]

        # Merge structural + extended errors for the final verdict.
        all_errors = errors + ext_errors

        return JSONResponse(
            {
                "valid": len(all_errors) == 0,
                "errors": all_errors,
                "warnings": warnings,
            }
        )

    @app.post("/api/v1/templates", status_code=201)
    async def create_template(req: TemplateCreateRequest) -> JSONResponse:
        """Create a new pipeline template by writing it to the templates directory.

        Parses and validates the submitted YAML content, then writes it to the
        appropriate templates directory (user or project) based on ``source``.
        Bundled templates are never written via the API.

        Request body (JSON):
            content (str): Raw YAML content of the new template.
            source (str): ``"user"`` (default) or ``"project"``.
            overwrite (bool): Allow replacing an existing file.  Default ``False``.

        Returns:
            201 with a :class:`TemplateWriteResponse`-shaped JSON object.
            409 when the template already exists and ``overwrite`` is ``False``.
            422 when the content fails validation.
        """
        engine = _make_engine()

        # 1. Parse YAML
        try:
            raw = yaml.safe_load(req.content)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail={"message": "YAML parse error", "errors": [str(exc)]},
            )

        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=422,
                detail={"message": "Template must be a YAML mapping"},
            )

        # 2. Load and validate
        template = _load_yaml_via_tempfile(engine, req.content)

        errors = engine.validate_template(template)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "Template validation failed", "errors": errors},
            )

        # 2b. Extended validation — returns (errors, warnings) tuple.
        # ``raw`` is the parsed YAML dict captured at the top of this handler.
        ext_errors: List[str] = []
        warnings: List[str] = []
        try:
            ext_errors, warnings = engine.validate_template_extended(template, raw)
        except Exception as exc:  # noqa: BLE001
            ext_errors = [f"Extended validation error: {exc}"]

        if ext_errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Template extended validation failed",
                    "errors": ext_errors,
                },
            )

        # 3. Determine destination path
        dest = _writable_template_path(engine, template.id, req.source)

        # 4. Conflict check
        if dest.exists() and not req.overwrite:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Template '{template.id}' already exists at '{dest}'.  "
                    "Set overwrite=true to replace it."
                ),
            )

        created = not dest.exists()

        # 5. Write
        dest.write_text(req.content, encoding="utf-8")

        # Surface extended-validation warnings in the response (additive,
        # non-breaking — consumers that don't expect the field can ignore it).
        response_data = TemplateWriteResponse(
            id=template.id,
            name=template.name,
            version=template.version,
            path=str(dest.resolve()),
            source=req.source,
            phases_count=len(template.phases),
            created=created,
        ).model_dump()
        response_data["warnings"] = warnings
        return JSONResponse(response_data, status_code=201)

    @app.put("/api/v1/templates/{name}")
    async def update_template(name: str, req: TemplateCreateRequest) -> JSONResponse:
        """Update an existing user-owned pipeline template.

        Resolves the template by *name*, validates the new content, and
        overwrites the file in place.  Only ``"user"`` and ``"project"`` source
        templates may be updated via the API; bundled templates return 403.

        Path parameter:
            name (str): Template name (file stem) or template ID.

        Request body (JSON):
            content (str): New YAML content.
            source: Ignored for PUT (the destination is the existing file's path).
            overwrite: Ignored for PUT (update always overwrites).

        Returns:
            200 with a :class:`TemplateWriteResponse`-shaped JSON object.
            403 when the resolved template is bundled or custom (read-only).
            404 when the template is not found.
            422 when the new content fails validation.
        """
        engine = _make_engine()

        # 1. Resolve the existing template to get its path
        existing_path = _resolve_template(name)

        # 2. Check source — only user templates are mutable via the API.
        # Project and bundled templates are read-only (project templates are
        # typically version-controlled; bundled templates are package assets).
        source = _template_source(engine, existing_path)
        if source != "user":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Template '{name}' is a {source} template and cannot be "
                    "modified via the API.  Only 'user' templates are writable."
                ),
            )

        # 3. Parse YAML — capture raw_data for extended validation below.
        try:
            raw = yaml.safe_load(req.content)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail={"message": "YAML parse error", "errors": [str(exc)]},
            )

        # 3b. Verify YAML id matches URL name parameter (#781)
        if not raw or not isinstance(raw, dict):
            raise HTTPException(400, "Invalid YAML content")
        yaml_id = raw.get("id")
        if not yaml_id:
            raise HTTPException(400, "Template YAML must contain an 'id' field")
        if yaml_id != name:
            raise HTTPException(400, f"YAML id '{yaml_id}' does not match URL name '{name}'")

        # 4. Load and validate new content
        template = _load_yaml_via_tempfile(engine, req.content)

        errors = engine.validate_template(template)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "Template validation failed", "errors": errors},
            )

        # 4b. Extended validation — returns (errors, warnings) tuple.
        # ``raw`` is the parsed YAML dict captured above (not discarded as before).
        # Extended-validation errors (e.g. missing required documentation fields)
        # are blocking and return 422 — mirroring create_template (POST) so the two
        # endpoints share one contract. Advisory warnings are non-blocking and are
        # surfaced on the 200 success response below.
        ext_errors: List[str] = []
        warnings: List[str] = []
        try:
            ext_errors, warnings = engine.validate_template_extended(template, raw)
        except Exception as exc:  # noqa: BLE001
            ext_errors = [f"Extended validation error: {exc}"]

        if ext_errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Template extended validation failed",
                    "errors": ext_errors,
                },
            )

        # 5. Overwrite existing file
        existing_path.write_text(req.content, encoding="utf-8")

        # Surface extended-validation warnings in the response (additive,
        # non-breaking — consumers that don't expect the field can ignore it).
        response_data = TemplateWriteResponse(
            id=template.id,
            name=template.name,
            version=template.version,
            path=str(existing_path.resolve()),
            source=source,
            phases_count=len(template.phases),
            created=False,
        ).model_dump()
        response_data["warnings"] = warnings
        return JSONResponse(response_data)

    @app.delete("/api/v1/templates/{name}", status_code=204)
    async def delete_template_api(name: str) -> Response:
        """Delete a user-owned pipeline template.

        Resolves the template by *name* or ID, checks that it belongs to the
        ``"user"`` source (project, bundled, and custom templates are
        protected), and removes the file from disk.

        Path parameter:
            name (str): Template name (file stem) or template ID.

        Returns:
            204 No Content on success (empty body — HTTP 204 must have no body).
            403 when the template is bundled or custom.
            404 when the template is not found.
        """
        engine = _make_engine()

        # 1. Resolve path
        existing_path = _resolve_template(name)

        # 2. Protect all non-user templates.
        # Only user-owned templates may be deleted via the API.  Project and
        # bundled templates are read-only (bundled = package assets; project =
        # version-controlled repo files).
        source = _template_source(engine, existing_path)
        if source != "user":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Template '{name}' is a {source} template and cannot be "
                    "deleted via the API.  Only 'user' templates are writable."
                ),
            )

        # 3. Delete
        existing_path.unlink()

        # HTTP 204 No Content — must return an empty body.
        return Response(status_code=204)

    @app.post("/api/v1/templates/{name}/duplicate", status_code=201)
    async def duplicate_template_api(name: str) -> JSONResponse:
        """Duplicate an existing template.

        Creates a copy of the template with a new unique ID (appends
        ``-copy`` or ``-copy-N`` suffix).  The duplicate is always written
        to the project templates directory.

        Path parameter:
            name (str): Template name (file stem) or template ID.

        Returns:
            201 with the full template detail of the new copy.
            404 when the source template is not found.
        """
        engine = _make_engine()

        # 1. Resolve and load the original template
        existing_path = _resolve_template(name)
        try:
            template = engine.load_template(existing_path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"Failed to load template: {exc}",
            )

        # 2. Read original YAML content
        original_content = existing_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(original_content)
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=422,
                detail="Original template is not a valid YAML mapping",
            )

        # 3. Generate a unique duplicate ID
        base_id = template.id
        candidate = f"{base_id}-copy"
        counter = 1
        existing_ids = {t["id"] for t in engine.list_templates()}
        while candidate in existing_ids:
            counter += 1
            candidate = f"{base_id}-copy-{counter}"

        # 4. Modify the YAML to set the new id and name
        raw["id"] = candidate
        base_name = raw.get("name", template.name)
        _copy_match = re.match(r"^(.*?)\s*\(Copy(?:\s+(\d+))?\)$", base_name)
        if _copy_match:
            base_name = _copy_match.group(1)
            _copy_counter = int(_copy_match.group(2) or 1) + 1
            raw["name"] = f"{base_name} (Copy {_copy_counter})"
        else:
            raw["name"] = f"{base_name} (Copy)"
        new_content = yaml.dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True)

        # 5. Write to user templates dir (user-writable) — exclusive create to avoid TOCTOU race
        dest = _writable_template_path(engine, candidate, "user")
        try:
            with dest.open("x", encoding="utf-8") as f:
                f.write(new_content)
        except FileExistsError:
            # Retry with incremented counter suffix
            for _retry in range(2, 100):
                retry_candidate = f"{base_id}-copy-{_retry}"
                dest = _writable_template_path(engine, retry_candidate, "user")
                try:
                    with dest.open("x", encoding="utf-8") as f:
                        f.write(new_content)
                    raw["id"] = retry_candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise HTTPException(500, "Could not find a unique filename for duplicate")

        # 6. Load the new template and return full detail
        try:
            new_template = engine.load_template(dest)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"Duplicate was written but failed to load: {exc}",
            )

        source = _template_source(engine, dest)
        phases_data = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "model_tier": p.model_tier,
                "thinking_level": p.thinking_level,
                "depends_on": p.depends_on,
                "task_type": p.task_type,
            }
            for p in new_template.phases
        ]

        return JSONResponse(
            {
                "id": new_template.id,
                "name": new_template.name,
                "version": new_template.version,
                "description": new_template.description,
                "author": new_template.author,
                "tags": new_template.tags,
                "phases": phases_data,
                "example_input": new_template.example_input,
                "config_schema": new_template.config_schema or {},
                "source": source,
                "yaml_content": new_content,
            },
            status_code=201,
        )
