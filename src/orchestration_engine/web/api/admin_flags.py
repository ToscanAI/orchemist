"""Admin feature-flag defaults for the REST API (Issue #942, sub-issue 952a).

Module-level admin defaults extracted verbatim from ``web/api.py`` as part of
the facade-preserving decomposition of the god-module. These are pure data
constants with no dependencies; lifting them out of ``create_api_app``'s closure
keeps them directly testable (the round-4 audit driver for the original
extraction).

Re-exported by ``web/api/__init__.py`` so the historical import path
``from orchestration_engine.web.api import _ADMIN_DEFAULTS`` keeps resolving.
"""

from typing import Any, Dict

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
