"""Request/response transform helpers for the REST API (Issue #942, sub-issue 952a).

Closure-free, module-level helpers extracted verbatim from ``web/api.py`` as
part of the facade-preserving decomposition of the god-module. These are pure
transforms with no FastAPI/closure dependency.

NOTE (952a scope): the Pydantic request/response *models* (``LaunchRequest``,
``RunResponse``, ``TriggerCreateRequest``, ...) are defined *inside*
``create_api_app`` — they are part of the route-factory closure and moving them
is behaviour-touching, so they remain in ``_app.py`` and are deferred to the
router-extraction sub-issues (952b-d). Only the closure-free helpers move here.

Re-exported by ``web/api/__init__.py`` so the historical import path
``from orchestration_engine.web.api import _strict_coerce_bool`` keeps resolving.
"""

from typing import Any, Dict, Optional

from .admin_flags import _ADMIN_DEFAULTS, _ADMIN_KNOWN_FLAGS, _ADMIN_KNOWN_MODES


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
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
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
