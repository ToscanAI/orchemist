"""Phase sequencer — executes pipeline phases in order, passing outputs forward.

Parallel execution support (Issue #102)
----------------------------------------
Independent phases within the same topological wave may now execute
concurrently via :class:`~concurrent.futures.ThreadPoolExecutor`.  Behaviour
is controlled by three fields on :class:`~.templates.PipelineTemplate`:

* ``parallel``     — enable/disable concurrent wave execution (default: ``True``)
* ``max_parallel`` — cap concurrent phases per wave (default: ``0`` = unlimited)
* ``fail_fast``    — abort remaining phases when one fails (default: ``True``)

All shared state (``phase_outputs``, progress callbacks) is protected by
reentrant locks so wave-level concurrency is safe.

Package layout (EPIC #942, sub-issue 953c — facade-preserving decomposition)
----------------------------------------------------------------------------
This ``__init__`` is the FACADE. The module was converted from a single
``sequencer.py`` file into a ``sequencer/`` package. As of sub-issue 953c the
two stateful classes were split one-class-per-file and moved out VERBATIM:
:class:`PhaseSequencer` (with the #978 termination guard, #988
``parallel_group`` fan-out, and #986 lifecycle-hook seam) into :mod:`._phase`,
and :class:`StateMachineSequencer` into :mod:`._state_machine`. No logic
changed. The *pure* module-level members had already moved out earlier: the
constants into :mod:`._consts`, the free helper functions into
:mod:`._helpers`, and the prompt-rendering proxy classes into :mod:`._proxies`.
Everything — the two classes, the constants/helpers/proxies, and the stdlib /
intra-package names the old inline bodies imported — is re-exported below so
the module's public AND private surface (``dir()``) is byte-identical to the
pre-split module, and so every historical
``from orchestration_engine.sequencer import ...`` keeps resolving
byte-identically.
"""

# Stdlib / intra-package imports retained as facade re-exports (EPIC #942 953c).
# These were referenced by the inline class bodies that moved to ._phase /
# ._state_machine. The facade no longer uses them directly, but they MUST stay
# importable from ``orchestration_engine.sequencer`` so the module ``dir()``
# surface is identical to the pre-split module (surface-diff guard).
import logging  # noqa: F401
import re  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import uuid  # noqa: F401
from collections import defaultdict  # noqa: F401
from concurrent.futures import Future, ThreadPoolExecutor, as_completed  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from ..file_guard import compute_directory_hash, compute_hash  # noqa: F401
from ..output_parser import extract_and_write, parse_output  # noqa: F401
from ..review_parser import ReviewOutcome, parse_review_output  # noqa: F401
from ..schemas import (  # noqa: F401
    Priority,
    TaskError,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskType,
)
from ..templates import PhaseDefinition, PipelineTemplate, TemplateEngine  # noqa: F401
from ..timestamps import now_utc  # noqa: F401
from ..transitions import (  # noqa: F401
    _VERDICT_KEYWORDS,
    PhaseOutcome,
    determine_outcome,
    extract_verdict,
)

# --- Facade re-exports (EPIC #942 953a/953b) --------------------------------
# Pure module-level members extracted into sibling sub-modules. Imported here
# so ``from orchestration_engine.sequencer import <name>`` keeps resolving for
# every historical importer / test, and so the module ``dir()`` surface is
# byte-identical to the pre-split module (surface-diff guard — e.g.
# ``_FINDING_TAG_RE`` is referenced only internally yet must stay importable).
from ._consts import _DEFAULT_SUPERVISOR_PROMPT, _TERMINAL_PUNCTUATION  # noqa: F401
from ._helpers import (  # noqa: F401
    _FINDING_TAG_RE,
    _analyze_round_findings,
    _are_findings_similar,
    _extract_findings_from_text,
    _extract_phase_text,
    _format_failure_context,
    _is_within_dir,
    _load_skill,
    _parse_supervisor_response,
    _resolve_model_tier,
    _resolve_task_type,
    _safe_call_hook,
    _sanitize_error_for_prompt,
    _wrap_callable_runner,
)

# --- Class re-exports (EPIC #942 953c) --------------------------------------
# The two stateful classes were split one-class-per-file (953c) and moved out
# VERBATIM. They are imported here so
# ``from orchestration_engine.sequencer import PhaseSequencer`` and
# ``... import StateMachineSequencer`` keep resolving byte-identically.
from ._phase import PhaseSequencer  # noqa: F401
from ._proxies import (  # noqa: F401
    _PhaseOutput,
    _PreviousOutputInlineProxy,
    _PreviousOutputProxy,
    _SafeDict,
)
from ._state_machine import StateMachineSequencer  # noqa: F401

logger = logging.getLogger(__name__)
