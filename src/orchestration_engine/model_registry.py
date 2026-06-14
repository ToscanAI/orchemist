"""model_registry.py — single canonical source for Claude model identities.

This is the one place that maps each :class:`~orchestration_engine.schemas.ModelTier`
to its canonical Anthropic model id, in both the **bare** form (emitted by
``AnthropicExecutor`` and the adaptive-retry escalation ladder) and the
``anthropic/``-prefixed form (emitted by the OpenClaw / OpenRouter executors and
``config.tier_mappings``).

Before this module, the same tier→id mapping was duplicated across ~15 sites,
several of which carried stale or dotted ids (``…-20241022``,
``anthropic/claude-haiku-4.5``, ``claude-sonnet-4-20250514``). Consolidating
here makes the canonical set authoritative and purges every stale emission site.

Design constraints:

* **Leaf module.** It imports **only** ``ModelTier`` from ``.schemas`` (which
  itself imports nothing from the engine), so ``model_registry → schemas`` is
  acyclic and the registry is importable at every depth already in use —
  top-level (``from .model_registry import …``) and ``executors/`` submodules
  (``from ..model_registry import …``).
* The ``ModelTier`` enum *values* (``"haiku-4-5"``, ``"sonnet-4"``,
  ``"opus-4-6"``) are tier **keys**, not model ids; they are unchanged. The
  canonical model ids live here.
* ``ModelTier.OPUS`` resolves to **Opus 4.8** (``claude-opus-4-8``) — the
  current canonical Anthropic Opus, maintainer-authorized as a deliberate model
  upgrade (it is priced identically to 4.6/4.7 at $5/$25). Opus 4.6 / 4.7 remain
  priced literals in ``pricing.yaml`` so any explicitly-pinned opus id still
  prices correctly; they are simply no longer the tier's default emission.
"""

from __future__ import annotations

from typing import Dict, Optional, Union

from .schemas import ModelTier

# ---------------------------------------------------------------------------
# Canonical id source of truth
# ---------------------------------------------------------------------------

#: Canonical **bare** model ids (no ``anthropic/`` prefix). Emitted by
#: ``AnthropicExecutor._MODEL_MAP`` and the ``adaptive_retry`` escalation ladder.
CANONICAL_BARE: Dict[ModelTier, str] = {
    ModelTier.HAIKU: "claude-haiku-4-5-20251001",
    ModelTier.SONNET: "claude-sonnet-4-6",
    ModelTier.OPUS: "claude-opus-4-8",  # OPUS tier → newest Opus (4.8), maintainer-authorized
}

#: Canonical ``anthropic/``-prefixed model ids. Emitted by the OpenClaw /
#: OpenRouter executors and ``config.tier_mappings``.
CANONICAL_PREFIXED: Dict[ModelTier, str] = {
    tier: f"anthropic/{mid}" for tier, mid in CANONICAL_BARE.items()
}

#: Canonical tier capability ordering, cheapest → most capable. The single
#: source of truth for "tier A is below/at/above tier B" comparisons. Both
#: KNOWN_MODEL_TIERS (templates.py) and MODEL_ESCALATION_LADDER (adaptive_retry.py)
#: encode this same haiku<sonnet<opus order independently; this tuple is the
#: canonical reference the bound logic clamps against.
TIER_ORDER: tuple[ModelTier, ...] = (ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS)

#: Default tier used when a value cannot be resolved to a known tier.
_DEFAULT_TIER: ModelTier = ModelTier.SONNET

#: Short↔versioned alias map. Bridges the SHORT executor-map keys
#: (``"haiku"``/``"sonnet"``/``"opus"``) and the VERSIONED enum-value keys
#: (``"haiku-4-5"``/``"sonnet-4"``/``"opus-4-6"``) to the canonical enum. This
#: subsumes the inline ``_MAP`` previously living in
#: ``sequencer._resolve_model_tier``.
_ALIAS_TO_TIER: Dict[str, ModelTier] = {
    # short form
    "haiku": ModelTier.HAIKU,
    "sonnet": ModelTier.SONNET,
    "opus": ModelTier.OPUS,
    # versioned form (the ModelTier enum values)
    ModelTier.HAIKU.value: ModelTier.HAIKU,
    ModelTier.SONNET.value: ModelTier.SONNET,
    ModelTier.OPUS.value: ModelTier.OPUS,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def canonical_id(tier: ModelTier, *, prefixed: bool = False) -> str:
    """Return the canonical id for *tier* (bare by default, prefixed on request)."""
    return CANONICAL_PREFIXED[tier] if prefixed else CANONICAL_BARE[tier]


def clamp_tier(
    tier: ModelTier,
    min_tier: Optional[ModelTier] = None,
    max_tier: Optional[ModelTier] = None,
) -> ModelTier:
    """Clamp *tier* into the inclusive band ``[min_tier, max_tier]`` per TIER_ORDER.

    Returns *tier* raised to *min_tier* if it sits below the floor, lowered to
    *max_tier* if it sits above the ceiling, else *tier* unchanged. A ``None``
    bound is unbounded on that side. With both bounds ``None`` this is the
    identity function (the byte-identical default — see #987). Assumes
    ``min_tier <= max_tier`` (validated upstream at template-load time); this
    helper does not re-validate the band.
    """
    idx = TIER_ORDER.index(tier)
    if min_tier is not None:
        idx = max(idx, TIER_ORDER.index(min_tier))
    if max_tier is not None:
        idx = min(idx, TIER_ORDER.index(max_tier))
    return TIER_ORDER[idx]


def resolve_tier(value: Union[ModelTier, str, None]) -> Optional[ModelTier]:
    """Resolve *value* to a :class:`ModelTier`, or ``None`` on miss.

    Accepts a :class:`ModelTier` instance, a SHORT alias (``"haiku"``,
    ``"sonnet"``, ``"opus"``), or a VERSIONED enum value (``"haiku-4-5"``,
    ``"sonnet-4"``, ``"opus-4-6"``). Case-insensitive. Returns ``None`` for any
    unrecognised value (preserving the sequencer's "use runner default"
    semantics).
    """
    if isinstance(value, ModelTier):
        return value
    if not value:
        return None
    return _ALIAS_TO_TIER.get(str(value).lower())


def bare_id(
    tier_or_str: Union[ModelTier, str, None],
    default: str = "claude-sonnet-4-6",
) -> str:
    """Return the canonical **bare** id for *tier_or_str*.

    *tier_or_str* may be a :class:`ModelTier` or any short/versioned tier key.
    Falls back to *default* when the value cannot be resolved to a known tier.
    """
    tier = resolve_tier(tier_or_str)
    if tier is None:
        return default
    return CANONICAL_BARE[tier]


def prefixed_id(
    tier_or_str: Union[ModelTier, str, None],
    default: str = "anthropic/claude-sonnet-4-6",
) -> str:
    """Return the canonical ``anthropic/``-prefixed id for *tier_or_str*.

    *tier_or_str* may be a :class:`ModelTier` or any short/versioned tier key.
    Falls back to *default* when the value cannot be resolved to a known tier.
    """
    tier = resolve_tier(tier_or_str)
    if tier is None:
        return default
    return CANONICAL_PREFIXED[tier]
