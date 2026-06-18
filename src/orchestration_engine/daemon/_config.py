"""Daemon config-resolution helpers.

Config-schema default application, effective retry-cap derivation, and the
happy-path phase oracle used by the postflight completeness check.  Extracted
verbatim from :mod:`orchestration_engine.daemon` (wave a of #1034); the public
surface is re-exported by the package facade, so callers continue to import
these names from ``orchestration_engine.daemon``.
"""

# ruff: noqa: E501

import logging
from typing import Any, Dict, List

from ..routing import DEFAULT_ROUTING_CONFIG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Happy-path phase derivation for the postflight oracle (Issue #915)
# ---------------------------------------------------------------------------

# Outcome keys that advance a run along its happy path, in priority order.
# ``approve`` is preferred over ``success`` so the walk follows the real happy
# target on phases that carry BOTH (e.g. standard's ``spec_adversary`` has
# ``approve: acceptance_test`` plus a defensive ``success: spec`` loop-back used
# only when verdict extraction fails).
_HAPPY_KEYS = ("approve", "success")


def _happy_path_phase_ids(template: Any) -> List[str]:
    """Return the happy-path reachable phase IDs of *template*.

    Walks the transition graph from the entry phase (``template.phases[0].id``)
    following ONLY the happy edges — ``approve`` preferred over ``success`` per
    phase — using the merged transitions from
    :meth:`TemplateEngine._compute_effective_transitions`.  A visited-set guards
    against cycles (e.g. standard's ``spec_adversary success: spec`` back-edge),
    and dangling targets (a happy edge pointing at an unknown id) are skipped.
    The walk stops along a branch when a phase has no ``approve``/``success``
    target (terminal).

    Conditional/terminal phases reachable only via non-happy outcomes
    (``request_changes``, ``abort``, ``exhausted``, ``failed``, ``timeout``) —
    e.g. ``fix`` / ``postmortem_*`` — are therefore excluded, which is exactly
    the oracle the postflight completeness check needs (a clean run never
    executes them).  Issue #915.

    Returns:
        Ordered list of happy-path phase IDs (first-seen order).  Returns ``[]``
        for a malformed or empty template so a failure degrades to a skipped
        check rather than crashing the (advisory) postflight block.
    """
    try:
        phases = getattr(template, "phases", None)
        if not phases:
            return []
        from ..templates import TemplateEngine  # noqa: PLC0415

        eff = TemplateEngine._compute_effective_transitions(template)
        valid_ids = {p.id for p in phases}
        entry = phases[0].id

        visited: List[str] = []  # preserves first-seen order
        seen: set = set()
        stack: List[str] = [entry]
        while stack:
            pid = stack.pop()
            if pid in seen or pid not in valid_ids:
                continue  # cycle/self-loop guard + ignore dangling targets
            seen.add(pid)
            visited.append(pid)
            outcomes = eff.get(pid, {})
            # Follow ONE happy successor: prefer 'approve', else 'success'.
            for key in _HAPPY_KEYS:
                tgt = outcomes.get(key)
                if tgt is not None:
                    stack.append(tgt)
                    break
        return visited
    except Exception as exc:  # pragma: no cover - defensive  # noqa: BLE001
        logger.warning("_happy_path_phase_ids: failed to derive oracle (non-fatal): %s", exc)
        return []


def apply_config_schema_defaults(config: Dict[str, Any], config_schema: Any) -> None:
    """Fill missing keys in *config* from *config_schema* property defaults.

    Mutates ``config`` in place. For every key declared under
    ``config_schema.properties.<key>.default``, the key is added to
    ``config`` if (and only if) it is not already present.

    Why this exists (#835):
        Prompt templates render via ``str.format(config=_SafeDict(config), …)``.
        Without applying schema defaults, an existing consumer who has not
        migrated their config dict to include a newly-added optional field
        would see the literal string ``<MISSING:fieldname>`` substituted into
        the rendered prompt (``_SafeDict.__missing__`` fallback) — a silent
        backward-compat regression. Filling defaults here keeps non-migrated
        consumers running cleanly when the YAML adds new optional fields.

    Safety:
        - Existing keys in ``config`` are never overwritten.
        - Properties without a ``default`` are not touched.
        - Non-dict ``config_schema``, non-dict ``properties``, and missing
          ``properties`` are all no-ops (defensive — schemas are operator-
          editable YAML).
    """
    if not isinstance(config_schema, dict):
        return
    props = config_schema.get("properties")
    if not isinstance(props, dict):
        return
    for key, spec in props.items():
        if isinstance(spec, dict) and "default" in spec and key not in config:
            config[key] = spec["default"]


def _get_effective_max_retries(template: Any) -> int:
    """Compute the effective max_retries cap from the template's routing config.

    Reads all routing tiers with ``strategy == 'retry'`` and applies the
    lowest non-zero cap across all such tiers. Returns:

    - ``0`` if all retry tiers explicitly set ``max_retries=0`` (no retries).
    - ``1`` if no retry tiers are defined at all (safe default).
    - The lowest non-zero cap among all retry tiers otherwise.

    Falls back to ``DEFAULT_ROUTING_CONFIG`` when the template has no
    ``routing_config`` attribute or it is ``None``.

    Args:
        template: A loaded pipeline template object.

    Returns:
        Integer effective max_retries cap (>= 0).
    """
    _DEFAULT = 1  # noqa: N806
    routing_config = getattr(template, "routing_config", None)
    if routing_config is None:
        routing_config = DEFAULT_ROUTING_CONFIG
    retry_tiers = [t for t in routing_config.tiers if t.strategy == "retry"]
    if not retry_tiers:
        return _DEFAULT
    caps = [t.max_retries for t in retry_tiers]
    nonzero = [c for c in caps if c > 0]
    if not nonzero:
        return 0  # All tiers explicitly cap retries at 0
    return min(nonzero)
