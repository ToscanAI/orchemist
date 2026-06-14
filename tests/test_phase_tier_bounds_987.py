"""Sealed acceptance tests — #987: per-phase model-tier BOUNDS (min_tier floor / max_tier ceiling).

Derived SOLELY from /home/toscan/ToscanWorkspace/.runs/run-987/behavioral.md (contracts B1-B6).
The tester saw the contract + only TODAY-REAL import paths/signatures — never the [NEW] implementation.

================================================================================
VALUE-SPACE CAVEAT (behavioral §0.1, §D.3 — DO NOT CONFLATE)
--------------------------------------------------------------------------------
* Resolution (`_resolve_model_tier`) and `clamp_tier` assert on the **ModelTier ENUM**
  (ModelTier.HAIKU/SONNET/OPUS). B1/B2/B3/B4 use the enum members.
* The escalation ladder (B6) asserts on **bare model-id STRINGS**
  (claude-haiku-4-5-20251001 / claude-sonnet-4-6 / claude-opus-4-8). B6 uses the strings.
  NB: ModelTier is a (str, Enum) whose *values* are versioned keys ("haiku-4-5", "sonnet-4",
  "opus-4-6"), NOT model ids — so an enum member never == a bare model id. Crossing the two
  spaces is a FALSE test.

================================================================================
EXPECTED-TODAY LEDGER (HEAD = main @ d71dc1b; derived from §C of the contract)
--------------------------------------------------------------------------------
SHIELDS (PASS-now — describe behavior already present at HEAD; must STAY green after impl):
  test_B4a_today_resolution_no_bounds_known_names .... PASS-now (1-arg _resolve_model_tier exists)
  test_B4a_today_resolution_no_bounds_unresolved ..... PASS-now (1-arg path; None/""/"bogus" -> None)
  test_B6_escalation_ladder_is_canonical_bare_ids .... PASS-now (MODEL_ESCALATION_LADDER exists today)
  test_B6_next_model_full_matrix ..................... PASS-now (_next_model exists today)

RED-until-impl (FAIL-now — exercise [NEW] symbols/params absent at HEAD):
  test_B1_tier_order_is_cheapest_to_most_capable ..... FAIL-now (TIER_ORDER [NEW], ImportError in-body)
  test_B1_clamp_tier_full_behaviour .................. FAIL-now (clamp_tier [NEW], ImportError in-body)
  test_B2_resolution_clamps_to_ceiling ............... FAIL-now (3-arg _resolve_model_tier [NEW];
                                                        1-arg sig rejects extra args -> TypeError = RED)
  test_B3_resolution_clamps_to_floor ................. FAIL-now (3-arg [NEW]; TypeError at HEAD)
  test_B4b_three_arg_unbounded_equals_one_arg ........ FAIL-now (3-arg [NEW]; TypeError at HEAD)
  test_B4b_floor_does_not_fabricate_tier_from_none ... FAIL-now (3-arg [NEW]; TypeError at HEAD)
  test_B5a_inverted_band_is_hard_error ............... FAIL-now (min_tier/max_tier fields [NEW] -> the
                                                        in-body PhaseDefinition(min_tier=...) TypeErrors)
  test_B5b_unknown_bound_value_is_warning ............ FAIL-now ([NEW] fields + lint branch)
  test_B5c_out_of_band_model_tier_is_warning ......... FAIL-now ([NEW] fields + lint branch)
  test_B5d_in_band_and_no_bounds_emit_nothing ........ FAIL-now ([NEW] fields; in-body construction TypeErrors)

SHIELD/RED split: 4 PASS-now, 10 FAIL-now (14 tests total).

Hermeticity: pure-unit. NO network, NO daemon, NO DB, NO pipeline run, NO env vars, NO filesystem
writes. Every [NEW] symbol/field is imported/constructed INSIDE a test body so module COLLECTION
succeeds at HEAD (where the [NEW] names are absent) and the SHIELDS still run.

CONTRACT-GAP (flag for the test_adversary): §0.5 names the validators' owning class `TemplateLoader`,
but no `TemplateLoader` symbol exists in `orchestration_engine.templates` at HEAD. The class that
actually owns `validate_template` / `validate_template_extended` / `KNOWN_MODEL_TIERS` is
`TemplateEngine` (the existing template tests call `TemplateEngine().validate_template_extended(...)`).
These tests therefore drive `TemplateEngine`, matching the live suite's idiom; the behavioral
severity table (ERROR/WARN/WARN, which validator) is honoured unchanged.
"""

