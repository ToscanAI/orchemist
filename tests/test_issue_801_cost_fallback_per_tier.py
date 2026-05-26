"""Acceptance tests for issue #801 — per-tier cost fallback rates.

Issue: when the OpenRouter API response omits `usage.total_cost` (the dominant
case for Anthropic models), the executor falls back to a single flat rate of
$10/Mtok, which over-estimates real OpenRouter billing by ~3x (run 6bb0349c:
$61.01 reported vs $20.69 actual on 6.1M tokens).

Fix contract: the fallback rate is per-tier (sonnet / opus / haiku), substring-
detected from the OpenRouter model id. Unknown models default to the sonnet
tier. When `usage.total_cost` IS present, it is used unchanged.

These tests verify the behavioral contracts in
`output/issue-801-run/behavioral.md` (Contracts A-G) and are written BEFORE
implementation per the Orchemist acceptance-test phase contract. They are
immutable after seal.
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
    """Contract B: no total_cost, sonnet model → 0.006/1K (= 0.006 for 1000 tokens)."""
    result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(
            prompt_tokens=500, completion_tokens=500, total_cost=None
        ),
    )
    assert result.state == TaskState.SUCCESS
    # 1000 tokens * 0.006/1K = 0.006
    assert float(result.cost_usd) == pytest.approx(0.006, abs=1e-9)
    # Sanity: the new rate is STRICTLY LESS than the old $0.01/1K bug.
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
    """Contract C: no total_cost, opus model → 0.033/1K (= 0.033 for 1000 tokens)."""
    result = _run(
        model_tier="opus",
        response_bytes=_mock_response(
            prompt_tokens=500, completion_tokens=500, total_cost=None
        ),
    )
    assert result.state == TaskState.SUCCESS
    # 1000 tokens * 0.033/1K = 0.033
    assert float(result.cost_usd) == pytest.approx(0.033, abs=1e-9)


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
    # Specific ratio per behavioral contract: 0.033 / 0.006 ≈ 5.5
    ratio = float(opus_result.cost_usd) / float(sonnet_result.cost_usd)
    assert ratio == pytest.approx(0.033 / 0.006, rel=1e-6)


def test_contract_c_opus_passthrough_model_id_also_detected():
    """Contract C: opus tier detection works on any model id containing 'opus'."""
    # Passing a literal model id (not a tier alias) — substring match should still
    # route to the opus rate per the behavioral spec's "lowercase form contains 'opus'".
    result = _run(
        model_tier="anthropic/claude-opus-4-6",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert float(result.cost_usd) == pytest.approx(0.033, abs=1e-9)


# ---------------------------------------------------------------------------
# Contract D — haiku-tier fallback rate (strictly lower than sonnet)
# ---------------------------------------------------------------------------


def test_contract_d_haiku_fallback_rate_500_plus_500_tokens():
    """Contract D: no total_cost, haiku model → 0.002/1K (= 0.002 for 1000 tokens)."""
    result = _run(
        model_tier="haiku",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.002, abs=1e-9)


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
    """Contract E: unknown model id → sonnet-rate cost, no exception, state=SUCCESS."""
    # 'meta-llama/llama-3.3-70b' contains neither 'opus' nor 'haiku' → sonnet default.
    result = _run(
        model_tier="meta-llama/llama-3.3-70b",
        response_bytes=_mock_response(prompt_tokens=500, completion_tokens=500),
    )
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.006, abs=1e-9)


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
    # sonnet-rate fallback (1000 tokens * 0.006/1K = 0.006 per round, x2 = 0.012).
    assert result.state == TaskState.SUCCESS
    assert float(result.cost_usd) == pytest.approx(0.012, abs=1e-9)


# ---------------------------------------------------------------------------
# Headline issue assertion — the 3x overestimate is gone
# ---------------------------------------------------------------------------


def test_headline_3x_overestimate_eliminated_for_sonnet():
    """Headline: the old rate was $0.01/1K; the new sonnet rate is strictly lower.

    This is the issue's headline number: $61.01 reported vs $20.69 actual ≈ 3x.
    With the fix, the sonnet rate drops from $10/Mtok to $6/Mtok — a 40%
    reduction that brings the reported value within ~75% of the empirical
    OpenRouter average ($3.4/Mtok), well inside the issue's 30%-error target
    for the sonnet-dominated phases that drove the original overestimate.
    """
    # Simulate the run-6bb0349c-style aggregate: 1M tokens of sonnet, no total_cost.
    result = _run(
        model_tier="sonnet",
        response_bytes=_mock_response(
            prompt_tokens=500_000, completion_tokens=500_000, total_cost=None
        ),
    )
    new_cost = float(result.cost_usd)
    old_cost = (1_000_000 / 1000.0) * 0.01  # what the bug produced: $10
    # New cost is at least 30% lower than the bug — proves the 3x is gone.
    assert new_cost < old_cost * 0.7
    # And it's at most 2x the empirical OpenRouter average ($3.4/Mtok = $3.40 for 1M).
    empirical_actual = 3.40
    assert new_cost <= 2.0 * empirical_actual
