"""Unit tests for the canonical model_registry (#916).

Covers tier resolution (short / versioned / enum / unknown), the bare_id /
prefixed_id helpers and their defaults, the maintainer-authorized OPUS→4-8
emission, the absence of any stale/dotted id in the registry's output, and a
cross-check that every id the registry can emit has an exact pricing.yaml key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project src importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine import model_registry as mr
from orchestration_engine.cost_tracker import PricingTable
from orchestration_engine.schemas import ModelTier


# ---------------------------------------------------------------------------
# resolve_tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("haiku", ModelTier.HAIKU),
        ("sonnet", ModelTier.SONNET),
        ("opus", ModelTier.OPUS),
        ("HAIKU", ModelTier.HAIKU),  # case-insensitive
        ("haiku-4-5", ModelTier.HAIKU),  # versioned enum value
        ("sonnet-4", ModelTier.SONNET),
        ("opus-4-6", ModelTier.OPUS),
        (ModelTier.OPUS, ModelTier.OPUS),  # ModelTier instance passes through
    ],
)
def test_resolve_tier_known(value, expected):
    assert mr.resolve_tier(value) is expected


@pytest.mark.parametrize("value", ["gpt-99", "claude-opus-4-6", "", None])
def test_resolve_tier_unknown_returns_none(value):
    # Unknown short/versioned keys (and a full model id, and empty/None) → None,
    # preserving the "use runner default" semantics.
    assert mr.resolve_tier(value) is None


# ---------------------------------------------------------------------------
# bare_id / prefixed_id canonical values
# ---------------------------------------------------------------------------


def test_canonical_bare_values():
    assert mr.bare_id(ModelTier.HAIKU) == "claude-haiku-4-5-20251001"
    assert mr.bare_id(ModelTier.SONNET) == "claude-sonnet-4-6"
    assert mr.bare_id(ModelTier.OPUS) == "claude-opus-4-8"


def test_canonical_prefixed_values():
    assert mr.prefixed_id(ModelTier.HAIKU) == "anthropic/claude-haiku-4-5-20251001"
    assert mr.prefixed_id(ModelTier.SONNET) == "anthropic/claude-sonnet-4-6"
    assert mr.prefixed_id(ModelTier.OPUS) == "anthropic/claude-opus-4-8"


def test_opus_tier_emits_opus_4_8():
    """Maintainer-authorized: the OPUS tier emits opus-4-8 (§0b)."""
    assert mr.bare_id(ModelTier.OPUS) == "claude-opus-4-8"
    assert mr.prefixed_id(ModelTier.OPUS) == "anthropic/claude-opus-4-8"


def test_helpers_accept_short_and_versioned_keys():
    assert mr.bare_id("opus") == "claude-opus-4-8"
    assert mr.bare_id("opus-4-6") == "claude-opus-4-8"
    assert mr.prefixed_id("haiku") == "anthropic/claude-haiku-4-5-20251001"
    assert mr.prefixed_id("haiku-4-5") == "anthropic/claude-haiku-4-5-20251001"


def test_helpers_fall_back_to_default_on_miss():
    assert mr.bare_id("gpt-99") == "claude-sonnet-4-6"
    assert mr.prefixed_id("gpt-99") == "anthropic/claude-sonnet-4-6"
    assert mr.bare_id(None) == "claude-sonnet-4-6"
    assert mr.prefixed_id(None) == "anthropic/claude-sonnet-4-6"
    # Custom default is honored.
    assert mr.bare_id("nope", default="x") == "x"
    assert mr.prefixed_id("nope", default="y") == "y"


# ---------------------------------------------------------------------------
# No stale / dotted id may be emitted
# ---------------------------------------------------------------------------


_STALE_FRAGMENTS = (
    "20241022",
    "claude-haiku-4.5",
    "sonnet-4-20250514",
    "opus-4-6",  # opus-4-6 must not be the tier's emitted id (it is only a pricing literal)
)


def test_registry_emits_no_stale_or_dotted_id():
    emitted = [mr.bare_id(t) for t in ModelTier] + [mr.prefixed_id(t) for t in ModelTier]
    for value in emitted:
        for frag in _STALE_FRAGMENTS:
            assert frag not in value, f"stale fragment {frag!r} in emitted id {value!r}"


# ---------------------------------------------------------------------------
# Every registry-emittable id has an exact pricing.yaml key
# ---------------------------------------------------------------------------


def test_every_emittable_id_has_exact_pricing_key():
    pt = PricingTable()
    for tier in ModelTier:
        for model_id in (mr.bare_id(tier), mr.prefixed_id(tier)):
            assert pt.has_model(model_id), (
                f"{model_id!r} has no exact pricing.yaml key — it would "
                f"silently bill at the table default."
            )