import importlib

import pytest

# --- TODAY-REAL imports only (must resolve at HEAD so collection succeeds) ---------------
from src.orchestration_engine.schemas import ModelTier
from src.orchestration_engine.sequencer import PhaseSequencer
from src.orchestration_engine.adaptive_retry import (
    AdaptiveRetryEngine,
    MODEL_ESCALATION_LADDER,
)
from src.orchestration_engine.templates import (
    PhaseDefinition,
    PipelineTemplate,
    TemplateEngine,
)

# Local convenience handles to the enum members (the resolution/clamp value space).
HAIKU = ModelTier.HAIKU
SONNET = ModelTier.SONNET
OPUS = ModelTier.OPUS


# Mirror of tests/test_templates.py::_raw_for — the existing idiom for the raw dict that
# validate_template_extended takes. The bound/tier checks under test read the loaded
# `template`'s phases (not raw_data), so a minimal mirror is sufficient (§0.5).
def _raw_for(template):
    return {
        "id": template.id,
        "name": template.name,
        "phases": [{"id": p.id, "name": p.name} for p in template.phases],
    }


def _phase(**kwargs):
    """Build a PhaseDefinition INSIDE a test body.

    Kept as a helper (not a module-level fixture object) so that constructing a phase with the
    [NEW] min_tier=/max_tier= kwargs only happens when a test runs — at HEAD the dataclass has no
    such fields and the call raises TypeError, which is the intended RED for the B5 tests, while
    module collection stays clean.
    """
    base = dict(id="p1", name="Phase One", prompt_template="Do work. {input}")
    base.update(kwargs)
    return PhaseDefinition(**base)


# ===========================================================================
# B1 — clamp_tier ordering + clamp  [RED until impl]
# ===========================================================================


def test_B1_tier_order_is_cheapest_to_most_capable():
    """B1: TIER_ORDER == (HAIKU, SONNET, OPUS), cheapest -> most capable.

    GIVEN TIER_ORDER imported from orchestration_engine.model_registry
    THEN the order is exactly haiku < sonnet < opus, by index.
    Value space: ModelTier ENUM members.
    Expected-today: FAIL-now — TIER_ORDER is [NEW]; the in-body import raises ImportError at HEAD.
    """
    from src.orchestration_engine.model_registry import TIER_ORDER  # [NEW], lazy

    assert tuple(TIER_ORDER) == (HAIKU, SONNET, OPUS)
    assert (
        TIER_ORDER.index(HAIKU) < TIER_ORDER.index(SONNET) < TIER_ORDER.index(OPUS)
    )


