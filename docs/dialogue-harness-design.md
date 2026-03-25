# Dialogue Harness — Multi-Model Conversational Spec Review

**Status:** Design phase
**Epic:** #677
**Author:** Toscan + Conny
**Date:** 2026-03-25

---

## The Discovery

Issue #676 (Config Resolution Pipeline) went through 3 manual review rounds between Toscan (Opus) and Gemini 3.1 Pro (deep think). Each round caught critical architectural flaws:

- **Round 1:** AC/contract mismatches, missing edge cases, architectural contradiction (init-vs-JIT timing)
- **Round 2:** Truthiness trap (`"false"` is truthy in Python), scope overreach (banning lists at init), missing happy-path ACs
- **Round 3:** Type coercion must be JIT not init, lists/dicts valid in config just not in interpolation

No single-agent pipeline phase catches these. The quality comes from two models with different reasoning styles iterating on the same artifact.

## Architecture Decision: SDK Over CLI

**The Gemini CLI has a 5-minute process timeout.** Deep think frequently exceeds this. The CLI is useful for quick one-shot queries but not for the sustained reasoning cycles this harness requires.

**Decision:** Use the Google Gen AI SDK directly (Python — we're a Python project). This gives us:
- Full timeout control (15 min+ for deep think)
- Streaming support for progress monitoring
- Direct access to thinking config
- No subprocess/shell overhead
- Native error handling (auth, rate limits, model errors)

### Why not the existing Gemini API key path?

We already have a Gemini API key for image generation. But:
- API key billing is per-token — deep think at 5+ min burns significant budget
- René's subscription (OAuth) gives ~1,500 req/day for free
- The SDK supports both auth methods — we use OAuth for the harness

### Gemini CLI Deep Think Config (Reference)

Even though we're using the SDK, the CLI config is useful for manual testing:

```json
// ~/.gemini/settings.json
{
  "modelConfigs": {
    "customAliases": {
      "deep-think-harness": {
        "modelConfig": {
          "model": "gemini-3.1-pro-preview",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "HIGH"
            },
            "temperature": 0.2
          }
        }
      }
    }
  }
}
```

```bash
# Manual testing
echo "Review this spec..." | gemini -p "" -m deep-think-harness
```

## Core Design

### The Loop

```
┌─────────────────────────────────────────────────────┐
│                  Dialogue Harness                     │
│                                                       │
│   Input: rough issue description / draft spec         │
│                                                       │
│   Round 1:                                            │
│     Drafter (Claude Opus) → initial spec              │
│     Reviewer (Gemini 3.1 Pro, deep think) → critique  │
│                                                       │
│   Round 2:                                            │
│     Drafter receives critique → revised spec          │
│     Reviewer receives revision → critique or APPROVED │
│                                                       │
│   Round N:                                            │
│     Reviewer says APPROVED → done                     │
│     OR max_rounds hit → done with last version        │
│                                                       │
│   Output: final spec + full conversation trace        │
└─────────────────────────────────────────────────────┘
```

### Participants

| Role | Model | Executor | Auth | Timeout |
|------|-------|----------|------|---------|
| **Drafter** | Claude Opus 4.6 | OpenClaw gateway (sessions_spawn) | Subscription token | 5 min |
| **Reviewer** | Gemini 3.1 Pro Preview | Google Gen AI SDK (Python) | OAuth (subscription) | 15 min |

### Why These Roles?

- **Claude Opus as drafter:** Excellent at structured writing, behavioral contracts, systematic spec production. Already our Tier 3 model for high-stakes work.
- **Gemini 3.1 Pro as reviewer:** Deep think mode produces adversarial analysis that catches architectural contradictions Claude misses. Different training, different blind spots — that's the point.

The models' weaknesses are complementary. Claude tends toward confident, internally-consistent specs that can have structural blind spots. Gemini deep think tends toward exhaustive edge-case analysis but sometimes over-engineers. Together they converge on specs that are both coherent and thorough.

## Implementation: Two Paths

### Path A: Standalone Script (MVP)

A Python script that orchestrates the conversation loop outside the pipeline engine. Simpler to build, faster to ship, proves the concept.

```
dialogue-harness.py <input.md> --output-dir <dir> --max-rounds 4
```

**Drafter calls:** OpenClaw gateway API (same as `openclaw_executor.py`)
**Reviewer calls:** Google Gen AI SDK directly

**Pros:** Fast to build, no engine changes, testable immediately
**Cons:** Not integrated with `orch launch`, no pipeline chaining, separate monitoring

### Path B: Engine-Integrated Dialogue Phase

A new phase type in the sequencer that the engine manages natively.

```yaml
- id: spec_review
  type: dialogue
  drafter:
    executor: openclaw
    model_tier: opus
    system_prompt: "..."
  reviewer:
    executor: gemini-sdk
    model: gemini-3.1-pro-preview
    thinking_level: HIGH
    system_prompt: "..."
  max_rounds: 4
  convergence_signal: "APPROVED"
```

**Pros:** Full engine integration, pipeline chaining, monitoring, cost tracking
**Cons:** Requires new executor + new phase type + sequencer changes

### Recommendation: Path A first, Path B after validation

Build the standalone script. Run it 5-10 times on real specs. Learn what works. Then integrate into the engine with confidence about the conversation dynamics, prompt engineering, and convergence patterns.

## SDK Integration Details

### Python: Google Gen AI SDK

```python
from google import genai
from google.genai import types

# OAuth client (subscription, no API billing)
client = genai.Client()  # Uses Application Default Credentials or OAuth

def get_gemini_review(spec_text: str, review_history: str = "") -> str:
    """Call Gemini 3.1 Pro with deep think for spec review."""
    
    prompt = f"""You are a principal software architect reviewing a specification.
    
## Spec to Review
{spec_text}

## Previous Review History
{review_history if review_history else "This is the first review round."}

## Your Task
Analyze this specification for:
1. Architectural contradictions between sections
2. Behavioral contracts that don't match acceptance criteria
3. Unaddressed technical edge cases (type safety, timing, scope)
4. Missing error paths and downstream cascade behavior

If the spec is airtight, respond with: APPROVED — followed by a brief summary of why.
If issues remain, list each issue with: the contradiction, why it matters, and exact fix text.
"""
    
    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=prompt,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level="HIGH"
            ),
            temperature=0.2,
            http_options=types.HttpOptions(
                timeout=900_000  # 15 minutes
            )
        )
    )
    
    return response.text
```

### Claude via OpenClaw Gateway

```python
import requests

GATEWAY_URL = "http://localhost:18789"
GATEWAY_TOKEN = "..."  # Read from openclaw.json

def get_claude_draft(input_text: str, review_feedback: str = "") -> str:
    """Spawn a sub-agent via OpenClaw to draft/revise a spec."""
    
    prompt = f"""You are a senior software engineer writing a specification.
    
## Input
{input_text}

## Reviewer Feedback to Address
{review_feedback if review_feedback else "This is the initial draft. No feedback yet."}

## Your Task
Produce a complete specification following the bug-report template with:
- Problem, Evidence, Root Cause
- Architecture section with explicit timing/scope decisions
- Behavioral contracts (Given/When/Then) for every behavior
- Edge cases with explicit outcomes
- Acceptance criteria (1:1 mapped to contracts)
- Files likely affected

Address ALL reviewer feedback. Do not ignore or defer items.
"""
    
    # Use sessions_spawn equivalent via gateway API
    response = requests.post(
        f"{GATEWAY_URL}/api/sessions/spawn",
        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
        json={
            "task": prompt,
            "model": "anthropic/claude-opus-4-6",
            "mode": "run",
            "runTimeoutSeconds": 300
        }
    )
    
    return response.json()["result"]
```

### Authentication: OAuth for Gemini SDK

The Google Gen AI SDK needs OAuth credentials. Two options:

**Option 1: Application Default Credentials (ADC)**
```bash
gcloud auth application-default login
# Stores creds at ~/.config/gcloud/application_default_credentials.json
# SDK picks them up automatically
```

**Option 2: Reuse Gemini CLI OAuth tokens**
The CLI stores OAuth tokens at `~/.gemini/oauth_creds.json`. The SDK may be able to consume these directly, or we can write a thin adapter that refreshes the token and passes it to the SDK client.

**TODO:** Test which auth path works with René's subscription quota (not API billing).

## Conversation Trace & Output

Each round produces files in the output directory:

```
output/
├── round-1-draft.md          # Drafter's initial spec
├── round-1-review.md         # Reviewer's critique
├── round-2-draft.md          # Drafter's revision
├── round-2-review.md         # Reviewer's APPROVED (or critique)
├── round-3-draft.md          # (if needed)
├── round-3-review.md         # (if needed)
├── final-spec.md             # Last drafter version (the deliverable)
├── conversation-summary.md   # Metadata: rounds, convergence, timing
└── conversation-full.json    # Complete trace for audit
```

`conversation-summary.md` contains:
```markdown
# Dialogue Harness — Conversation Summary

- **Topic:** Config Resolution Pipeline
- **Rounds:** 3
- **Converged:** Yes (APPROVED in round 3)
- **Total time:** 18m 42s
- **Drafter model:** Claude Opus 4.6
- **Reviewer model:** Gemini 3.1 Pro Preview (deep think)
- **Round 1:** 4 issues found (2 critical, 2 moderate)
- **Round 2:** 2 issues found (1 critical, 1 moderate)  
- **Round 3:** APPROVED — "no remaining architectural contradictions"
```

## Prompt Engineering Notes

### Reviewer Prompt Must Include:

1. **Structural checks:** Do the ACs match the contracts 1:1?
2. **Timing analysis:** When does each operation happen? Init vs JIT vs per-phase?
3. **Type safety:** What happens with `None`, `False`, `[]`, `{}` in Python?
4. **Scope boundaries:** What's explicitly out of scope? Is it stated?
5. **Downstream impact:** If X fails, what happens to Y?

### Drafter Prompt Must Include:

1. **The issue template** — enforce the structure
2. **"Address ALL feedback"** — Claude tends to acknowledge feedback then not fix it
3. **Explicit instruction to update ACs when contracts change** — prevents drift
4. **"Show your changes"** — helps the reviewer see what was modified

### Anti-Patterns to Prevent:

- **Infinite agreement loop:** Reviewer keeps finding new issues each round without acknowledging progress. Fix: reviewer must state which previous issues are resolved.
- **Scope creep:** Reviewer suggests features beyond the bug fix. Fix: reviewer prompt says "Do not suggest new features. Only check what's specified."
- **Drafter ignoring feedback:** Drafter acknowledges critique but doesn't change the spec. Fix: reviewer must verify previous feedback was actually applied.

## Open Questions

1. **OAuth token reuse:** Can the Python Gen AI SDK consume the Gemini CLI's OAuth tokens, or do we need a separate `gcloud auth` flow?
2. **Subscription quota:** Does the SDK path count against the same ~1,500 req/day as the CLI?
3. **Thinking budget visibility:** Can we see how much thinking time Gemini used per response? Useful for cost/time estimation.
4. **Streaming:** Should the harness stream Gemini's response for progress indication, or just wait for completion?
5. **Error recovery:** If Gemini times out mid-deep-think, do we retry the same round or skip to next?

## Potential Beyond Spec Review

The dialogue pattern has applications beyond spec review:

- **Code review:** Drafter writes code, reviewer checks for bugs/security/performance
- **Red team content:** Drafter writes article, reviewer attacks it for backlash risk
- **Architecture decisions:** Two models debate trade-offs, produce a decision record
- **Test generation:** One model writes tests, another tries to break them

The harness is model-agnostic by design. Today it's Claude + Gemini. Tomorrow it could be any two models with different reasoning strengths.

## Next Steps

1. **René tests Gemini CLI config** — confirm `deep-think-harness` alias works, measure actual response times
2. **Test SDK auth** — verify OAuth path works with subscription quota
3. **Build MVP script** (Path A) — standalone `dialogue-harness.py`
4. **Run 5 real specs** — validate conversation dynamics, prompt engineering, convergence
5. **Integrate into engine** (Path B) — new executor + dialogue phase type
