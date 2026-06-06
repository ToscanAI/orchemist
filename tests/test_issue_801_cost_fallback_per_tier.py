"""Acceptance tests for issue #801 — per-tier cost fallback rates.

SUPERSEDED CONTRACT (updated for #911 + #913 / #916). The original #801 contract
used a *blended* per-tier `$/1K` heuristic (haiku 0.002 / sonnet 0.006 /
opus 0.033), deliberately biased to under-report. That heuristic was removed by
two later maintainer decisions:

  * #911 — first-party Anthropic pricing (Opus $5/$25; Sonnet $3/$15;
    Haiku $1/$5), and
  * #913 — routing the no-`total_cost` fallback through
    `PricingTable.compute_cost(model, prompt_tokens, completion_tokens)`, which
    prices the two directions separately and exactly (no blended rate).

The behavioral *invariants* the #801 contract guaranteed are preserved and
re-asserted here against the recomputed first-party values: usage.total_cost is
used unchanged when present; the no-`total_cost` cost is linear in tokens; tiers
are ordered opus > sonnet > haiku; unknown ids price at the table `default`
($3/$15, the sonnet class). Only the exact numbers change (they are now the
true first-party costs, e.g. sonnet 500+500 = 0.009 rather than the
deliberately-under-reported 0.006).

These tests are a regular (NOT sealed) test file; the "immutable after seal"
framing of the original acceptance phase no longer applies — see SPEC §0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the project src importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine.executors.openrouter_executor import OpenRouterExecutor
from orchestration_engine.schemas import TaskSpec, TaskState, TaskType


# ---------------------------------------------------------------------------
# Fixtures / helpers (intentionally local — do not import from
# tests/test_openrouter_executor.py to keep this acceptance file self-contained
# and immutable post-seal).
# ---------------------------------------------------------------------------


def _make_task(prompt: str = "Compute cost") -> TaskSpec:
    """Single-shot task (disable_tools=True) — exercises `_parse_response` path."""
    return TaskSpec(
        type=TaskType.CODE,
        payload={"prompt": prompt, "disable_tools": True},
    )


def _mock_response(
    *,
    prompt_tokens: int = 500,
    completion_tokens: int = 500,
    total_cost=None,
    content: str = "ok",
) -> bytes:
    """Build a mock OpenRouter API response as bytes (matches what urlopen returns)."""
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if total_cost is not None:
        usage["total_cost"] = total_cost
    return json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": usage,
        }
    ).encode("utf-8")


def _mock_urlopen(response_bytes: bytes) -> MagicMock:
    """Build a context-manager-shaped mock for urllib.request.urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_bytes
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _run(model_tier: str, response_bytes: bytes):
    """Execute a single-shot task with a mocked HTTP response, return the TaskResult."""
    executor = OpenRouterExecutor(api_key="sk-or-test")
    task = _make_task()
    with patch("urllib.request.urlopen") as mock_url:
        mock_url.return_value = _mock_urlopen(response_bytes)
        return executor.execute(task, model_tier=model_tier)


# ---------------------------------------------------------------------------
# Contract A — authoritative API cost is used when present (unchanged)
# ---------------------------------------------------------------------------


def test_contract_a_api_total_cost_used_when_present():
    """Contract A: usage.total_cost present → returned cost equals API value."""
    result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(
            prompt_tokens=500, completion_tokens=500, total_cost=0.0042
        ),
    )
    assert result.state == TaskState.SUCCESS
    # Exact API value flows through unchanged (within float→Decimal round-trip).
    assert float(result.cost_usd) == pytest.approx(0.0042, abs=1e-9)


# ---------------------------------------------------------------------------
# Contract B — sonnet-tier fallback rate
# ---------------------------------------------------------------------------


def test_contract_b_sonnet_fallback_rate_500_plus_500_tokens():
    """Contract B: no total_cost, sonnet → first-party PricingTable cost.

    500 in * $3/Mtok + 500 out * $15/Mtok = 0.0015 + 0.0075 = 0.009
    (superseded the old blended 0.006; #911/#913).
    """
    result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(
            prompt_tokens=500, completion_tokens=500, total_cost=None
        ),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.009, abs=1e-9)
    # Sanity: still STRICTLY LESS than the old $0.01/1K flat bug.
    assert float(result.cost_usd) < 0.01