def test_B1_clamp_tier_full_behaviour():
    """B1: clamp_tier clamps a ModelTier into [min_tier, max_tier] per TIER_ORDER and RETURNS a ModelTier.

    GIVEN clamp_tier(tier, min_tier=None, max_tier=None) from orchestration_engine.model_registry
    THEN every cased outcome in §B1 holds (in-range identity, below-floor raised, above-ceiling
         lowered, min=None no floor, max=None no ceiling, both-None identity).
    Value space: arguments AND return are ModelTier ENUM members (RETURN-TYPE PIN, §B1).
    Expected-today: FAIL-now — clamp_tier is [NEW]; the in-body import raises ImportError at HEAD.
    """
    from src.orchestration_engine.model_registry import clamp_tier  # [NEW], lazy

    # 1. In-range -> unchanged (identity within band).
    assert clamp_tier(SONNET, min_tier=HAIKU, max_tier=OPUS) == SONNET
    assert clamp_tier(HAIKU, min_tier=HAIKU, max_tier=OPUS) == HAIKU  # at the floor
    assert clamp_tier(OPUS, min_tier=HAIKU, max_tier=OPUS) == OPUS  # at the ceiling

    # 2. Below floor -> raised to min_tier.
    assert clamp_tier(HAIKU, min_tier=OPUS, max_tier=None) == OPUS
    assert clamp_tier(HAIKU, min_tier=SONNET, max_tier=None) == SONNET
    assert clamp_tier(SONNET, min_tier=OPUS) == OPUS

    # 3. Above ceiling -> lowered to max_tier.
    assert clamp_tier(OPUS, max_tier=SONNET) == SONNET
    assert clamp_tier(OPUS, min_tier=None, max_tier=HAIKU) == HAIKU
    assert clamp_tier(SONNET, max_tier=HAIKU) == HAIKU

    # 4. min_tier=None -> no floor.
    assert clamp_tier(HAIKU, min_tier=None, max_tier=SONNET) == HAIKU
    assert clamp_tier(HAIKU, min_tier=None, max_tier=OPUS) == HAIKU

    # 5. max_tier=None -> no ceiling.
    assert clamp_tier(OPUS, min_tier=SONNET, max_tier=None) == OPUS
    assert clamp_tier(SONNET, min_tier=HAIKU, max_tier=None) == SONNET

    # 6. Both None -> identity (THE byte-identical no-op), for every tier.
    for tier in (HAIKU, SONNET, OPUS):
        assert clamp_tier(tier, None, None) == tier
        assert clamp_tier(tier) == tier  # defaults

    # RETURN-TYPE PIN: result is a ModelTier enum member, not a string/short-name/model-id.
    assert isinstance(clamp_tier(SONNET, min_tier=HAIKU, max_tier=OPUS), ModelTier)


# ===========================================================================
# B2 — resolution clamps to the CEILING  [RED until impl]
# ===========================================================================


def test_B2_resolution_clamps_to_ceiling():
    """B2: a phase authored HIGH but capped LOWER resolves to the LOWER tier, never the authored one.

    GIVEN PhaseSequencer._resolve_model_tier (a @staticmethod) with the [NEW] 3-arg signature
          (model_tier_str, min_tier=None, max_tier=None)
    WHEN authored "opus" with ceiling max_tier="sonnet" (no floor)
    THEN result == ModelTier.SONNET and != ModelTier.OPUS; tighter ceilings clamp further; a
         ceiling at/above the authored tier does NOT lower it.
    Value space: ModelTier ENUM (ASSERTION-TARGET PIN, §B2) — never a bare model-id string.
    Expected-today: FAIL-now — the 3-arg form is [NEW]; at HEAD the 1-arg signature rejects the
                    extra positional args with TypeError (RED-by-design), so the SONNET outcome
                    cannot be produced today.
    """
    resolve = PhaseSequencer._resolve_model_tier

    # Decisive ceiling: opus authored, capped to sonnet -> SONNET, NOT OPUS.
    result = resolve("opus", None, "sonnet")
    assert result == ModelTier.SONNET
    assert result != ModelTier.OPUS

    # Tighter ceiling clamps further.
    assert resolve("opus", None, "haiku") == ModelTier.HAIKU
    assert resolve("opus", None, "haiku") != ModelTier.OPUS
    assert resolve("sonnet", None, "haiku") == ModelTier.HAIKU

    # Ceiling at or above authored tier -> unchanged.
    assert resolve("sonnet", None, "opus") == ModelTier.SONNET
    assert resolve("sonnet", None, "sonnet") == ModelTier.SONNET


# ===========================================================================
# B3 — resolution clamps to the FLOOR  [RED until impl]
# ===========================================================================


def test_B3_resolution_clamps_to_floor():
    """B3: a phase authored LOW but floored HIGHER resolves UP to the floor, never below it.

    GIVEN PhaseSequencer._resolve_model_tier with the [NEW] 3-arg signature
    WHEN authored "sonnet" with floor min_tier="opus" (no ceiling)
    THEN result == ModelTier.OPUS and != ModelTier.SONNET; a floor above a HAIKU author also raises;
         a floor at/below the authored tier does NOT raise it.
    Value space: ModelTier ENUM (ASSERTION-TARGET PIN, §B3).
    Expected-today: FAIL-now — 3-arg form [NEW]; TypeError at HEAD (RED-by-design).
    """
    resolve = PhaseSequencer._resolve_model_tier

    # Decisive floor: sonnet authored, floored to opus -> OPUS, NOT below.
    result = resolve("sonnet", "opus", None)
    assert result == ModelTier.OPUS
    assert result != ModelTier.SONNET

    # Floor above a HAIKU author also raises.
    assert resolve("haiku", "sonnet", None) == ModelTier.SONNET
    assert resolve("haiku", "opus", None) == ModelTier.OPUS

    # Floor at or below authored tier -> unchanged.
    assert resolve("opus", "sonnet", None) == ModelTier.OPUS
    assert resolve("sonnet", "sonnet", None) == ModelTier.SONNET


