"""FastAPI REST API for the Orchestration Engine (Issue #257).

Provides a versioned JSON REST API backed by the same daemon-based
async execution infrastructure used by ``orch launch``.  This is
separate from ``web/app.py`` (browser UI) — it targets programmatic
consumers such as CI/CD pipelines, OpenClaw, and external scripts.

All dependencies (fastapi, uvicorn) are optional extras — import this
module only after confirming they are installed.

Package layout (Issue #942, sub-issue 952a — facade-preserving decomposition):
this ``__init__`` is the FACADE. The module was converted from a single
``web/api.py`` file into a ``web/api/`` package; ``create_api_app`` (with all
its inner route closures unchanged) lives in :mod:`._app`, and the closure-free
module-level members were extracted into sibling modules
(:mod:`.schemas`, :mod:`.security`, :mod:`.admin_flags`, :mod:`.sse`,
:mod:`.deps`). This facade re-exports the exact public-to-the-test-suite surface
so every historical ``from orchestration_engine.web.api import ...`` keeps
resolving byte-identically. Route extraction (APIRouters) is deferred to 952b-d.
"""

# ``subprocess`` is re-exported at the facade level because the test-suite
# patches ``orchestration_engine.web.api.subprocess.Popen`` (test_rest_api.py).
# It refers to the same shared ``subprocess`` module ``_app`` imports, so
# patching ``.Popen`` on it affects the daemon-launch call site in ``_app``.
import subprocess  # noqa: F401

from ._app import create_api_app  # noqa: F401
from .admin_flags import _ADMIN_DEFAULTS, _ADMIN_KNOWN_FLAGS  # noqa: F401
from .deps import _get_persistent_db_path  # noqa: F401
from .schemas import (  # noqa: F401
    _apply_input_map,
    _coerce_admin_doc,
    _merge_feature_flags_with_passthrough,
    _strict_coerce_bool,
)
from .security import (  # noqa: F401
    SlidingWindowRateLimiter,
    _check_rate_limit,
    _verify_github_signature,
)
from .sse import _SSE_LIMITER, _SseConnectionLimiter  # noqa: F401