def test_contract_b_sonnet_fallback_scales_linearly_with_tokens():
    """Contract B: rate is linear in token count — 2000 tokens at sonnet = 2x of 1000."""
    result_1k = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    result_2k = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(prompt_tokens=1000, completion_tokens=1000),
    )
    assert float(result_2k.cost_usd) == pytest.approx(
        2 * float(result_1k.cost_usd), rel=1e-6
    )


# ---------------------------------------------------------------------------
# Contract C — opus-tier fallback rate (strictly higher than sonnet)
# ---------------------------------------------------------------------------


def test_contract_c_opus_fallback_rate_500_plus_500_tokens():
    """Contract C: no total_cost, opus → first-party PricingTable cost.

    500 in * $5/Mtok + 500 out * $25/Mtok = 0.0025 + 0.0125 = 0.015
    (superseded the old blended 0.033, derived from the stale $15/$75 price;
    #911/#913).
    """
    result = _run(
        model_tier="opus",
        response_bytes=_mock_response(
            prompt_tokens=500, completion_tokens=500, total_cost=None
        ),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.015, abs=1e-9)


def test_contract_c_opus_cost_strictly_greater_than_sonnet_same_tokens():
    """Contract C cross-tier invariant: opus_cost > sonnet_cost at identical token counts."""
    sonnet_result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    opus_result = _run(
        model_tier="opus",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    # The ratio MUST be > 1 (sanity gate against a regression collapsing tiers).
    assert float(opus_result.cost_usd) > float(sonnet_result.cost_usd)
    # First-party ratio: 0.015 / 0.009 ≈ 1.667 (opus > sonnet invariant preserved).
    ratio = float(opus_result.cost_usd) / float(sonnet_result.cost_usd)
    assert ratio == pytest.approx(0.015 / 0.009, rel=1e-6)


def test_contract_c_opus_passthrough_model_id_also_detected():
    """Contract C: a literal opus id passes through and prices via PricingTable.

    The literal model id (not a tier alias) passes through to compute_cost; the
    retained opus-4-6 pricing key prices it at $5/$25 → 0.015 for 500+500,
    identical to the opus-4-8 the tier now emits (#911/#913).
    """
    result = _run(
        model_tier="anthropic/claude-opus-4-6",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert float(result.cost_usd) == pytest.approx(0.015, abs=1e-9)


# ---------------------------------------------------------------------------
# Contract D — haiku-tier fallback rate (strictly lower than sonnet)
# ---------------------------------------------------------------------------


def test_contract_d_haiku_fallback_rate_500_plus_500_tokens():
    """Contract D: no total_cost, haiku → first-party PricingTable cost.

    500 in * $1/Mtok + 500 out * $5/Mtok = 0.0005 + 0.0025 = 0.003
    (superseded the old blended 0.002; #911/#913).
    """
    result = _run(
        model_tier="haiku",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.003, abs=1e-9)


def test_contract_d_haiku_cost_strictly_less_than_sonnet_same_tokens():
    """Contract D cross-tier invariant: haiku_cost < sonnet_cost at identical token counts."""
    sonnet_result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    haiku_result = _run(
        model_tier="haiku",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert float(haiku_result.cost_usd) < float(sonnet_result.cost_usd)


# ---------------------------------------------------------------------------
# Contract E — unknown model id falls back to sonnet rate (no crash)
# ---------------------------------------------------------------------------


def test_contract_e_unknown_model_id_uses_sonnet_rate():
    """Contract E: unknown model id → PricingTable `default` ($3/$15), no crash.

    'meta-llama/llama-3.3-70b' has no pricing.yaml key → hits `default`
    (sonnet class) → 500 in*$3 + 500 out*$15 per Mtok = 0.009 (#911/#913).
    """
    result = _run(
        model_tier="meta-llama/llama-3.3-70b",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.009, abs=1e-9)


# ---------------------------------------------------------------------------
# Contract G — zero-token response yields zero cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_tier",
    ["sonnet", "opus", "haiku", "meta-llama/llama-3.3-70b"],
)
def test_contract_g_zero_tokens_yields_zero_cost_all_tiers(model_tier: str):
    """Contract G: zero tokens → zero cost, regardless of tier (linear-with-tokens property)."""
    result = _run(
        model_tier=model_tier,
        response_bytes=_mock_response(
            prompt_tokens=0, completion_tokens=0, total_cost=None
        ),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == 0.0


# ---------------------------------------------------------------------------
# Contract F — tool-loop path uses same per-tier fallback per round-trip
# ---------------------------------------------------------------------------


def test_contract_f_tool_loop_accumulates_per_tier_fallback_per_roundtrip():
    """Contract F: tool-loop path uses the same per-tier fallback as single-shot.

    Two-round tool loop where neither response provides total_cost. Round 1 issues
    a tool call (forces a second round-trip); round 2 returns plain text. The total
    cost MUST equal the sum of two per-round-trip sonnet-rate fallbacks.
    """
    executor = OpenRouterExecutor(api_key="sk-or-test")
    # Tool-enabled task (disable_tools NOT set → defaults to False → tool loop).
    task = TaskSpec(
        type=TaskType.CODE,
        payload={"prompt": "do work", "sandbox_roots": {"tmp_dir": "/tmp"}},
    )

    # Round 1 response: model issues a tool call (forces another round-trip).
    round1 = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "shell_exec",
                                    "arguments": json.dumps({"command": "echo hi"}),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 500, "completion_tokens": 500},
            # NB: no total_cost — exercises the fallback for THIS round-trip.
        }
    ).encode("utf-8")

    # Round 2 response: model returns plain text → loop terminates.
    round2 = json.dumps(
        {
            "choices": [{"message": {"content": "done"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 500},
            # NB: no total_cost — exercises the fallback for THIS round-trip too.
        }
    ).encode("utf-8")

    # Patch the executor's HTTP call to return round1 then round2.
    call_idx = {"n": 0}
    responses = [round1, round2]

    def _fake_post(body):
        idx = call_idx["n"]
        call_idx["n"] += 1
        return json.loads(responses[idx].decode("utf-8"))

    with patch.object(executor, "_do_post", side_effect=_fake_post):
        result = executor.execute(task, model_tier="sonnet")

    # Both round-trips should have completed; the cost equals 2x the per-round
    # first-party sonnet cost (500 in + 500 out → 0.009 per round, x2 = 0.018;
    # #911/#913).
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.018, abs=1e-9)


# ---------------------------------------------------------------------------
# Headline issue assertion — the 3x overestimate is gone
# ---------------------------------------------------------------------------


def test_headline_flat_rate_bug_eliminated_for_sonnet():
    """Headline: the flat $10/Mtok bug is gone; cost is now exact first-party.

    The original $801 bug priced every model at a single flat $10/Mtok
    ($61.01 reported vs $20.69 actual on 6.1M tokens). After #913 the
    no-`total_cost` path computes the true first-party cost via PricingTable
    with separate input/output rates. For 500k in + 500k out of sonnet that is
    0.5*$3 + 0.5*$15 = $9.00 — strictly below the old flat $10 (the
    overestimate is removed) and an *accurate* figure, not the deliberately
    under-reported blended rate of the superseded #801 contract.
    """
    # Simulate the run-6bb0349c-style aggregate: 1M tokens of sonnet, no total_cost.
    result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(
            prompt_tokens=500_000, completion_tokens=500_000, total_cost=None
        ),
    )
    new_cost = float(result.cost_usd)
    old_cost = (1_000_000 / 1000.0) * 0.01  # what the flat-rate bug produced: $10
    # Exact first-party sonnet cost for this 50/50 token split.
    assert new_cost == pytest.approx(9.0, abs=1e-9)
    # And it is strictly below the old flat-rate bug — the overestimate is gone.
    assert new_cost < old_cost