# ===========================================================================
# B4a — today's resolution, no bounds  [SHIELD: green at HEAD]
# ===========================================================================


def test_B4a_today_resolution_no_bounds_known_names():
    """B4a (SHIELD): with the 1-arg form that EXISTS today, each short name resolves to its tier.

    GIVEN PhaseSequencer._resolve_model_tier called with a SINGLE argument
    THEN "haiku"->HAIKU, "sonnet"->SONNET, "opus"->OPUS, unchanged.
    Value space: ModelTier ENUM.
    Expected-today: PASS-now — uses ONLY the pre-existing 1-arg signature; the immovable definition
                    of "today's behavior" that the clamp must not alter (§B4a). Must STAY green.
    """
    resolve = PhaseSequencer._resolve_model_tier
    assert resolve("haiku") == ModelTier.HAIKU
    assert resolve("sonnet") == ModelTier.SONNET
    assert resolve("opus") == ModelTier.OPUS


def test_B4a_today_resolution_no_bounds_unresolved():
    """B4a (SHIELD): an absent/unknown authored tier resolves to None (runner default).

    GIVEN the 1-arg PhaseSequencer._resolve_model_tier
    THEN None / "" / "bogus-tier" each resolve to None.
    Expected-today: PASS-now — pre-existing 1-arg behavior. None means "runner uses its default".
    """
    resolve = PhaseSequencer._resolve_model_tier
    assert resolve(None) is None
    assert resolve("") is None
    assert resolve("bogus-tier") is None


# ===========================================================================
# B4b — the 3-arg unbounded call equals the 1-arg call  [RED until impl]
# ===========================================================================


def test_B4b_three_arg_unbounded_equals_one_arg():
    """B4b: passing (None, None) for the bounds is byte-identical to the 1-arg call, every input.

    GIVEN the post-impl 3-arg signature
    THEN _resolve_model_tier(X, None, None) == _resolve_model_tier(X) for X in {sonnet, haiku, opus}.
    Value space: ModelTier ENUM. This is the load-bearing back-compat shield (§D.1): the clamp must
                 never alter a no-bounds resolution.
    Expected-today: FAIL-now — the 3-arg form is [NEW]; TypeError at HEAD (RED-by-design).
    """
    resolve = PhaseSequencer._resolve_model_tier
    assert resolve("sonnet", None, None) == resolve("sonnet") == ModelTier.SONNET
    assert resolve("haiku", None, None) == ModelTier.HAIKU
    assert resolve("opus", None, None) == ModelTier.OPUS


def test_B4b_floor_does_not_fabricate_tier_from_none():
    """B4b: an unresolved authored tier returns None even WITH a floor — clamp never fabricates.

    GIVEN the 3-arg signature
    THEN _resolve_model_tier("bogus-tier", "opus", None) is None  (floor does NOT force OPUS),
         _resolve_model_tier(None, "opus", "opus") is None,
         _resolve_model_tier("", "sonnet", None) is None.
    The subtle, decisive design point (§B4b / §D.1): clamping applies ONLY to a tier that already
    resolved to a concrete ModelTier; "author wanted the runner default" (None) survives any bounds.
    Expected-today: FAIL-now — 3-arg form [NEW]; TypeError at HEAD (RED-by-design).
    """
    resolve = PhaseSequencer._resolve_model_tier
    assert resolve("bogus-tier", "opus", None) is None
    assert resolve(None, "opus", "opus") is None
    assert resolve("", "sonnet", None) is None


# ===========================================================================
# B5 — validation: ERROR on inverted band, WARN on unknown bound / out-of-band  [RED until impl]
# ===========================================================================


