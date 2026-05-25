"""Runtime feature-flag reader (#840).

Reads the admin state from ``~/.orchestration-engine/admin.json`` and exposes
a tiny ``is_enabled(flag_name)`` API for sequencer/daemon code paths.

Design:
    - Single source of truth: ``admin.json`` on disk (written by the admin UI
      via ``PUT /api/v1/admin/feature-flags`` in ``web/api.py``).
    - Cached for ``_TTL_SECONDS`` (30) to avoid re-reading on every phase
      transition. The first ``is_enabled`` call after the TTL expires re-reads.
    - Defensive against missing/malformed files: returns the per-flag default
      from ``_DEFAULTS`` rather than raising.
    - Tests can call :func:`reset_cache()` to force the next read to hit disk.

Flags currently consumed at runtime:
    - ``phase0_hard_gate`` (default False) — when True, exhaustion of the
      ``existing_symbols_inventory`` phase HALTS the pipeline instead of
      routing via the YAML's ``transitions.exhausted`` (graceful-degradation
      fallback to SPEC). Consumed in :mod:`sequencer` MAX_ITERATIONS handling.
    - ``dialogue_phase`` (default False) — when False, any phase with
      ``type: dialogue`` is SKIPPED with a warning instead of dispatched.
      Consumed in :meth:`sequencer.StateMachineSequencer._execute_dialogue_phase`.

Flags persisted but NOT yet runtime-consumed (no-ops; documented in the
admin doc surface as forward-compat):
    - ``extend_verdict`` — currently consumed only via prompt-level YAML, not
      a runtime gate.
    - ``cross_repo`` — reserved; no implementation yet.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Defaults — must match ``_ADMIN_DEFAULTS["feature_flags"]`` in
# ``web/api.py``. Drift between the two is caught by
# ``tests/test_feature_flags_runtime.py::TestDefaultsMatchAdminApi``.
_DEFAULTS: Dict[str, bool] = {
    "phase0_hard_gate": False,
    "extend_verdict": True,
    "dialogue_phase": False,
    "cross_repo": False,
}

# Canonical id of the Phase 0 phase. Shared between this module (which
# documents the flag's scope), the sequencer (which gates its hard-gate
# override on this id), and the web API artifact endpoint (which serves
# the inventory artifact from runs of phases bearing this id). When a
# downstream template uses a different id (e.g. skills v4.2's
# abbreviated "existing_symbols"), it falls OUTSIDE the hard-gate
# protection — by design today, since the gate's contract is anchored
# to this canonical name.
PHASE_0_ID: str = "existing_symbols_inventory"

_TTL_SECONDS: float = 30.0

# Cache: (loaded_dict, monotonic_load_timestamp). Populated lazily on the
# first ``is_enabled`` call. Protected by ``_LOCK`` for thread safety —
# the sequencer is single-threaded today but the web API serves concurrent
# requests via FastAPI workers and may also call ``is_enabled`` in future.
_CACHE: Dict[str, Any] = {"flags": None, "loaded_at": 0.0}
_LOCK = threading.Lock()


def _admin_json_path() -> Path:
    """Path to the admin JSON file. Honoured by tests via ``ORCH_ADMIN_PATH``
    env var (mirrors the web API's lookup pattern)."""
    override = os.environ.get("ORCH_ADMIN_PATH")
    if override:
        return Path(override)
    return Path.home() / ".orchestration-engine" / "admin.json"


def _read_flags_from_disk() -> Dict[str, bool]:
    """Read flag values from admin.json, applying per-key defaults for
    anything missing or malformed.

    Returns a fresh dict every call — callers may mutate it without
    affecting the cache.
    """
    path = _admin_json_path()
    flags = dict(_DEFAULTS)
    try:
        if not path.is_file():
            return flags
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "feature_flags: failed to read %s — using defaults. Cause: %s",
            path, exc,
        )
        return flags
    if not isinstance(loaded, dict):
        return flags
    ff = loaded.get("feature_flags")
    if not isinstance(ff, dict):
        return flags
    for key in _DEFAULTS:
        if key in ff and isinstance(ff[key], bool):
            flags[key] = ff[key]
    return flags


def reset_cache() -> None:
    """Force the next :func:`is_enabled` call to re-read from disk.

    Used by tests; production callers should rely on the 30s TTL.
    """
    with _LOCK:
        _CACHE["flags"] = None
        _CACHE["loaded_at"] = 0.0


def get_flags(*, fresh: bool = False) -> Dict[str, bool]:
    """Return a copy of the current flag dict.

    Args:
        fresh: When True, force a disk read regardless of cache age. When
            False (default), honour the 30s TTL.

    Returns:
        Dict with the four canonical flag keys. Never None; never missing
        a key (defaults applied for anything absent in admin.json).
    """
    now = time.monotonic()
    with _LOCK:
        if (
            not fresh
            and _CACHE["flags"] is not None
            and (now - _CACHE["loaded_at"]) < _TTL_SECONDS
        ):
            return dict(_CACHE["flags"])
        flags = _read_flags_from_disk()
        _CACHE["flags"] = flags
        _CACHE["loaded_at"] = now
        return dict(flags)


def is_enabled(flag_name: str) -> bool:
    """Return True if *flag_name* is enabled in the current admin state.

    Unknown flag names are logged at WARNING level and return False —
    safer than raising in the middle of a pipeline run.
    """
    if flag_name not in _DEFAULTS:
        logger.warning(
            "feature_flags.is_enabled(%r): unknown flag — returning False. "
            "Known flags: %s", flag_name, sorted(_DEFAULTS.keys())
        )
        return False
    return get_flags()[flag_name]
