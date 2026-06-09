"""FastAPI REST API for the Orchestration Engine (Issue #257).

Provides a versioned JSON REST API backed by the same daemon-based
async execution infrastructure used by ``orch launch``.  This is
separate from ``web/app.py`` (browser UI) — it targets programmatic
consumers such as CI/CD pipelines, OpenClaw, and external scripts.

All dependencies (fastapi, uvicorn) are optional extras — import this
module only after confirming they are installed.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml

from orchestration_engine.db import default_db_path, parse_json_list
from orchestration_engine.env_utils import env_int

logger = logging.getLogger(__name__)


def _get_persistent_db_path() -> str:
    """Return the path to the persistent on-disk DB used by async runs.

    Thin string-returning wrapper around :func:`orchestration_engine.db.default_db_path`
    preserved for callsite signature compatibility (Issue #864 consolidation).
    """
    return str(default_db_path())


def _verify_github_signature(secret: str, payload_bytes: bytes, sig_header: Optional[str]) -> bool:
    """Verify an HMAC-SHA256 GitHub-style webhook signature.

    GitHub sends ``X-Hub-Signature-256: sha256=<hex_digest>``.  This function
    recomputes the HMAC-SHA256 of *payload_bytes* using *secret* and compares
    it to the digest in *sig_header* using a constant-time comparison to
    prevent timing attacks.

    Args:
        secret: Shared HMAC secret string.
        payload_bytes: Raw request body bytes.
        sig_header: Value of the ``X-Hub-Signature-256`` header
            (e.g. ``"sha256=abc123..."``).  May be ``None``.

    Returns:
        ``True`` when the signature is valid, ``False`` otherwise
        (including when *sig_header* is ``None`` or malformed).
    """
    if not sig_header:
        return False
    if not sig_header.startswith("sha256="):
        return False
    expected = sig_header[len("sha256="):]
    computed = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)


def _apply_input_map(payload: Dict[str, Any], input_map: Dict[str, Any]) -> Dict[str, Any]:
    """Transform a webhook payload dict into pipeline input vars using *input_map*.

    Each key in *input_map* becomes an input variable.  Values that start with
    ``"$."`` are treated as simple dot-path expressions into *payload*.  Other
    values are used as literals.

    Example::

        payload   = {"repository": {"full_name": "org/repo"}, "ref": "refs/heads/main"}
        input_map = {"repo": "$.repository.full_name", "branch": "$.ref", "env": "prod"}
        # result: {"repo": "org/repo", "branch": "refs/heads/main", "env": "prod"}

    Args:
        payload: Parsed webhook JSON body.
        input_map: Dict mapping pipeline variable names to payload paths or
            literal values.

    Returns:
        Dict of pipeline input variables.  Missing paths produce ``None``.
    """
    result: Dict[str, Any] = {}
    for var_name, path_or_literal in input_map.items():
        if isinstance(path_or_literal, str) and path_or_literal.startswith("$."):
            # Simple dot-path resolution: "$.a.b.c" → payload["a"]["b"]["c"]
            parts = path_or_literal[2:].split(".")
            value: Any = payload
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    value = None
                    break
            result[var_name] = value
        else:
            result[var_name] = path_or_literal
    return result


def _check_rate_limit(trigger_id: str, rate_limit: int, db: Any) -> bool:
    """Check whether a webhook trigger has exceeded its per-minute rate limit.

    Args:
        trigger_id: The trigger identifier to check.
        rate_limit: Maximum allowed invocations per minute (0 = unlimited).
        db: :class:`~orchestration_engine.db.Database` instance.

    Returns:
        ``True`` when the rate limit is exceeded (caller should return 429).
        ``False`` when the invocation is allowed.
    """
    if rate_limit == 0:
        return False  # Unlimited
    since = datetime.now(timezone.utc) - timedelta(seconds=60)
    count = db.count_webhook_invocations_since(trigger_id, since)
    return count >= rate_limit


class SlidingWindowRateLimiter:
    """A named, unit-testable sliding-window rate limiter for webhook triggers.

    Uses a 60-second sliding window backed by the ``webhook_invocations``
    table in the DB.  Delegates to the same logic as ``_check_rate_limit()``
    but wraps it in a proper class so it can be tested and extended
    independently.

    Example::

        limiter = SlidingWindowRateLimiter(db)
        if limiter.check(trigger_id, rate_limit):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    """

    def __init__(self, db: Any) -> None:
        """Initialise with a DB instance.

        Args:
            db: :class:`~orchestration_engine.db.Database` instance used to
                query invocation counts and record new invocations.
        """
        self._db = db

    def check(self, trigger_id: str, rate_limit: int) -> bool:
        """Return ``True`` if the rate limit is exceeded, ``False`` otherwise.

        Args:
            trigger_id: The trigger identifier to check.
            rate_limit: Maximum allowed invocations per minute (0 = unlimited).

        Returns:
            ``True`` when the caller should block the request (429).
            ``False`` when the invocation is allowed.
        """
        return _check_rate_limit(trigger_id, rate_limit, self._db)


# ── Module-scope admin helpers (round-4 audit) ────────────────────────────────
# These are pure transforms; lifting them out of `create_api_app`'s closure
# makes them directly testable, which closes a round-4 trivial-satisfaction
# finding (the previous in-closure test could not actually observe shared
# mutable state in module-level defaults because TestClient JSON-round-trips
# everything).

_ADMIN_DEFAULTS: Dict[str, Any] = {
    "autonomy_level": "4.3",
    "feature_flags": {
        "phase0_hard_gate": False,
        "extend_verdict": True,
        "dialogue_phase": False,
        "cross_repo": False,
    },
    "modes": {
        "openrouter": True,
        "standalone": True,
        "openclaw": False,
        "dry_run": True,
    },
}
_ADMIN_KNOWN_FLAGS = frozenset(_ADMIN_DEFAULTS["feature_flags"].keys())
_ADMIN_KNOWN_MODES = frozenset(_ADMIN_DEFAULTS["modes"].keys())


def _strict_coerce_bool(value: Any) -> Optional[bool]:
    """Strict bool coercion. Returns the bool when the input is one of
    the canonical truthy/falsy values, or ``None`` to signal
    "unrecognised — caller must decide what to substitute".

    Accepts: ``bool`` (any), ``int``/``float`` ∈ {0, 1}, and the strings
    ``true``/``false``/``yes``/``no``/``on``/``off``/``1``/``0`` plus
    the empty string (→ False). Everything else returns ``None`` —
    callers handle the substitute (per-field default in
    ``_coerce_admin_doc``, 400 response in the PUT handler).
    """
    if isinstance(value, bool):
        return value
    # `bool` is a subclass of `int`; the bool check above wins. Numbers
    # outside {0, 1} are explicitly rejected to avoid e.g. `2` becoming True.
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, float) and value in (0.0, 1.0):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"): return True
        if low in ("false", "0", "no", "off", ""): return False
    return None


def _coerce_admin_doc(loaded: Any) -> Dict[str, Any]:
    """Merge a possibly-malformed loaded JSON value over the defaults.

    Defensive against every shape that a hand-edited `admin.json` can
    take: not-a-dict, nested key is the wrong type, scalar where dict
    expected, missing keys, values that aren't bools. The output is
    ALWAYS the same shape as ``_ADMIN_DEFAULTS`` with every value
    type-validated; unparseable values fall back to the per-key default
    (NOT to `bool(value)` which would silently coerce "maybe" → True).

    Returns a freshly-constructed dict — callers may mutate it without
    affecting subsequent requests. Inner dicts are always built fresh
    via ``dict(_ADMIN_DEFAULTS["..."])`` so module-level defaults never
    get aliased.
    """
    if not isinstance(loaded, dict):
        return {
            "autonomy_level": _ADMIN_DEFAULTS["autonomy_level"],
            "feature_flags": dict(_ADMIN_DEFAULTS["feature_flags"]),
            "modes": dict(_ADMIN_DEFAULTS["modes"]),
        }
    merged: Dict[str, Any] = {
        "autonomy_level": str(loaded.get("autonomy_level", _ADMIN_DEFAULTS["autonomy_level"])),
        "feature_flags": dict(_ADMIN_DEFAULTS["feature_flags"]),
        "modes": dict(_ADMIN_DEFAULTS["modes"]),
    }
    ff = loaded.get("feature_flags")
    if isinstance(ff, dict):
        for k in _ADMIN_KNOWN_FLAGS:
            if k in ff:
                coerced = _strict_coerce_bool(ff[k])
                if coerced is not None:
                    merged["feature_flags"][k] = coerced
                # else: keep default for this flag (round-2 review fix)
    mm = loaded.get("modes")
    if isinstance(mm, dict):
        for k in _ADMIN_KNOWN_MODES:
            if k in mm:
                coerced = _strict_coerce_bool(mm[k])
                if coerced is not None:
                    merged["modes"][k] = coerced
    return merged


def _merge_feature_flags_with_passthrough(
    disk_flags: Dict[str, Any],
) -> Dict[str, Any]:
    """Canonicalise known flags + preserve unknown nested keys.

    Round-4 audit caught a regression: round-3's pre-write canonicalisation
    via `_coerce_admin_doc({"feature_flags": disk_flags})["feature_flags"]`
    silently dropped any flag a forward-compat operator (or beta build) had
    added to `feature_flags` but isn't in ``_ADMIN_KNOWN_FLAGS``. This
    helper does the same canonicalisation for known flags but preserves
    unknown ones verbatim, mirroring the `extra` top-level handling.
    """
    canonical = _coerce_admin_doc({"feature_flags": disk_flags})["feature_flags"]
    if not isinstance(disk_flags, dict):
        return canonical
    unknown = {k: v for k, v in disk_flags.items() if k not in _ADMIN_KNOWN_FLAGS}
    # Canonical values take precedence — operator-edited unknown keys do not
    # shadow the engine-managed known ones.
    return {**unknown, **canonical}


class _SseConnectionLimiter:
    """Per-process SSE connection counter + per-IP cap (Issue #841).

    Module-level singleton (``_SSE_LIMITER``) so that:
      - all FastAPI workers in the same process share counts
      - tests can import and drive admit/release directly without
        opening real streams (which TestClient blocks on indefinitely
        due to the 1-second poll loop)
      - the metrics endpoint and the stream endpoint read the same
        counters atomically

    Limits are env-var driven (``ORCH_SSE_MAX_TOTAL`` default 100,
    ``ORCH_SSE_MAX_PER_IP`` default 10; ``0`` disables the
    corresponding limit). Env vars are re-read on every ``admit`` call
    so operators can re-tune live without restarting.
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._active_total: int = 0
        self._active_per_ip: Dict[str, int] = {}

    @staticmethod
    def limits() -> Tuple[int, int]:
        """Return ``(max_total, max_per_ip)`` from env vars. Malformed
        values fall back to the documented defaults — never raises."""
        max_total = env_int(os.environ.get("ORCH_SSE_MAX_TOTAL"), 100)
        max_per_ip = env_int(os.environ.get("ORCH_SSE_MAX_PER_IP"), 10)
        return max_total, max_per_ip

    def admit(self, client_ip: str) -> Optional[str]:
        """Try to admit a new SSE connection. Returns ``None`` on
        success (counters incremented) or a human-readable detail
        string when a limit is exceeded (counters unchanged).

        Caller MUST call :meth:`release` with the SAME ``client_ip``
        from a finally block when the connection ends.
        """
        max_total, max_per_ip = self.limits()
        with self._lock:
            if max_total > 0 and self._active_total >= max_total:
                return (
                    f"SSE total connection limit reached "
                    f"({self._active_total}/{max_total}). Try again later."
                )
            if max_per_ip > 0:
                cur = self._active_per_ip.get(client_ip, 0)
                if cur >= max_per_ip:
                    return (
                        f"SSE per-IP connection limit reached "
                        f"({cur}/{max_per_ip} from {client_ip}). "
                        f"Close one before opening another."
                    )
            self._active_total += 1
            self._active_per_ip[client_ip] = self._active_per_ip.get(client_ip, 0) + 1
        return None

    def release(self, client_ip: str) -> None:
        """Decrement counters for a closing connection. Saturating at
        zero — never goes negative even if release() is called more
        times than admit() (defensive)."""
        with self._lock:
            self._active_total = max(0, self._active_total - 1)
            cur = self._active_per_ip.get(client_ip, 0)
            if cur <= 1:
                self._active_per_ip.pop(client_ip, None)
            else:
                self._active_per_ip[client_ip] = cur - 1

    def metrics(self) -> Dict[str, Any]:
        """Return a snapshot dict suitable for JSON serialisation."""
        max_total, max_per_ip = self.limits()
        with self._lock:
            return {
                "active_total": self._active_total,
                "active_per_ip": dict(self._active_per_ip),
                "max_total": max_total,
                "max_per_ip": max_per_ip,
            }

    def _reset_for_tests(self) -> None:
        """Used by the test suite to start each test from zero counts."""
        with self._lock:
            self._active_total = 0
            self._active_per_ip.clear()


# Process-wide singleton. The web app injects this into request handlers
# via a closure reference; tests import it directly to verify counters.
_SSE_LIMITER = _SseConnectionLimiter()


def create_api_app(
    db_path: Optional[str] = None,
    user_templates_dir: Optional["Path"] = None,  # type: ignore[name-defined]
) -> "FastAPI":  # noqa: F821 (type hint only)
    """Create and return the REST API FastAPI application.

    Args:
        db_path: Path to the SQLite DB for pipeline_runs.  Defaults to the
                 same persistent DB used by ``orch launch``.
        user_templates_dir: Override the user templates directory used by
                            :class:`~orchestration_engine.templates.TemplateEngine`.
                            Useful in tests to isolate the user template store.
                            Defaults to ``~/.orch/templates/``.

    Returns:
        Configured ``FastAPI`` instance.
    """
    import asyncio

    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    from sse_starlette.sse import EventSourceResponse

    from orchestration_engine import __version__
    from orchestration_engine.db import Database, TERMINAL_STATUSES
    from orchestration_engine.templates import TemplateEngine, TemplateNotFoundError
    from orchestration_engine.timestamps import (
        normalize_row as _normalize_row,
        normalize_ts as _normalize_ts,
        now_utc as _now_utc,
    )
    from orchestration_engine.webhooks import InputMapper, TriggerMatcher

    effective_db_path = db_path or _get_persistent_db_path()

    # Capture user_templates_dir so route handlers can create properly configured engines.
    _user_templates_dir = user_templates_dir

    def _make_engine() -> "TemplateEngine":  # type: ignore[name-defined]
        """Create a TemplateEngine with the configured user templates directory."""
        if _user_templates_dir is not None:
            return TemplateEngine(user_dir=_user_templates_dir)
        return TemplateEngine()

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
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "Template load error", "errors": [str(exc)]},
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    app = FastAPI(
        title="Orchestration Engine REST API",
        version=__version__,
        description=(
            "Programmatic JSON REST API for the Orchestration Engine.  "
            "Backed by the same daemon-based async execution used by ``orch launch``."
        ),
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )

    # CORS — wide-open for local/CI use; tighten in production deployments.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Startup sweep (#754) — reap zombie pipeline runs left over from a
    # previous engine crash so the ORCH_MAX_DAEMONS=8 cap (#839) is
    # accurate from the first request. Sweep errors are logged but
    # NEVER block server start — the API must come up even if the DB
    # is briefly unhealthy.
    # ------------------------------------------------------------------
    @app.on_event("startup")
    def _startup_sweep_zombie_runs() -> None:
        try:
            _db = Database(Path(effective_db_path))
            _swept = _db.sweep_zombie_runs()
            if _swept >= 1:
                logger.info(
                    "Startup sweep: marked %d zombie pipeline run(s) as crashed "
                    "(daemons died without updating status; #754)",
                    _swept,
                )
            else:
                logger.debug(
                    "Startup sweep: marked 0 zombie pipeline runs (clean state; #754)"
                )
        except Exception as _exc:  # pragma: no cover — last-resort guard
            logger.error(
                "Startup sweep failed (server will still start): %s: %s",
                type(_exc).__name__, _exc,
            )

    # ------------------------------------------------------------------
    # Pydantic request/response models
    # ------------------------------------------------------------------

    class LaunchRequest(BaseModel):
        """Body for POST /api/v1/runs — launch a new pipeline run.

        Accepts either ``template`` or ``template_id`` as the template identifier.
        ``template_id`` is an alias for ``template`` for API consumers that use
        the ``template_id`` naming convention.
        """

        template: Optional[str] = None
        """Template name (resolved from search paths) or path to a .yaml file."""

        template_id: Optional[str] = None
        """Alias for ``template``.  When provided, used as the template identifier."""

        mode: Literal["standalone", "openclaw", "openrouter", "dry-run"] = "dry-run"
        """Execution mode passed to the daemon subprocess."""

        input: Dict[str, Any] = {}
        """Initial pipeline input (equivalent to ``--input`` / ``--input-file``)."""

        output_dir: Optional[str] = None
        """Directory to write phase outputs.  Auto-generated when omitted."""

        gateway_url: Optional[str] = None
        """OpenClaw gateway URL (openclaw mode).  Falls back to OPENCLAW_GATEWAY_URL."""

        skip_scoring: bool = False
        """Skip auto-scoring even if the template declares a scenario."""

        executor: Optional[str] = None
        """Executor backend for standalone mode (api/claudecode/auto)."""

        api_key: Optional[str] = None
        """API key passed to daemon as env var. ANTHROPIC_API_KEY (standalone) or OPENROUTER_API_KEY (openrouter). Never persisted."""

        model_map: Optional[Dict[str, str]] = None
        """Custom model tier overrides for openrouter mode."""

        issue_number: Optional[int] = None
        """GitHub issue number to auto-fetch as pipeline input."""

        repo: Optional[str] = None
        """GitHub repository slug (owner/repo) for issue lookup."""

        @property
        def resolved_template(self) -> Optional[str]:
            """Return the effective template identifier (template or template_id)."""
            return self.template or self.template_id

    class RunResponse(BaseModel):
        """Serialised pipeline run record returned by the API."""

        run_id: str
        template_id: str
        template_path: str
        mode: str
        status: str
        current_phase: Optional[str]
        completed_phases: List[str]
        pid: Optional[int]
        output_dir: str
        error_message: Optional[str]
        gateway_url: Optional[str]
        skip_scoring: bool
        scoring_status: Optional[str]
        scoring_score: Optional[float]
        started_at: Optional[str]
        completed_at: Optional[str]
        created_at: Optional[str]
        parent_run_id: Optional[str] = None
        chain_depth: int = 0
        review_reason: Optional[str] = None
        reviewed_at: Optional[str] = None
        reviewed_by: Optional[str] = None

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
    # Pydantic models — Trigger CRUD
    # ------------------------------------------------------------------

    class TriggerCreateRequest(BaseModel):
        """Body for POST /api/v1/triggers — create a new webhook trigger."""

        id: Optional[str] = None
        """Trigger identifier.  When omitted a unique ID is generated
        automatically (``trig-<12 hex chars>``)."""

        template_id: str
        """ID of the pipeline template to run when this trigger fires."""

        mode: str = "async"
        """Execution mode: ``'sync'``, ``'async'``, or ``'fire_and_forget'``."""

        secret: Optional[str] = None
        """Optional shared HMAC secret for request verification (write-only —
        never returned in responses)."""

        rate_limit: int = 0
        """Maximum requests per minute (0 = unlimited)."""

        input_map: Dict[str, Any] = {}
        """Maps webhook payload fields to pipeline input variables."""

        filters: List[Dict[str, Any]] = []
        """List of filter conditions evaluated against incoming payloads."""

        enabled: bool = True
        """Whether this trigger is active.  Disabled triggers silently skip."""

    class TriggerUpdateRequest(BaseModel):
        """Body for PUT /api/v1/triggers/{id} — update an existing trigger.

        All fields are optional; only provided fields are updated.
        """

        mode: Optional[str] = None
        secret: Optional[str] = None
        rate_limit: Optional[int] = None
        input_map: Optional[Dict[str, Any]] = None
        filters: Optional[List[Dict[str, Any]]] = None
        enabled: Optional[bool] = None

    class TriggerResponse(BaseModel):
        """Serialised trigger record returned by the API.

        Note:
            The ``secret`` field is always redacted to ``'***'`` in responses.
            Secrets are write-only.
        """

        id: str
        template_id: str
        mode: str
        secret: Optional[str]
        """Always ``'***'`` when a secret is configured, ``None`` otherwise."""
        rate_limit: int
        input_map: Dict[str, Any]
        filters: List[Dict[str, Any]]
        enabled: bool
        created_at: Optional[str]

    # ------------------------------------------------------------------
    # Helper — redact secret and build TriggerResponse dict from a DB row
    # ------------------------------------------------------------------

    def _trigger_to_response(row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB trigger row dict to a TriggerResponse-compatible dict.

        The ``secret`` field is redacted to ``'***'`` when set, to enforce
        write-only semantics.

        Args:
            row: A trigger row dict as returned by ``db.get_trigger()`` or
                 ``db.list_triggers()``.

        Returns:
            A dict safe to serialise as a ``TriggerResponse``.
        """
        return {
            "id": row["id"],
            "template_id": row["template_id"],
            "mode": row.get("mode", "async"),
            "secret": "***" if row.get("secret") else None,
            "rate_limit": row.get("rate_limit", 0),
            "input_map": row.get("input_map") or {},
            "filters": row.get("filters") or [],
            "enabled": bool(row.get("enabled", True)),
            "created_at": row.get("created_at"),
        }

    # ------------------------------------------------------------------
    # Helper — build RunResponse dict from a DB row
    # ------------------------------------------------------------------

    def _run_to_dict(run: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB pipeline_runs row dict to a RunResponse-compatible dict."""
        # completed_phases is stored as a JSON string in the DB — defer to
        # the canonical parser in db.py (Issue #866).
        completed_phases = parse_json_list(run.get("completed_phases"))

        return {
            "run_id": run["run_id"],
            "template_id": run.get("template_id", ""),
            "template_path": run.get("template_path", ""),
            "mode": run.get("mode", ""),
            "status": run.get("status", ""),
            "current_phase": run.get("current_phase"),
            "completed_phases": completed_phases,
            "pid": run.get("pid"),
            "output_dir": run.get("output_dir", ""),
            "error_message": run.get("error_message"),
            "gateway_url": run.get("gateway_url"),
            "skip_scoring": bool(run.get("skip_scoring", 0)),
            "scoring_status": run.get("scoring_status"),
            "scoring_score": run.get("scoring_score"),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "created_at": run.get("created_at"),
            "parent_run_id": run.get("parent_run_id"),       # Issue #330.3: chaining parent
            "chain_depth": int(run.get("chain_depth") or 0), # Issue #330.3: chaining depth
            "review_reason": run.get("review_reason"),         # Issue #331.4: review queue
            "reviewed_at": run.get("reviewed_at"),             # Issue #331.4: review queue
            "reviewed_by": run.get("reviewed_by"),             # Issue #331.4: review queue
        }

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
            except ValueError:
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
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', template_id):
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

    # ------------------------------------------------------------------
    # Helper — resolve template name or path
    # ------------------------------------------------------------------

    def _resolve_template(name_or_path: str) -> Path:
        """Resolve a template name or file path to an absolute Path.

        Resolution strategy (mirrors cli.py ``_resolve_template_arg``):
        1. Direct file path (has .yaml/.yml extension or path separators).
        2. File-stem resolution via ``TemplateEngine.resolve_template``.
        3. Template-ID scan (handles cases where the YAML ``id`` field
           differs from the file stem, e.g. ``content-pipeline-v24`` in
           ``templates/content-pipeline.yaml``).

        Raises HTTPException(404) when not found.
        """
        looks_like_path = (
            name_or_path.endswith(".yaml")
            or name_or_path.endswith(".yml")
            or os.sep in name_or_path
            or "/" in name_or_path
        )

        if looks_like_path:
            p = Path(name_or_path)
            if not p.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"Template file not found: {name_or_path}",
                )
            # Sandbox check: ensure path is within allowed template directories
            engine = _make_engine()
            resolved = p.resolve()
            allowed = any(
                resolved.is_relative_to(d.resolve())
                for d, _ in engine.get_search_paths()
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail="Path outside template directories",
                )
            return p

        engine = _make_engine()

        # 1. File-stem resolution (fast path)
        try:
            return engine.resolve_template(name_or_path)
        except TemplateNotFoundError:
            pass

        # 2. Scan all templates and match by template ID
        for entry in engine.list_templates():
            if entry["id"] == name_or_path:
                return Path(entry["path"])

        raise HTTPException(
            status_code=404,
            detail=f"Template '{name_or_path}' not found",
        )

    # ------------------------------------------------------------------
    # Helper — launch a pipeline run (extracted for reuse by webhook route)
    # ------------------------------------------------------------------

    def _launch_pipeline_from_trigger(
        template_file: Path,
        template: Any,
        input_data: Dict[str, Any],
        mode: str,
        gateway_url: Optional[str],
        db: Any,
        skip_scoring: bool = False,
        output_dir_override: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Core pipeline launch logic: DB row + daemon spawn + return run dict.

        Encapsulates the shared logic used by both ``POST /api/v1/runs`` and
        ``POST /api/v1/webhooks/{trigger_id}`` so neither handler duplicates
        it.

        Args:
            template_file: Resolved absolute path to the template YAML.
            template: Loaded and validated template object.
            input_data: Pipeline input variables dict.
            mode: Execution mode (``'standalone'``, ``'openclaw'``,
                ``'dry-run'``).
            gateway_url: OpenClaw gateway URL (or ``None``).
            db: Open :class:`~orchestration_engine.db.Database` instance.
            skip_scoring: Skip auto-scoring.  Default ``False``.
            output_dir_override: Optional explicit output directory path.

        Returns:
            A :class:`RunResponse`-shaped dict for the newly created run.

        Raises:
            HTTPException(429): When ``ORCH_MAX_DAEMONS`` active runs are
                already executing (#839 backpressure). The launcher does
                NOT spawn another daemon process while this many are
                already in flight — unbounded concurrent daemons trip
                SQLite WAL contention and produce zombie runs (#754).
        """
        # ── Backpressure check (#839) ────────────────────────────────
        # Default cap matches the empirical safe ceiling for SQLite WAL
        # on a 4-core dev laptop; production deployments raise it via
        # the env var. A value of 0 disables the cap entirely (legacy
        # behaviour). We read the env var on every launch so an operator
        # can tune live without restarting the server.
        _max_daemons = env_int(os.environ.get("ORCH_MAX_DAEMONS"), 8)
        if _max_daemons > 0:
            _active = db.count_active_pipeline_runs()
            if _active >= _max_daemons:
                logger.warning(
                    "Launch rejected (#839 backpressure): %d active runs "
                    ">= ORCH_MAX_DAEMONS=%d. Wait for in-flight runs to "
                    "complete or raise the cap.",
                    _active, _max_daemons,
                )
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Backpressure: {_active} pipeline runs already active "
                        f"(cap ORCH_MAX_DAEMONS={_max_daemons}). Wait for "
                        f"in-flight runs to complete or raise the cap."
                    ),
                    headers={"Retry-After": "30"},
                )

        run_id = str(uuid.uuid4())[:8]
        if output_dir_override:
            output_dir = Path(output_dir_override)
        else:
            _safe_id = re.sub(r'[^\w\-]', '_', template.id)
            _ts = _now_utc().strftime('%Y%m%d-%H%M%S')
            output_dir = Path(f"./output/{_safe_id}-{_ts}-{run_id}")
        output_dir.mkdir(parents=True, exist_ok=True)

        db.insert_pipeline_run(
            {
                "run_id": run_id,
                "template_path": str(template_file.resolve()),
                "template_id": template.id,
                "input_json": json.dumps(input_data),
                "mode": mode,
                "output_dir": str(output_dir.resolve()),
                "gateway_url": gateway_url,
                "skip_scoring": int(skip_scoring),
                "status": "pending",
            }
        )

        log_file_path = output_dir / ".orch-daemon.log"
        daemon_env = {**os.environ, **(extra_env or {})}
        with open(str(log_file_path), "a") as log_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "orchestration_engine.daemon",
                    run_id,
                    effective_db_path,
                ],
                start_new_session=True,
                stdout=log_fh,
                stderr=log_fh,
                env=daemon_env,
            )

        db.update_pipeline_run(run_id, pid=proc.pid)

        run = db.get_pipeline_run(run_id)
        return _run_to_dict(run)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/api/v1/health")
    async def health() -> JSONResponse:
        """Return API server health status."""
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/api/v1/health/webhook")
    async def webhook_health() -> JSONResponse:
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
        _KNOWN_REGRESSION_TEMPLATE_IDS = {
            "regression-pipeline-v1",
            "regression-fix-pipeline-v1",
        }
        _REGRESSION_WEBHOOK_PATH_SUFFIX = "/api/v1/webhooks/"

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
            except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
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
                category = tpl.category or (
                    tpl.phases[0].task_type if tpl.phases else "general"
                )
                author = tpl.author or ""
            except Exception:
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
                    except Exception:
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
                    except Exception:
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
        except Exception as exc:
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
            except Exception as exc:
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
        except Exception as exc:
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
        except Exception as exc:
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
        except Exception as exc:
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
        # For PUT (update), extended validation errors are treated as warnings
        # (non-blocking) — a user template update with advisory issues is still
        # accepted. Only structural errors from validate_template() block the update.
        ext_errors: List[str] = []
        warnings: List[str] = []
        try:
            ext_errors, warnings = engine.validate_template_extended(template, raw)
        except Exception as exc:
            warnings = [f"Extended validation warning: {exc}"]

        # Treat extended errors as additional warnings for PUT (non-blocking).
        warnings = ext_errors + warnings

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
        except Exception as exc:
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
        base_name = raw.get('name', template.name)
        _copy_match = re.match(r'^(.*?)\s*\(Copy(?:\s+(\d+))?\)$', base_name)
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
            with dest.open('x', encoding='utf-8') as f:
                f.write(new_content)
        except FileExistsError:
            # Retry with incremented counter suffix
            for _retry in range(2, 100):
                retry_candidate = f"{base_id}-copy-{_retry}"
                dest = _writable_template_path(engine, retry_candidate, "user")
                try:
                    with dest.open('x', encoding='utf-8') as f:
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
        except Exception as exc:
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

    # ---- Pipeline Runs -----------------------------------------------

    @app.post("/api/v1/runs", status_code=201)
    async def launch_run(req: LaunchRequest) -> JSONResponse:
        """Launch a new pipeline run in the background.

        Equivalent to ``orch launch`` — spawns a daemon subprocess and returns
        immediately with the run record.  Poll ``GET /api/v1/runs/{run_id}``
        to track progress.

        Returns:
            201 with the new run record (same shape as GET /api/v1/runs/{run_id}).
        """
        # 1. Resolve and validate template
        # Support both 'template' and 'template_id' fields (template_id is an alias)
        effective_template = req.resolved_template
        if not effective_template:
            raise HTTPException(
                status_code=422,
                detail="Either 'template' or 'template_id' must be provided",
            )
        template_file = _resolve_template(effective_template)

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        errors = engine.validate_template(template)
        if req.skip_scoring:
            errors = [e for e in errors if "require a scenario" not in e]
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"message": "Template has validation errors", "errors": errors},
            )

        # 2. Prepare input data with executor and model map overrides
        launch_input = dict(req.input)
        if req.executor:
            launch_input['_executor_type'] = req.executor
        if req.model_map:
            launch_input['_model_map'] = req.model_map

        # Build extra env vars for API key (never persisted to DB)
        extra_env: Dict[str, str] = {}
        if req.api_key:
            if req.mode == 'openrouter':
                extra_env['OPENROUTER_API_KEY'] = req.api_key
            else:
                extra_env['ANTHROPIC_API_KEY'] = req.api_key

        # 2b. Launch via shared helper (DB row + daemon spawn)
        db = Database(Path(effective_db_path))
        effective_gw_url = req.gateway_url or os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=launch_input,
            mode=req.mode,
            gateway_url=effective_gw_url,
            db=db,
            skip_scoring=req.skip_scoring,
            output_dir_override=req.output_dir,
            extra_env=extra_env or None,
        )

        # 3. Return the created run record
        return JSONResponse(run_dict, status_code=201)

    @app.post("/api/v1/webhooks/{trigger_id}")
    async def handle_webhook(trigger_id: str, request: Request) -> JSONResponse:
        """Receive an incoming webhook and fire the associated pipeline.

        Looks up the trigger configuration, verifies the HMAC-SHA256 signature
        (when a ``secret`` is configured), enforces the per-trigger rate limit,
        applies the ``input_map`` to transform the webhook payload into pipeline
        input vars, and launches the pipeline.

        Path parameter:
            trigger_id (str): Trigger identifier (must match a row in the
                ``triggers`` table).

        Request headers:
            X-Hub-Signature-256: Optional HMAC-SHA256 signature header
                (required when the trigger has a ``secret`` configured).

        Returns:
            - **200** when the trigger is disabled (no pipeline launched).
            - **201** when the pipeline was launched in ``async`` or ``sync``
              mode.
            - **200** when the trigger mode is ``fire_and_forget``.
            - **403** when the signature is invalid or missing (and a secret
              is configured).
            - **404** when the trigger is not found.
            - **429** when the rate limit is exceeded.
        """
        db = Database(Path(effective_db_path))

        # 1. Look up trigger
        trigger_row = db.get_trigger(trigger_id)
        if trigger_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )

        # 2. Check enabled flag — disabled triggers silently accept but skip
        if not trigger_row.get("enabled", True):
            return JSONResponse(
                {"status": "skipped", "reason": "trigger_disabled"},
                status_code=200,
            )

        # 3. Read body bytes (must happen before signature verification)
        payload_bytes = await request.body()

        # 4. Verify GitHub HMAC-SHA256 signature (if secret configured)
        secret = trigger_row.get("secret")
        if secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(secret, payload_bytes, sig_header):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing webhook signature",
                )

        # 5. Rate-limit check (sliding-window, 60-second window)
        rate_limit = trigger_row.get("rate_limit", 0)
        limiter = SlidingWindowRateLimiter(db)
        if limiter.check(trigger_id, rate_limit):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit of {rate_limit} req/min exceeded for trigger '{trigger_id}'",
            )

        # 6. Parse payload JSON
        try:
            payload: Dict[str, Any] = json.loads(payload_bytes) if payload_bytes else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}

        # 6b. Evaluate trigger filters — if any filter doesn't match, skip silently
        filters = trigger_row.get("filters") or []
        if filters and not TriggerMatcher.matches(filters, payload):
            return JSONResponse(
                {"status": "skipped", "reason": "filter_mismatch"},
                status_code=200,
            )

        # 7. Apply input_map to transform payload → pipeline input
        # Values starting with "$." are resolved by _apply_input_map (dot-path).
        # Values of the form "{{payload.x.y}}" are resolved by InputMapper (template).
        # Other values are treated as literals.
        input_map = trigger_row.get("input_map") or {}
        if input_map:
            # First pass: resolve $.path expressions
            dot_path_map = {
                k: v for k, v in input_map.items()
                if not (isinstance(v, str) and v.startswith("{{payload."))
            }
            template_map = {
                k: v for k, v in input_map.items()
                if isinstance(v, str) and v.startswith("{{payload.")
            }
            input_data = _apply_input_map(payload, dot_path_map) if dot_path_map else {}
            # Second pass: resolve {{payload.*}} templates
            if template_map:
                input_data.update(InputMapper.apply(payload, template_map))
        else:
            input_data = dict(payload)

        # 8. Resolve and validate template
        template_id = trigger_row["template_id"]
        template_file = _resolve_template(template_id)

        engine = TemplateEngine()
        try:
            template = engine.load_template(template_file)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid template: {exc}")

        # 9. Record invocation (after all guards pass, before launching)
        db.record_webhook_invocation(trigger_id)

        # 10. Launch pipeline via shared helper
        trigger_mode = trigger_row.get("mode", "async")
        # Map trigger mode to daemon mode (fire_and_forget → standalone)
        daemon_mode_map = {
            "sync": "standalone",
            "async": "standalone",
            "fire_and_forget": "standalone",
        }
        daemon_mode = daemon_mode_map.get(trigger_mode, "standalone")

        gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL")
        run_dict = _launch_pipeline_from_trigger(
            template_file=template_file,
            template=template,
            input_data=input_data,
            mode=daemon_mode,
            gateway_url=gateway_url,
            db=db,
        )

        # fire_and_forget → respond 200 (accepted, no run details)
        if trigger_mode == "fire_and_forget":
            return JSONResponse(
                {"status": "accepted", "run_id": run_dict["run_id"]},
                status_code=200,
            )

        return JSONResponse(run_dict, status_code=201)

    # ------------------------------------------------------------------
    # Trigger CRUD endpoints
    # ------------------------------------------------------------------

    @app.post("/api/v1/triggers", status_code=201)
    async def create_trigger(body: TriggerCreateRequest) -> JSONResponse:
        """Create a new webhook trigger.

        Args:
            body: ``TriggerCreateRequest`` JSON body.

        Returns:
            - **201** with a ``TriggerResponse`` JSON object on success.
            - **400** when the trigger config fails validation.
            - **409** when a trigger with the same ``id`` already exists.
        """
        from orchestration_engine.webhooks import TriggerConfig, TriggerValidationError

        db = Database(Path(effective_db_path))

        trigger_id = body.id or TriggerConfig.generate_id()

        try:
            cfg = TriggerConfig(
                id=trigger_id,
                template_id=body.template_id,
                mode=body.mode,
                secret=body.secret,
                rate_limit=body.rate_limit,
                input_map=body.input_map,
                filters=body.filters,
                enabled=body.enabled,
            )
        except TriggerValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        import sqlite3
        try:
            db.create_trigger(cfg.to_dict())
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail=f"Trigger with id '{trigger_id}' already exists",
            )

        row = db.get_trigger(trigger_id)
        return JSONResponse(_trigger_to_response(row), status_code=201)

    @app.get("/api/v1/triggers")
    async def list_triggers_endpoint(
        template_id: Optional[str] = None,
        mode: Optional[str] = None,
        enabled: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        """List webhook triggers with optional filtering and pagination.

        Query parameters:
            template_id: Filter by template id.
            mode: Filter by execution mode.
            enabled: ``'true'`` / ``'false'`` to filter by enabled state.
            limit: Maximum number of results (default 100).
            offset: Number of results to skip (for pagination).

        Returns:
            JSON object with ``items`` array.
        """
        db = Database(Path(effective_db_path))

        # Parse the enabled query param (string) into Optional[bool]
        enabled_filter: Optional[bool] = None
        if enabled is not None:
            if enabled.lower() == "true":
                enabled_filter = True
            elif enabled.lower() == "false":
                enabled_filter = False
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Query param 'enabled' must be 'true' or 'false'",
                )

        rows = db.list_triggers(
            template_id=template_id,
            mode=mode,
            enabled=enabled_filter,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"items": [_trigger_to_response(r) for r in rows]})

    @app.get("/api/v1/triggers/{trigger_id}")
    async def get_trigger_endpoint(trigger_id: str) -> JSONResponse:
        """Get a single webhook trigger by id.

        Path parameter:
            trigger_id: Trigger identifier.

        Returns:
            - **200** with a ``TriggerResponse`` JSON object.
            - **404** when the trigger is not found.
        """
        db = Database(Path(effective_db_path))
        row = db.get_trigger(trigger_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )
        return JSONResponse(_trigger_to_response(row))

    @app.put("/api/v1/triggers/{trigger_id}")
    async def update_trigger_endpoint(
        trigger_id: str, body: TriggerUpdateRequest
    ) -> JSONResponse:
        """Update an existing webhook trigger.

        Only fields that are explicitly provided in the request body are
        updated; omitted fields retain their current values.

        Path parameter:
            trigger_id: Trigger identifier.

        Args:
            body: ``TriggerUpdateRequest`` JSON body.

        Returns:
            - **200** with the updated ``TriggerResponse`` JSON object.
            - **400** when validation fails.
            - **404** when the trigger is not found.
        """
        from orchestration_engine.webhooks import TriggerValidationError

        db = Database(Path(effective_db_path))

        # Verify the trigger exists
        existing = db.get_trigger(trigger_id)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )

        # Build kwargs with only the fields that were provided
        update_kwargs: Dict[str, Any] = {}
        if body.mode is not None:
            update_kwargs["mode"] = body.mode
        if body.secret is not None:
            update_kwargs["secret"] = body.secret
        if body.rate_limit is not None:
            update_kwargs["rate_limit"] = body.rate_limit
        if body.input_map is not None:
            update_kwargs["input_map"] = body.input_map
        if body.filters is not None:
            update_kwargs["filters"] = body.filters
        if body.enabled is not None:
            update_kwargs["enabled"] = body.enabled

        if update_kwargs:
            # Validate the merged result before writing
            from orchestration_engine.webhooks import TriggerConfig
            merged = {**existing, **update_kwargs}
            try:
                TriggerConfig.from_dict(merged)
            except TriggerValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            db.update_trigger(trigger_id, **update_kwargs)

        row = db.get_trigger(trigger_id)
        return JSONResponse(_trigger_to_response(row))

    @app.delete("/api/v1/triggers/{trigger_id}", status_code=204)
    async def delete_trigger_endpoint(trigger_id: str) -> Response:
        """Delete a webhook trigger.

        Path parameter:
            trigger_id: Trigger identifier.

        Returns:
            - **204** (no content) on success.
            - **404** when the trigger is not found.
        """
        db = Database(Path(effective_db_path))
        deleted = db.delete_trigger(trigger_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Trigger '{trigger_id}' not found",
            )
        return Response(status_code=204)

    @app.get("/api/v1/runs")
    async def list_runs(
        status: Optional[str] = None,
        template_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """List pipeline runs with optional filtering and pagination.

        Query parameters:
            status: Filter by run status (pending, running, success, failed, cancelled, crashed).
            template_id: Filter by template ID.
            limit: Maximum number of results (default 20, max 100).
            offset: Number of results to skip (for pagination).

        Returns:
            JSON object with ``items`` array and ``total`` count.
        """
        # Clamp limit/offset to avoid surprising SQLite behaviour with
        # negative values (negative LIMIT means "no limit"; negative OFFSET
        # is treated as 0 by SQLite but is semantically wrong).
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        db = Database(Path(effective_db_path))
        runs = db.list_pipeline_runs_filtered(
            status=status,
            template_id=template_id,
            limit=limit,
            offset=offset,
        )
        # Get total count (without pagination)
        total = db.count_pipeline_runs(status=status, template_id=template_id)
        items = [_run_to_dict(r) for r in runs]
        return JSONResponse({"items": items, "total": total, "limit": limit, "offset": offset})

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        """Return the current state of a pipeline run.

        Also performs a liveness check on the daemon PID: if the process is
        no longer alive but the run is still ``running``, the status is updated
        to ``crashed`` in the DB before returning.

        Raises 404 when the run ID is not found.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        # PID liveness check
        if run.get("status") == "running" and run.get("pid"):
            try:
                from orchestration_engine.daemon import is_process_alive

                if not is_process_alive(run["pid"]):
                    db.update_pipeline_run(run_id, status="crashed")
                    run["status"] = "crashed"
            except Exception:
                pass

        return JSONResponse(_run_to_dict(run))

    @app.get("/api/v1/runs/{run_id}/children")
    async def get_run_children(run_id: str) -> JSONResponse:
        """Return all child pipeline runs spawned by a parent run.

        Queries ``pipeline_runs WHERE parent_run_id = run_id`` ordered by
        ``created_at ASC``.

        Returns::

            {
                "run_id": "<parent-run-id>",
                "children": [<RunResponse-shaped dicts>, ...]
            }

        Raises 404 when the parent run ID is not found.
        """  # Issue #330.3: children REST API
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        children = db.list_pipeline_run_children(run_id)
        return JSONResponse({
            "run_id": run_id,
            "children": [_run_to_dict(c) for c in children],
        })

    @app.get("/api/v1/runs/{run_id}/logs")
    async def get_run_logs(run_id: str) -> JSONResponse:
        """Return the daemon log file contents for a pipeline run.

        Returns ``{"run_id": ..., "log": "..."}`` where ``log`` is the full
        text of the ``.orch-daemon.log`` file.

        Raises 404 when the run ID or log file is not found.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        log_path = Path(run["output_dir"]) / ".orch-daemon.log"
        if not log_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Log file not found for run '{run_id}' (run may not have started yet)",
            )

        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"run_id": run_id, "log": log_text})

    # ── Run artifact endpoints (filed as harness gaps from PR #816) ──
    # The pipeline writes one file per phase under `output_dir`. The harness
    # needs to read these to render the Run Cockpit artifacts list, the
    # Phase 0 inventory card, and the Adversary Loop dialogue rounds.
    #
    # All three endpoints follow the same pattern:
    #   1. Resolve `output_dir` from the run record (404 if missing)
    #   2. Constrain the requested path within `output_dir` (defence against
    #      path traversal: `Path.resolve()` + `is_relative_to`)
    #   3. Cap any returned text at 1 MiB so a malformed artifact can't OOM
    #      the response (operators chasing a truly huge artifact can still
    #      ssh to the daemon host).

    # Maximum bytes returned for any single artifact body. The Phase 0
    # inventory + dialogue artifacts are typically 2-10 KB; spec / behavioral
    # / review markdown are 3-50 KB; even an aggressive run rarely exceeds
    # 200 KB total. 1 MiB is a comfortable ceiling.
    _ARTIFACT_MAX_BYTES = 1024 * 1024

    def _resolve_output_dir(run: Dict[str, Any]) -> Path:
        """Return the run's output_dir as a resolved absolute Path.

        Raises ``HTTPException(404)`` when ``output_dir`` is missing from
        the run record or does not exist on disk.
        """
        out = run.get("output_dir")
        if not out:
            raise HTTPException(status_code=404, detail="Run has no output_dir")
        out_path = Path(out).resolve()
        if not out_path.exists() or not out_path.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Run output_dir does not exist on disk: {out_path}",
            )
        return out_path

    def _read_artifact(out_dir: Path, filename: str) -> str:
        """Read *filename* from *out_dir* with path-traversal + size guards.

        Returns the file's text content (UTF-8, replacement on invalid bytes),
        truncated at ``_ARTIFACT_MAX_BYTES`` with an explicit truncation
        marker appended when over the limit.
        """
        # Constrain to a single path segment — no traversal, no nesting.
        # Even though we re-resolve below, this is a fast-fail that gives a
        # better error message than "file not found" for crafted inputs.
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="Invalid artifact filename")

        target = (out_dir / filename).resolve()
        try:
            target.relative_to(out_dir)
        except ValueError:
            raise HTTPException(status_code=400, detail="Artifact path escapes output_dir")

        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"Artifact '{filename}' not found")

        raw = target.read_bytes()
        truncated = len(raw) > _ARTIFACT_MAX_BYTES
        if truncated:
            raw = raw[:_ARTIFACT_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[…truncated by API: artifact exceeded 1 MiB…]"
        return text

    @app.get("/api/v1/runs/{run_id}/artifacts")
    async def list_run_artifacts(run_id: str) -> JSONResponse:
        """List files in a run's output_dir.

        Returns ``{"run_id": ..., "output_dir": ..., "files": [...]}`` where
        each file entry has ``{name, size_bytes, mtime}``. Hidden files
        (those starting with ``.``) are excluded — the daemon's
        ``.orch-daemon.log`` is exposed via ``/logs`` instead.

        Files are sorted alphabetically by name (the conventional
        phase-ordered prefixes — ``0_existing_symbols.md``, ``1_spec.md``,
        etc. — sort naturally in pipeline order).
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)

        files = []
        for entry in sorted(out_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or not entry.is_file():
                continue
            stat = entry.stat()
            files.append({
                "name": entry.name,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            })
        return JSONResponse({
            "run_id": run_id,
            "output_dir": str(out_dir),
            "files": files,
        })

    @app.get("/api/v1/runs/{run_id}/artifacts/{filename}")
    async def get_run_artifact(run_id: str, filename: str) -> JSONResponse:
        """Return the body of a single artifact file from a run's output_dir.

        Path-traversal guarded; body truncated at 1 MiB. The response shape
        is ``{"run_id": ..., "filename": ..., "size_bytes": ..., "content": ...}``
        — text only; binary files will be returned with replacement chars.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)
        content = _read_artifact(out_dir, filename)
        target = (out_dir / filename).resolve()
        return JSONResponse({
            "run_id": run_id,
            "filename": filename,
            "size_bytes": target.stat().st_size,
            "content": content,
        })

    @app.get("/api/v1/runs/{run_id}/phase0")
    async def get_run_phase0(run_id: str) -> JSONResponse:
        """Parse the Phase 0 existing-symbols inventory for a run.

        Looks for ``existing_symbols.md`` (or a phase-numbered variant) in
        the run's ``output_dir`` and extracts the four canonical sections
        defined in `coding-pipeline-standard.yaml` v4.2:

            1. UI primitives
            2. Project shared libraries
            3. Adjacent action / hook / route patterns
            4. Workspace barrels

        Plus the §5/§6 verdict-label entries when present.

        Returns ``{"run_id": ..., "sections": {"ui_primitives": {"count": N,
        "entries": [...]}, "shared_libs": {...}, "adjacent_patterns": {...},
        "workspace_barrels": {...}}, "verdicts": {"CONSUME": N, "EXTEND": N,
        "DIVERGENT": N, "NEW_OK": N}, "raw": "..."}``.

        Raises 404 if no Phase 0 artifact exists (the pipeline may have used
        ``coding-pipeline-skip-spec.yaml`` which has no Phase 0).
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)

        # Try common filename variants (Phase 0 may be numbered, prefixed, etc.)
        candidates = [
            "existing_symbols.md",
            "0_existing_symbols.md",
            "phase0_existing_symbols.md",
            "existing_symbols_inventory.md",
        ]
        artifact: Optional[Path] = None
        for name in candidates:
            p = out_dir / name
            if p.exists() and p.is_file():
                artifact = p
                break
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail=f"No Phase 0 inventory artifact in {out_dir} (tried: {candidates})",
            )

        raw = artifact.read_text(encoding="utf-8", errors="replace")

        # Parse the four standard sections. Headings come from the v4.2 YAML
        # ("## 1. UI primitives", "## 2. Project shared libraries", etc.).
        # We split on the next "## " heading after each section's start.
        import re as _re

        section_specs: List[Tuple[str, str]] = [
            ("ui_primitives", r"^##\s+1\.\s+UI primitives"),
            ("shared_libs", r"^##\s+2\.\s+Project shared libraries"),
            ("adjacent_patterns", r"^##\s+3\.\s+Adjacent"),
            ("workspace_barrels", r"^##\s+4\.\s+Workspace barrels"),
        ]
        sections: Dict[str, Dict[str, Any]] = {}
        for key, pattern in section_specs:
            m = _re.search(pattern, raw, _re.MULTILINE)
            if m is None:
                sections[key] = {"count": 0, "entries": []}
                continue
            start = m.end()
            next_h2 = _re.search(r"^##\s+\d", raw[start:], _re.MULTILINE)
            body = raw[start : (start + next_h2.start()) if next_h2 else len(raw)]
            # An "entry" is any bullet line (- something) or backtick-wrapped
            # symbol line. Empty stubs ("(empty — consumer did not provide…)")
            # produce zero entries.
            entries = []
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") and "(empty —" not in stripped:
                    entries.append(stripped[2:])
            sections[key] = {"count": len(entries), "entries": entries[:50]}  # cap per-section payload

        # Verdict label counts from §5/§6 (CONSUME / EXTEND / DIVERGENT / NEW-OK / BLOCKED)
        verdicts = {
            "CONSUME": len(_re.findall(r"\bCONSUME\b", raw)),
            "EXTEND": len(_re.findall(r"\bEXTEND\b", raw)),
            "DIVERGENT": len(_re.findall(r"\bDIVERGENT\b", raw)),
            "NEW_OK": len(_re.findall(r"\bNEW[-_]OK\b", raw)),
            "BLOCKED": len(_re.findall(r"\bBLOCKED\b", raw)),
        }

        return JSONResponse({
            "run_id": run_id,
            "filename": artifact.name,
            "sections": sections,
            "verdicts": verdicts,
            "raw": raw if len(raw) <= _ARTIFACT_MAX_BYTES else raw[:_ARTIFACT_MAX_BYTES] + "\n[…truncated…]",
        })

    @app.get("/api/v1/runs/{run_id}/dialogue")
    async def get_run_dialogue(run_id: str) -> JSONResponse:
        """Return the cross-model dialogue artifact for a run, if present.

        Looks for ``dialogue.md`` / ``dialogue_phase.md`` / ``spec-review-dialogue.md``
        in the run's ``output_dir``. The dialogue artifact is written by
        the Track B dialogue phase (PR #808) — only runs that used the
        ``coding-pipeline-with-dialogue`` template (or equivalent) will have one.

        Returns ``{"run_id": ..., "filename": ..., "rounds": [...], "raw": "..."}``.
        Each round entry is ``{"index": N, "side": "drafter" | "reviewer",
        "model": "...", "verdict": "...", "content": "...", "jaccard": float | null}``.

        Raises 404 when no dialogue artifact exists (most runs).
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        out_dir = _resolve_output_dir(run)

        candidates = [
            "dialogue.md",
            "dialogue_phase.md",
            "spec-review-dialogue.md",
            "spec_review_dialogue.md",
        ]
        artifact: Optional[Path] = None
        for name in candidates:
            p = out_dir / name
            if p.exists() and p.is_file():
                artifact = p
                break
        if artifact is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No dialogue artifact in {out_dir} — this run did not use a "
                    f"dialogue phase (Track B, PR #808). Tried: {candidates}"
                ),
            )

        raw = _read_artifact(out_dir, artifact.name)

        # Parse rounds. The dialogue phase writes one section per turn
        # with a heading like "## Round N · DRAFTER (model)" or
        # "## Round N · REVIEWER (model) · VERDICT".
        import re as _re

        rounds: List[Dict[str, Any]] = []
        round_re = _re.compile(
            r"^##\s+Round\s+(?P<idx>\d+)\s*[·\-:]\s*"
            r"(?P<side>DRAFTER|REVIEWER)"
            r"(?:\s*\((?P<model>[^)]+)\))?"
            r"(?:\s*[·\-:]\s*(?P<verdict>APPROVE|REQUEST_CHANGES|REVISE|ABORT))?",
            _re.IGNORECASE | _re.MULTILINE,
        )
        matches = list(round_re.finditer(raw))
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            content = raw[start:end].strip()
            # Extract jaccard from content body if present
            # Capture decimal Jaccard value, stopping before any trailing
            # sentence punctuation (e.g. ``Jaccard 0.93.`` → ``0.93``).
            jac_m = _re.search(r"[Jj]accard[^0-9]*(\d+(?:\.\d+)?)", content)
            rounds.append({
                "index": int(m.group("idx")),
                "side": (m.group("side") or "").lower(),
                "model": m.group("model"),
                "verdict": (m.group("verdict") or "").lower() or None,
                "content": content[:4096],  # cap per-round body
                "jaccard": float(jac_m.group(1)) if jac_m else None,
            })

        return JSONResponse({
            "run_id": run_id,
            "filename": artifact.name,
            "rounds": rounds,
            "raw": raw,
        })

    # ── Harness aggregate endpoints (items 4, 6, 7 from the post-0.10 audit) ──
    # These three close the read-side data gaps the harness was rendering as
    # demo content. They are pure DB reads with reasonable pagination caps —
    # no writes (with one exception, see /admin/feature-flags below).
    #
    # The trust + regression endpoints unblock the Trust & Gates side panel
    # and the Fleet Dashboard regression queue. The /decisions endpoint reuses
    # the `review_outcomes` table (each row IS a decision: APPROVE / REQUEST_CHANGES
    # by a specific reviewer at a specific time) so we don't need a new table.

    # ``_normalize_ts`` / ``_normalize_row`` live in ``orchestration_engine.timestamps``
    # (imported at the top of this function). The closure that previously held
    # both helpers was hoisted to a module so ``db.py`` could share the same
    # UTC-tagging logic (#876).

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
        return JSONResponse({
            "items": items,
            "total": int(total),
            "limit": limit,
            "offset": offset,
        })

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
        return JSONResponse({
            "items": [],
            "total": 0,
            "scan_status": "no_scanner_yet",
            "next_scan_at": None,
        })

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
        return JSONResponse({
            "items": items,
            "total": int(total),
            "limit": limit,
            "offset": offset,
        })

    # ── Admin config defaults + helpers ─────────────────────────────────────
    # Helpers (_ADMIN_DEFAULTS, _strict_coerce_bool, _coerce_admin_doc,
    # _merge_feature_flags_with_passthrough) are now at module scope above
    # `create_api_app` so they can be tested directly. See the module-scope
    # docstrings for invariants.
    #
    # Concurrency model (no asyncio.Lock — round-3 simplification):
    # - PUT /admin/feature-flags has zero `await` points inside its
    #   read-modify-write critical section, so FastAPI's single-event-loop
    #   scheduling already serialises it.
    # - `os.replace()` is atomic on POSIX, so the on-disk file is always
    #   well-formed (last-writer-wins for cross-process writers).

    @app.get("/api/v1/admin/state")
    async def get_admin_state() -> JSONResponse:
        """Aggregate admin-console read state.

        Returns the operator-set values (or canonical defaults) regardless
        of how malformed the on-disk JSON is. Defensive against:
        - missing file
        - unparseable JSON
        - top-level value that isn't a dict (e.g. a string, list, number)
        - nested ``feature_flags`` / ``modes`` keys that aren't dicts
        - flag values that aren't bools (coerced via `_strict_coerce_bool`)
        - unknown extra keys (preserved under ``"extra"``)
        """
        import json as _json
        from .. import feature_flags as _ff
        admin_path = _ff._admin_json_path()  # honours ORCH_ADMIN_PATH (#840)
        raw_loaded: Any = None
        source = "default"
        if admin_path.exists():
            try:
                raw_loaded = _json.loads(admin_path.read_text())
                source = "file"
            except (OSError, ValueError):
                raw_loaded = None
        merged = _coerce_admin_doc(raw_loaded)
        # Preserve any unknown top-level keys for forward-compat (the next
        # release may add new admin settings; downgraded engines shouldn't
        # silently drop them on read).
        extra: Dict[str, Any] = {}
        if isinstance(raw_loaded, dict):
            extra = {
                k: v for k, v in raw_loaded.items()
                if k not in {"autonomy_level", "feature_flags", "modes"}
            }
        return JSONResponse({
            **merged,
            "extra": extra,
            "source": source,
            "path": str(admin_path),
        })

    @app.put("/api/v1/admin/feature-flags")
    async def update_feature_flags(request: Request) -> JSONResponse:
        """Persist a feature-flag patch to `admin.json` with atomic write.

        Body: ``{"phase0_hard_gate": true, ...}`` — any subset of the known
        flags. Strict bool coercion: accepts Python/JSON bools, 0/1, and
        the strings ``"true"``/``"false"`` (case-insensitive); anything
        else is rejected with 400.

        Concurrency: the handler body has zero ``await`` points inside its
        read-modify-write critical section, so FastAPI's single-event-loop
        scheduling already serialises it (a coroutine without ``await``
        cannot be preempted by asyncio). For the cross-process case,
        ``os.replace()`` atomicity gives last-writer-wins on the on-disk
        file — acceptable for operator-preferred admin flags.

        Atomicity: tempfile + ``os.replace`` ensures the on-disk file is
        never half-written. Tempfile cleanup uses ``Path.unlink(missing_ok=True)``
        inside a guarded ``finally`` so the original exception is preserved
        even if cleanup itself fails.

        Forward-compat: unknown nested keys inside ``feature_flags`` on
        disk (e.g. a flag from a future beta build) are preserved verbatim
        through ``_merge_feature_flags_with_passthrough``. Known flags are
        always normalised to ``bool``.
        """
        import json as _json
        import os as _os
        import tempfile as _tempfile

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        bad_keys = set(body.keys()) - _ADMIN_KNOWN_FLAGS
        if bad_keys:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown flag(s): {sorted(bad_keys)}. Known: {sorted(_ADMIN_KNOWN_FLAGS)}",
            )
        # Reject any value that isn't an explicit canonical boolean. The
        # strict-coerce helper returns None on unrecognised input → 400.
        patch: Dict[str, bool] = {}
        for k, v in body.items():
            coerced = _strict_coerce_bool(v)
            if coerced is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Flag {k!r} expects a boolean (or 0/1, or 'true'/'false') — "
                        f"got {type(v).__name__}: {v!r}"
                    ),
                )
            patch[k] = coerced

        from .. import feature_flags as _ff
        admin_path = _ff._admin_json_path()  # honours ORCH_ADMIN_PATH (#840)
        admin_dir = admin_path.parent

        admin_dir.mkdir(parents=True, exist_ok=True)
        current: Dict[str, Any] = {}
        if admin_path.exists():
            try:
                loaded = _json.loads(admin_path.read_text())
                if isinstance(loaded, dict):
                    current = loaded
            except (OSError, ValueError):
                current = {}
        # Canonicalise the merged feature_flags BEFORE writing so disk and
        # response always agree (round-3 fix), while preserving unknown
        # nested keys for forward-compat (round-4 fix). The previous
        # canonicalise-via-_coerce_admin_doc silently dropped any flag a
        # forward-compat operator (or beta build) had set on disk that
        # wasn't in _ADMIN_KNOWN_FLAGS.
        existing_flags = current.get("feature_flags")
        disk_flags: Dict[str, Any] = dict(existing_flags) if isinstance(existing_flags, dict) else {}
        before_canonical: Dict[str, Any] = {
            k: disk_flags.get(k, _ADMIN_DEFAULTS["feature_flags"][k])
            for k in _ADMIN_KNOWN_FLAGS
        }
        disk_flags.update(patch)
        merged_flags = _merge_feature_flags_with_passthrough(disk_flags)
        current["feature_flags"] = merged_flags
        # The response only exposes known flags (the AdminState TS contract);
        # unknown ones live on disk for the future engine to discover.
        canonical_flags = {k: merged_flags[k] for k in _ADMIN_KNOWN_FLAGS}

        # Atomic write: tempfile in the same directory + os.replace.
        # No asyncio.Lock — see _ADMIN_DEFAULTS / concurrency-model comment
        # above for why this handler is already serialised by the event loop.
        fd, tmp = _tempfile.mkstemp(prefix="admin.", suffix=".tmp", dir=str(admin_dir))
        try:
            with _os.fdopen(fd, "w") as fh:
                _json.dump(current, fh, indent=2)
            _os.replace(tmp, admin_path)
            tmp = None  # ownership transferred to the destination
        finally:
            # Clean up the tempfile if the replace failed. Path.unlink
            # with missing_ok=True avoids the TOCTOU window of the prior
            # `if os.path.exists(): os.unlink()` pattern and won't mask
            # the original exception with its own.
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
        # Append-only audit log (#838) — record only the keys that
        # ACTUALLY changed value, so audit rows are scannable.
        changed_keys = sorted(
            k for k, v in canonical_flags.items()
            if before_canonical.get(k) != v
        )
        if changed_keys:
            try:
                db = Database(Path(effective_db_path))
                db.append_admin_audit(
                    action="update_feature_flags",
                    target=",".join(changed_keys),
                    before={k: before_canonical[k] for k in changed_keys},
                    after={k: canonical_flags[k] for k in changed_keys},
                )
            except Exception as _exc:  # noqa: BLE001 — audit log is best-effort
                logger.warning("admin audit-log append failed: %s", _exc)
        return JSONResponse({
            "feature_flags": canonical_flags,
            "path": str(admin_path),
        })

    @app.get("/api/v1/admin/audit-log")
    async def get_admin_audit_log(limit: int = 100, offset: int = 0) -> JSONResponse:
        """Return the admin-state audit log (#838).

        Append-only record of every mutation made via the admin API. Each
        row carries the before/after JSON, the action verb, the target
        surface, and the OS pid of the FastAPI worker that served the
        request (best-effort attribution — the engine has no per-user
        auth today).

        Query params:
            limit (int): Max rows to return (default 100, max 1000).
            offset (int): Skip this many newest rows (default 0).

        Response::

            {
                "rows": [
                    {
                        "id": 42,
                        "action": "update_feature_flags",
                        "target": "phase0_hard_gate,dialogue_phase",
                        "before": {...},
                        "after": {...},
                        "source_pid": 12345,
                        "created_at": "2026-05-25 18:31:12"
                    },
                    ...
                ],
                "limit": 100,
                "offset": 0
            }
        """
        if limit < 1 or limit > 1000:
            raise HTTPException(
                status_code=400,
                detail="limit must be between 1 and 1000",
            )
        if offset < 0:
            raise HTTPException(
                status_code=400,
                detail="offset must be >= 0",
            )
        db = Database(Path(effective_db_path))
        rows = db.list_admin_audit(limit=limit, offset=offset)
        return JSONResponse({
            "rows": rows,
            "limit": limit,
            "offset": offset,
        })

    # ── SSE connection limits (#841) ─────────────────────────────────────
    # The limiter lives at module scope (see SseConnectionLimiter below)
    # so it's directly importable from tests and shared across requests
    # served by the same process. Limits are read from env vars on every
    # admit, so operators can re-tune live without restarting.
    _sse_limiter = _SSE_LIMITER  # alias for the closure-using stream handler

    @app.get("/api/v1/sse/metrics")
    async def get_sse_metrics() -> JSONResponse:
        """Return current SSE connection counts and limits (#841).

        Surfaced by the harness Admin Console + by ops dashboards. The
        ``active_total`` and ``active_per_ip`` snapshots are read under
        the lock; the limits are re-read from env vars on every call
        (operator may have re-tuned live).
        """
        return JSONResponse(_sse_limiter.metrics())

    @app.get("/api/v1/runs/{run_id}/stream")
    async def stream_run(run_id: str, request: Request) -> EventSourceResponse:
        """Stream live phase-transition events for a pipeline run via SSE.

        Connects to the ``pipeline_run_events`` table and emits fine-grained
        events as the daemon writes them.  The stream ends automatically once
        the run reaches a terminal state (``success``, ``failed``,
        ``cancelled``, ``crashed``, ``scoring_failed``, ``pending_review``,
        ``rejected``) **and** all buffered events have been delivered.

        **Event types** (``event`` field):

        * ``phase_started`` — daemon has begun executing the phase.
        * ``phase_completed`` — phase finished; ``data`` includes
          ``tokens_consumed``, ``cost_usd``, and ``state``.
        * ``status_changed`` — run-level status transition (emitted once on
          terminal state: ``success``, ``failed``, ``cancelled``, ``crashed``,
          ``scoring_failed``, ``pending_review``, ``rejected``).
        * ``tool_call_started`` / ``tool_call_completed`` — reserved schema
          for tool-aware executors to emit per-tool-call progress. Schema:
          ``{run_id, phase_id, tool_name, tool_args | result_summary,
          iteration}``. NOT emitted by any executor today and NOT rendered
          by the harness frontend; the handler will forward them transparently
          when both sides land in a follow-up PR.
        * ``error`` — run not found or unexpected failure.

        **Data** is a JSON object with at minimum ``run_id`` and ``phase_id``
        (``null`` for run-level events).

        **Polling interval:** 1 second.  Clients that disconnect trigger a
        clean server-side shutdown of the generator.

        Raises 404 when the run ID is not found (returned as an SSE
        ``error`` event rather than an HTTP error so the EventSource protocol
        stays clean).
        """
        _TERMINAL_STATES = TERMINAL_STATUSES
        _POLL_INTERVAL = 1.0  # seconds between DB polls

        # ── SSE connection limits (#841) ─────────────────────────────
        # Check limits BEFORE opening the stream. On hit, return 429
        # with Retry-After so clients back off instead of reconnecting
        # tight-loop. Successful admit MUST be paired with a release in
        # the generator's finally block.
        client_ip = (request.client.host if request.client else "unknown")
        admit_err = _sse_limiter.admit(client_ip)
        if admit_err is not None:
            raise HTTPException(
                status_code=429,
                detail=admit_err,
                headers={"Retry-After": "30"},
            )

        db = Database(Path(effective_db_path))

        # Validate run exists before opening the stream.
        run = db.get_pipeline_run(run_id)
        if run is None:
            # Release the slot we just admitted — the not-found stream
            # is a fast-fail and shouldn't count against the cap.
            _sse_limiter.release(client_ip)
            async def _not_found():
                yield {
                    "event": "error",
                    "data": json.dumps({"error": f"Run '{run_id}' not found"}),
                }
            return EventSourceResponse(_not_found())

        async def _event_generator():
            last_event_id = 0
            emitted_terminal = False

            try:
                while True:
                    # Respect client disconnect
                    if await request.is_disconnected():
                        break

                    # Fetch new events since the last one delivered
                    events = db.list_pipeline_run_events(
                        run_id, after_id=last_event_id
                    )
                    for evt in events:
                        last_event_id = evt["id"]
                        # Parse metadata JSON for enriched fields (#747)
                        meta = {}
                        raw_meta = evt.get("metadata_json")
                        if raw_meta:
                            try:
                                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
                            except (json.JSONDecodeError, TypeError):
                                pass

                        payload = {
                            "run_id": run_id,
                            "phase_id": evt.get("phase_id"),
                            "tokens_consumed": evt.get("tokens_consumed"),
                            "cost_usd": evt.get("cost_usd"),
                            "state": evt.get("state"),
                            "created_at": _normalize_ts(evt.get("created_at")),
                            # Enriched fields (#747)
                            "model_tier": meta.get("model_tier"),
                            "model_used": meta.get("model_used"),
                            "phase_name": meta.get("phase_name"),
                            "thinking_level": meta.get("thinking_level"),
                            "elapsed_seconds": meta.get("elapsed_seconds"),
                            "tokens_in": meta.get("tokens_in"),
                            "tokens_out": meta.get("tokens_out"),
                            "word_count": meta.get("word_count"),
                        }
                        yield {
                            "event": evt["event_type"],
                            "data": json.dumps(payload),
                            "id": str(evt["id"]),
                        }

                    # Check current run status for terminal state
                    current_run = db.get_pipeline_run(run_id)
                    if current_run and current_run.get("status") in _TERMINAL_STATES:
                        if not emitted_terminal:
                            emitted_terminal = True
                            terminal_payload = {
                                "run_id": run_id,
                                "phase_id": None,
                                "status": current_run["status"],
                                "completed_at": current_run.get("completed_at"),
                                "error_message": current_run.get("error_message"),
                            }
                            yield {
                                "event": "status_changed",
                                "data": json.dumps(terminal_payload),
                            }
                        # Drain any remaining events before closing
                        if not events:
                            break

                    await asyncio.sleep(_POLL_INTERVAL)
            finally:
                # Always release the SSE slot — whether the loop exited
                # via client disconnect, terminal-state break, or any
                # other path (#841).
                _sse_limiter.release(client_ip)

        return EventSourceResponse(_event_generator())

    # ------------------------------------------------------------------
    # Pydantic models — Review Queue (Issue #331.4)
    # ------------------------------------------------------------------

    class ReviewResponse(BaseModel):
        """Serialised pending-review run record (pipeline run + routing decision)."""

        run_id: str
        template_id: str
        status: str
        created_at: Optional[str]
        completed_at: Optional[str]
        review_reason: Optional[str]
        reviewed_at: Optional[str]
        reviewed_by: Optional[str]
        confidence_score: Optional[float]
        tier_name: Optional[str]
        action: Optional[str]
        justification: Optional[str]

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

    # ------------------------------------------------------------------
    # Review Queue endpoints (Issue #331.4)
    # ------------------------------------------------------------------

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

    @app.delete("/api/v1/runs/{run_id}", status_code=200)
    async def cancel_run(run_id: str) -> JSONResponse:
        """Cancel a running or pending pipeline run.

        Sends SIGTERM to the daemon process (if any) and updates the run
        status to ``cancelled`` in the DB.

        Returns:
            200 with ``{"run_id": ..., "cancelled": true}`` on success.
            404 when the run ID is not found.
            409 when the run is already in a terminal state.
        """
        db = Database(Path(effective_db_path))
        run = db.get_pipeline_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        terminal_states = TERMINAL_STATUSES
        if run.get("status") in terminal_states:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is already in terminal state '{run['status']}' "
                    "and cannot be cancelled"
                ),
            )

        cancelled = db.cancel_pipeline_run(run_id)
        return JSONResponse({"run_id": run_id, "cancelled": cancelled})

    # ------------------------------------------------------------------
    # Cost API endpoints (Issue #5.2.3)
    # ------------------------------------------------------------------

    _VALID_GROUP_BY = {"day", "template", "model"}
    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    @app.get("/api/v1/costs/summary")
    async def cost_summary(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
        limit: int = 20,
        offset: int = 0,
    ) -> JSONResponse:
        """Return aggregated cost data grouped by day, template, or model.

        Query parameters:
            start_date: Optional ISO date ``YYYY-MM-DD`` (inclusive lower bound).
            end_date:   Optional ISO date ``YYYY-MM-DD`` (inclusive upper bound).
            group_by:   Grouping dimension — ``day`` (default), ``template``, or
                        ``model``.
            limit:      Page size (default 20, max 100).
            offset:     Number of items to skip (default 0).

        Returns:
            200 with ``{"items": [...], "total": int, "limit": int, "offset": int}``.
            400 when ``group_by`` is invalid or date strings are malformed.
        """
        if group_by not in _VALID_GROUP_BY:
            raise HTTPException(
                status_code=400,
                detail=f"group_by must be one of {sorted(_VALID_GROUP_BY)}, got {group_by!r}",
            )
        if start_date is not None and not _DATE_RE.match(start_date):
            raise HTTPException(
                status_code=400,
                detail=f"start_date must be YYYY-MM-DD, got {start_date!r}",
            )
        if end_date is not None and not _DATE_RE.match(end_date):
            raise HTTPException(
                status_code=400,
                detail=f"end_date must be YYYY-MM-DD, got {end_date!r}",
            )
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        db = Database(Path(effective_db_path))
        items = db.get_cost_summary(start_date, end_date, group_by, limit, offset)
        total = db.count_cost_summary(start_date, end_date, group_by)
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.get("/api/v1/costs/run/{run_id}")
    async def cost_run_breakdown(run_id: str) -> JSONResponse:
        """Return per-phase cost records for a specific pipeline run.

        Path parameters:
            run_id: The pipeline run identifier.

        Returns:
            200 with ``{"run_id": str, "items": [...], "total_cost": float,
            "total_input_tokens": int, "total_output_tokens": int}``.
            404 when no cost records exist for the given ``run_id``.
        """
        db = Database(Path(effective_db_path))
        items = db.get_run_costs(run_id)
        if not items:
            raise HTTPException(
                status_code=404,
                detail=f"No cost records found for run '{run_id}'",
            )
        total_cost = sum(row.get("cost_usd", 0.0) or 0.0 for row in items)
        total_input = sum(row.get("input_tokens", 0) or 0 for row in items)
        total_output = sum(row.get("output_tokens", 0) or 0 for row in items)
        return JSONResponse(
            {
                "run_id": run_id,
                "items": items,
                "total_cost": total_cost,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
            }
        )

    # ------------------------------------------------------------------
    # Trust profile API endpoints (Issue #4.2.4)
    # ------------------------------------------------------------------

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
        items = all_profiles[offset: offset + limit]
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

        from ..trust import TrustCalibrator

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
            "repo":                   profile["repo"],
            "template_id":            profile["template_id"],
            "task_type":              profile["task_type"],
            "auto_merge_threshold":   new_threshold,
            "human_review_threshold": float(profile["human_review_threshold"]),
            "trust_score":            new_score,
            "total_runs":             int(profile["total_runs"]),
            "successful_merges":      successful_merges,
            "regressions":            int(profile["regressions"]),
            "reverted_prs":           int(profile["reverted_prs"]),
            "last_run_at":            profile.get("last_run_at"),
            "created_at":             profile["created_at"],
            "updated_at":             now_iso,
        }
        db.upsert_trust_profile(updated)

        # Build audit note (include reviewer if supplied)
        audit_reason = "manual_override"
        if body.reviewed_by:
            audit_reason = f"manual_override:{body.reviewed_by}"

        db.insert_trust_adjustment({
            "profile_id":   profile_id,
            "delta":        delta,
            "reason":       audit_reason,
            "run_id":       None,
            "score_before": old_score,
            "score_after":  new_score,
            "created_at":   now_iso,
        })

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

    # ------------------------------------------------------------------
    # Telegram HITL Callback Endpoint (Issue #429.5)
    # ------------------------------------------------------------------

    @app.post("/api/v1/telegram/callback", status_code=200)
    async def telegram_callback(request: Request) -> JSONResponse:
        """Handle Telegram Bot API webhook updates for HITL inline keyboard actions.

        Telegram delivers a ``callback_query`` update when the user taps an
        [Approve] or [Reject] button on a ``human_review`` notification.  This
        endpoint verifies the ``X-Telegram-Bot-Api-Secret-Token`` header,
        delegates to :class:`~orchestration_engine.notifications.TelegramCallbackHandler`,
        and returns an ``{"ok": true}`` response to Telegram.

        Security:
            The ``NOTIFY_TELEGRAM_WEBHOOK_SECRET`` environment variable must be
            set.  Requests without a matching secret token header are rejected
            with HTTP 403.

        Returns:
            - **200** with ``{"ok": true, "action": ..., "run_id": ...}`` on success.
            - **403** when the secret token header is missing or invalid.
            - **400** when the request body is not valid JSON or the callback
              data cannot be parsed.
        """
        # Verify the shared secret Telegram sends in the header
        webhook_secret = os.environ.get("NOTIFY_TELEGRAM_WEBHOOK_SECRET", "")
        if webhook_secret:
            token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(token_header, webhook_secret):
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or missing X-Telegram-Bot-Api-Secret-Token header",
                )
        else:
            logger.warning(
                "NOTIFY_TELEGRAM_WEBHOOK_SECRET is not set — "
                "/api/v1/telegram/callback is accepting unauthenticated requests. "
                "Set the env var to enable webhook signature verification."
            )

        try:
            body_bytes = await request.body()
            update = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in request body: {exc}",
            )

        # Resolve DB path: env var → config → persistent default
        db_path = (
            os.environ.get("NOTIFY_TELEGRAM_CALLBACK_DB_PATH", "")
            or effective_db_path
        )
        gateway_url = os.environ.get(
            "NOTIFY_OPENCLAW_GATEWAY_URL",
            os.environ.get("OPENCLAW_GATEWAY_URL", ""),
        )
        gateway_token = os.environ.get("NOTIFY_OPENCLAW_GATEWAY_TOKEN", "")
        bot_token = os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", "")

        from orchestration_engine.notifications import TelegramCallbackHandler

        handler = TelegramCallbackHandler(
            db_path=db_path,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            bot_token=bot_token,
            chat_id=chat_id,
        )

        result = handler.handle_update(update)
        if not result.get("ok"):
            # Log the error but return 200 to prevent Telegram from retrying
            logger.warning(
                "Telegram callback handler returned non-ok result: %s", result
            )
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GitHub Issues Webhook (Issue #5.1.3)
    # ------------------------------------------------------------------

    @app.post("/api/v1/github/issues", status_code=202)
    async def handle_github_issues(request: Request) -> JSONResponse:
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
        from orchestration_engine.issue_automation import (
            IssueAutomation,
            IssueClassifier,
            TemplateSelector,
            InputExtractor,
            post_github_comment,
        )
        from orchestration_engine.notifications import NotificationDispatcher

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
        from orchestration_engine.config import get_global_config
        cfg = get_global_config()
        if cfg.github_app and cfg.github_app.webhook_secret:
            sig_header = request.headers.get("X-Hub-Signature-256")
            if not _verify_github_signature(
                cfg.github_app.webhook_secret, _body_bytes, sig_header
            ):
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
            issue_labels = [
                lbl.get("name", "") for lbl in (issue.get("labels") or [])
            ]
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
        classifier = IssueClassifier()   # stub mode; replace executor via subclass/config
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
        issue_label_names = [
            lbl.get("name", "") for lbl in (issue.get("labels") or [])
        ]

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

    # ------------------------------------------------------------------
    # GitHub Issues — pipeline-ready label trigger (Issue #511)
    # ------------------------------------------------------------------

    @app.post("/api/v1/github/issues/pipeline-ready", status_code=202)
    async def handle_github_issues_pipeline_ready(request: Request) -> JSONResponse:
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
        from orchestration_engine.issue_automation import (
            generate_pipeline_input,
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
        from orchestration_engine.config import get_global_config
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
        except Exception as exc:
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
            f"🤖 **Orchemist** detected `pipeline-ready` label and launched the coding pipeline.\n\n"
            f"**Branch:** `{branch_name}`\n"
            f"**Run ID:** `{run_id}`\n\n"
            f"Progress can be tracked via `orch status {run_id}`."
        )
        post_github_comment(repo=repo, issue_number=issue_number, body=comment_body)

        return JSONResponse(
            {"status": "accepted", "run_id": run_id, "branch_name": branch_name},
            status_code=202,
        )

    # ------------------------------------------------------------------
    # Direct Issue Launch REST Endpoint (Issue #632)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Merge Gate endpoints (#743)
    # ------------------------------------------------------------------

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
        from ..git_integration import GitContext

        all_gates = GitContext.list_gates()
        if status:
            all_gates = [g for g in all_gates if g.get("status") == status]
        total = len(all_gates)
        items = all_gates[offset : offset + limit]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/gates/{run_id}")
    async def get_gate(run_id: str):
        """Get a single gate by run ID."""
        from ..git_integration import GitContext

        gate = GitContext.load_gate(run_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"No gate found for run ID '{run_id}'")
        return gate

    @app.post("/api/v1/gates/{run_id}/approve")
    async def approve_gate(run_id: str, req: GateApproveRequest = GateApproveRequest()):
        """Approve a merge gate."""
        from ..git_integration import GitContext, GitError

        gate = GitContext.load_gate(run_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"No gate found for run ID '{run_id}'")

        current_status = gate.get("status")
        if current_status != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Gate is in status '{current_status}', can only approve 'awaiting_approval' gates",
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
        from ..git_integration import GitContext, GitError

        gate = GitContext.load_gate(run_id)
        if gate is None:
            raise HTTPException(status_code=404, detail=f"No gate found for run ID '{run_id}'")

        current_status = gate.get("status")
        if current_status != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Gate is in status '{current_status}', can only reject 'awaiting_approval' gates",
            )

        try:
            updated = GitContext.update_gate_status(
                run_id, "rejected", message=req.reason or "Rejected via API"
            )
        except GitError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        return updated

    return app