def test_B5a_inverted_band_is_hard_error():
    """B5a: min_tier > max_tier (inverted, unsatisfiable) -> HARD ERROR in validate_template.

    GIVEN a template whose phase has min_tier="opus", max_tier="haiku" (min strictly above max)
    WHEN validate_template(template) is called
    THEN the returned List[str] CONTAINS an error string mentioning BOTH "min_tier" and "max_tier";
         AND a VALID band (min_tier="haiku", max_tier="opus") produces NO such inverted-band error.
    Match by SUBSTRING, never exact list equality (§0.7).
    Expected-today: FAIL-now — min_tier/max_tier are [NEW] PhaseDefinition fields; the in-body
                    PhaseDefinition(min_tier=..., max_tier=...) raises TypeError at HEAD, and the
                    inverted-band branch in validate_template does not yet exist (RED-by-design).
    """
    engine = TemplateEngine()

    inverted = PipelineTemplate(
        id="t", name="T", phases=[_phase(min_tier="opus", max_tier="haiku")]
    )
    errors = engine.validate_template(inverted)
    assert any("min_tier" in e and "max_tier" in e for e in errors), errors

    valid = PipelineTemplate(
        id="t", name="T", phases=[_phase(min_tier="haiku", max_tier="opus")]
    )
    valid_errors = engine.validate_template(valid)
    assert not any("min_tier" in e and "max_tier" in e for e in valid_errors), valid_errors


def test_B5b_unknown_bound_value_is_warning():
    """B5b: an unknown min_tier/max_tier value -> WARN in validate_template_extended, NOT error.

    GIVEN a phase with min_tier="sonet" (typo, not in KNOWN_MODEL_TIERS), otherwise valid
    WHEN validate_template_extended(template, {}) -> (errors, warnings)
    THEN a warning naming the unknown "min_tier" + "sonet" is present, and NO "min_tier" error;
         symmetrically for max_tier="opuss".
    Match by SUBSTRING.
    Expected-today: FAIL-now — [NEW] fields + lint branch; in-body construction TypeErrors at HEAD.
    """
    engine = TemplateEngine()

    tpl_min = PipelineTemplate(id="t", name="T", phases=[_phase(min_tier="sonet")])
    errors, warnings = engine.validate_template_extended(tpl_min, _raw_for(tpl_min))
    assert any("min_tier" in w and "sonet" in w for w in warnings), warnings
    assert not any("min_tier" in e for e in errors), errors

    tpl_max = PipelineTemplate(id="t", name="T", phases=[_phase(max_tier="opuss")])
    errors2, warnings2 = engine.validate_template_extended(tpl_max, _raw_for(tpl_max))
    assert any("max_tier" in w and "opuss" in w for w in warnings2), warnings2
    assert not any("max_tier" in e for e in errors2), errors2


def test_B5c_out_of_band_model_tier_is_warning():
    """B5c: authored model_tier OUTSIDE [min_tier, max_tier] -> WARN (NOT error; clamp self-heals).

    GIVEN a phase authored model_tier="opus" with max_tier="sonnet" (above the ceiling — a valid,
          self-healing band, just a likely authoring mistake)
    WHEN validate_template_extended(template, {})
    THEN a warning mentioning BOTH "model_tier" and "max_tier" is present in warnings, and NOT an
         error mentioning both; symmetrically authored model_tier="sonnet" with min_tier="opus"
         (below floor) -> warning mentioning "model_tier" and "min_tier".
    Match by SUBSTRING.
    Expected-today: FAIL-now — [NEW] fields + lint branch; in-body construction TypeErrors at HEAD.
    """
    engine = TemplateEngine()

    above_ceiling = PipelineTemplate(
        id="t", name="T", phases=[_phase(model_tier="opus", max_tier="sonnet")]
    )
    errors, warnings = engine.validate_template_extended(
        above_ceiling, _raw_for(above_ceiling)
    )
    assert any("model_tier" in w and "max_tier" in w for w in warnings), warnings
    assert not any("model_tier" in e and "max_tier" in e for e in errors), errors

    below_floor = PipelineTemplate(
        id="t", name="T", phases=[_phase(model_tier="sonnet", min_tier="opus")]
    )
    errors2, warnings2 = engine.validate_template_extended(
        below_floor, _raw_for(below_floor)
    )
    assert any("model_tier" in w and "min_tier" in w for w in warnings2), warnings2
    assert not any("model_tier" in e and "min_tier" in e for e in errors2), errors2


def test_B5d_in_band_and_no_bounds_emit_nothing():
    """B5d: an in-band phase AND a no-bounds phase emit NEITHER the new error nor the new warnings.

    GIVEN a phase model_tier="sonnet", min_tier="haiku", max_tier="opus" (authored in-band, valid),
          AND a phase with NO bounds (min_tier=None, max_tier=None)
    THEN validate_template contributes no inverted-band error (no string with both "min_tier" and
         "max_tier"); validate_template_extended contributes no unknown-bound warning and no
         out-of-band-model_tier warning for these phases.
    Match by SUBSTRING (pre-existing advisories may coexist).
    Expected-today: FAIL-now — the in-band phase uses [NEW] min_tier/max_tier kwargs, so the in-body
                    PhaseDefinition(...) raises TypeError at HEAD (RED-by-design). Post-impl this is
                    the back-compat half: valid/no-bound phases stay silent in the [NEW] checks.
    """
    engine = TemplateEngine()

    in_band = _phase(id="ib", name="In Band", model_tier="sonnet", min_tier="haiku", max_tier="opus")
    no_bounds = _phase(id="nb", name="No Bounds", model_tier="sonnet", min_tier=None, max_tier=None)
    tpl = PipelineTemplate(id="t", name="T", phases=[in_band, no_bounds])

    errors = engine.validate_template(tpl)
    assert not any("min_tier" in e and "max_tier" in e for e in errors), errors

    ext_errors, warnings = engine.validate_template_extended(tpl, _raw_for(tpl))
    # No unknown-bound warning (the bounds are all valid short names or None).
    assert not any("min_tier" in w and "haiku" in w for w in warnings), warnings
    assert not any("max_tier" in w and "opus" in w for w in warnings), warnings
    # No out-of-band-model_tier warning (sonnet is inside [haiku, opus]).
    assert not any("model_tier" in w and "max_tier" in w for w in warnings), warnings
    assert not any("model_tier" in w and "min_tier" in w for w in warnings), warnings


# ===========================================================================
# B6 — escalation ladder UNCHANGED  [SHIELD: green at HEAD]
# ===========================================================================


def test_B6_escalation_ladder_is_canonical_bare_ids():
    """B6 (SHIELD): MODEL_ESCALATION_LADDER is exactly the three canonical bare model-id STRINGS.

    GIVEN MODEL_ESCALATION_LADDER from orchestration_engine.adaptive_retry
    THEN == ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"], cheapest -> top.
    Value space: bare model-id STRINGS (NOT ModelTier enums) — §B6 / §D.3.
    Expected-today: PASS-now — the ladder exists today. Must STAY byte-identical after impl
                    (the reframing's proof: bounds live at resolution, NOT escalation, §D.2).
    """
    assert MODEL_ESCALATION_LADDER == [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
    ]


def test_B6_next_model_full_matrix():
    """B6 (SHIELD): _next_model climbs one rung, caps at the top, unknown/None jump to the top.

    GIVEN AdaptiveRetryEngine._next_model (a @staticmethod) from orchestration_engine.adaptive_retry
    THEN the FULL matrix:
         haiku-id -> sonnet-id, sonnet-id -> opus-id, opus-id -> opus-id (capped),
         None -> opus-id, unknown-id -> opus-id.
    Value space: bare model-id STRINGS.
    Expected-today: PASS-now — existing escalation semantics. Guards against an impl wrongly touching
                    the escalation path; must STAY green after impl (§D.2).
    """
    nxt = AdaptiveRetryEngine._next_model
    assert nxt("claude-haiku-4-5-20251001") == "claude-sonnet-4-6"
    assert nxt("claude-sonnet-4-6") == "claude-opus-4-8"
    assert nxt("claude-opus-4-8") == "claude-opus-4-8"  # capped at top
    assert nxt(None) == "claude-opus-4-8"
    assert nxt("some-unknown-model-id") == "claude-opus-4-8"
