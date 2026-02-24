# Architecture Review: OpenClaw Output Extraction (D+E Hybrid)

**Reviewer:** Toscan (subagent, Opus 4.6)  
**Date:** 2026-02-24  
**Document:** `openclaw-output-extraction-architecture.md`  
**Verdict:** **APPROVE_WITH_CHANGES**

---

## Executive Summary

The D+E hybrid (file-based output + marker extraction with fallback chain) is a **sound approach** but the document has a critical blind spot: it overlooks a simpler, higher-value first step and misjudges the complexity of alternatives. The architecture is over-engineered for an MVP when a much simpler path exists.

---

## Question-by-Question Analysis

### Q1: Is the D+E hybrid the right choice? Are there approaches we missed?

**The D+E hybrid is defensible but not optimal as a starting point.**

The document correctly eliminates Approach A (text-only agents) and B (raw transcript). However, it conflates two different things under Approach A:

- **A1: Disable tools entirely** — correctly rejected
- **A2: Keep tools, but instruct agent to produce a comprehensive final message** — this is actually a different, viable approach that the doc dismisses by lumping it with A1

**Missed approach — "Work freely, report completely" (A2):**
```
You have full tool access. Read files, run tests, write code — do whatever you need.

When you are FINISHED, your final response message MUST contain the complete 
deliverable. Include all code you wrote (in fenced blocks), all findings, all 
analysis. Your response is the ONLY thing downstream phases will see.
```

This is NOT Approach A. The agent retains full tool access. It's just told that its final message is the deliverable. This is how OpenClaw sub-agents already work in practice (the subagent context says "your final response will be automatically reported to the main agent").

**Why this matters:** This approach requires ZERO infrastructure changes. No file conventions, no marker parsing, no fallback chains. Just better prompts. It should be Step 1.

**The D+E hybrid is Step 2** — for cases where A2's "comprehensive final message" isn't reliable enough (very large outputs, binary artifacts, etc.).

### Q2: Is the fallback chain (file → marker → text) robust enough?

**Mostly, but has gaps:**

1. **Empty file edge case:** What if the agent creates the output file but it's empty or contains only a header? Need minimum content validation, not just existence check.

2. **Partial writes:** If the agent errors out mid-write, you get a truncated file. The fallback to marker extraction would save you here, but only if the marker section was already written to the conversation.

3. **Multiple files:** The architecture assumes a single `output.md` per phase. What about phases that produce multiple artifacts (e.g., implementation writes to 5 source files)? The `output.md` would need to be a manifest/summary, not the literal output. This isn't addressed.

4. **Marker reliability:** The `## DELIVERABLE` marker is fragile. Agents may produce `## Deliverable`, `## DELIVERABLES`, `**Deliverable:**`, etc. The parser should be case-insensitive and handle common variations, or use a more distinctive marker like `<!-- PHASE_OUTPUT_START -->`.

5. **The fallback to "full last assistant text" is actually fine for most cases** — if you implement A2 (comprehensive final message prompting). The fallback chain becomes: file → marker → full text, and "full text" is already high quality because the prompt required it.

### Q3: Can we use `sessions_send` on completed sub-agent sessions? (Approach C)

**Yes — `sessions_send` exists and is a real gateway tool.** I confirmed it's in the `DEFAULT_TOOL_ALLOW` list:

```typescript
export declare const DEFAULT_TOOL_ALLOW: readonly [
  "exec", "process", "read", "write", "edit", "apply_patch", 
  "image", "sessions_list", "sessions_history", 
  "sessions_send",  // ← confirmed
  "sessions_spawn", "session_status"
];
```

Its description is: *"Send a message to another session/sub-agent"*

**However, the critical question remains: does it work on *completed* sessions?**

The gateway likely keeps session state (the session store has 57 entries per `openclaw health`). Sending to a completed session would need to:
1. Rehydrate the session context
2. Run another agent turn with the new message
3. Return the response

**This is plausible** given OpenClaw's architecture (sessions persist in `sessions.json`), but needs empirical testing. The document correctly flags this as needing investigation.

**Recommendation:** Test this with a simple experiment:
```bash
# 1. Spawn a sub-agent that does some work
# 2. Wait for it to complete
# 3. Send a follow-up via sessions_send asking for a summary
# 4. Check if it responds with context intact
```

If `sessions_send` works on completed sessions, it becomes the **cleanest approach** — no prompt engineering, no file conventions, no markers. Just ask the agent to summarize after it's done. The agent has full context of its own work.

**Risk:** Extra turn = extra latency + tokens. But this is a one-time cost per phase, and the summary quality would be excellent because the agent has its full conversation context.

### Q4: Are there simpler alternatives we overlooked?

**Yes — the document overlooks the simplest effective approach.**

**Priority-ordered implementation plan (simplest first):**

| Step | Approach | Effort | Reliability |
|------|----------|--------|-------------|
| **1** | A2: "Work freely, report completely" prompt engineering | 30 min | ~85% |
| **2** | Fix `_PhaseOutput` to extract `result.text` instead of `str(result_dict)` | 15 min | N/A (bug fix) |
| **3** | C: `sessions_send` summary request (if empirically confirmed) | 2 hrs | ~95% |
| **4** | D+E: File + marker hybrid (full implementation) | 4-6 hrs | ~95% |

The document jumps straight to Step 4. Steps 1-3 should be tried first.

### Q5: Should we start with explicit prompt instructions?

**Absolutely yes.** This should be the first thing implemented. It's the 80/20 solution.

The document's Approach A incorrectly bundles "don't use tools" with "produce comprehensive output." These are orthogonal. The correct framing is:

> **Phase agents have full tool access AND must produce comprehensive final output.**

This is already how OpenClaw's own subagent system works — the subagent context literally says: *"your final response will be automatically reported to the main agent."* The orchestration engine should follow the same pattern.

---

## Critical Bugs Found in Current Code

### Bug 1: `_PhaseOutput` wraps the wrong thing

```python
# sequencer.py line ~168
phase_kwargs: Dict[str, _PhaseOutput] = {
    pid: _PhaseOutput(str(pout))  # pout is result.model_dump() — the ENTIRE TaskResult dict
    for pid, pout in self.phase_outputs.items()
}
```

`str(pout)` produces something like:
```
{'task_id': 'abc123', 'task_type': 'content', 'state': 'success', 'confidence': 0.8, 'result': {'text': 'actual useful output'}, 'errors': [], 'started_at': '...', 'completed_at': '...', 'model_used': '...', ...}
```

When a downstream phase references `{implement.output}`, it gets this entire dict stringified, not just the useful text. **This is a bug independent of the output extraction problem.** The fix:

```python
phase_kwargs: Dict[str, _PhaseOutput] = {
    pid: _PhaseOutput(
        pout.get("result", {}).get("text", str(pout))
        if isinstance(pout, dict) else str(pout)
    )
    for pid, pout in self.phase_outputs.items()
}
```

This should be fixed immediately regardless of which output extraction approach is chosen.

### Bug 2: Polling logic has a race condition

```python
# openclaw_executor.py, _run_session polling loop
if has_text:
    # Extract all text blocks
    text_parts = []
    ...
    return "\n".join(text_parts), tokens_in + tokens_out
```

The executor returns as soon as it finds an assistant message with text. But the session might not be complete — the agent could be mid-work and has produced intermediate text before making more tool calls. **There's no check for session completion status.**

The polling should check `sessions_list` or `sessions_history` for a completion indicator (e.g., session status = "completed" or the final message has `stopReason: "end_turn"` without pending tool calls).

Current code checks `stopReason` but only in the `not has_text` branch — the wrong place.

### Bug 3: Missing `sessions_history` pagination

The executor fetches `limit: 5` messages. If the agent had a long conversation (many tool calls), the last 5 messages might all be tool responses. The actual final assistant text could be message #6 or #7. Either increase the limit significantly or paginate backward until finding the final assistant text message.

---

## What the Architecture Gets Right

1. **Problem diagnosis is excellent.** The two-problem framing (text-only extraction + lossy phase wrapping) is spot-on.
2. **Approach B rejection is correct.** Raw transcript forwarding would destroy context windows.
3. **Approach F (two-phase) is correctly deferred.** Elegant but expensive.
4. **The fallback chain concept is sound.** Having multiple extraction strategies with graceful degradation is good engineering.
5. **Template-level `output_strategy` field** is a good extensibility point.

---

## Recommended Changes

### Must-Do (before implementing D+E)

1. **Fix `_PhaseOutput` bug** — extract `result.text`, not `str(full_dict)`. (15 min)
2. **Implement A2 prompt engineering** — add a standard output instruction suffix to all phase prompts. (30 min)
3. **Fix polling race condition** — verify session completion before extracting output. (1 hr)
4. **Increase history limit** — `limit: 5` is too low. Use 20+ or paginate. (15 min)
5. **Test `sessions_send` on completed sessions** — empirical test, 30 min. If it works, implement Approach C before D+E.

### Should-Do (D+E implementation improvements)

6. **Use a machine-parseable marker** — `<!-- PHASE_OUTPUT_START -->` / `<!-- PHASE_OUTPUT_END -->` instead of `## DELIVERABLE`. Less likely to be mangled by agent formatting.
7. **Add content validation to file output** — minimum size check, not just existence.
8. **Document the multi-file artifact case** — phases that produce multiple files need a manifest/summary convention.
9. **Add output size metrics** — log output size per phase for monitoring. Critical for catching "I wrote the code" vs actual code output.

### Nice-to-Have

10. **Structured output schema per phase type** — define what each phase type should produce (code phase → `{files: [...], summary: "..."}`, review phase → `{verdict: "...", issues: [...]}`)
11. **Output quality scoring** — heuristic to detect "conversational" vs "substantive" output (e.g., output that's <200 chars for a code phase is suspicious)

---

## Verdict: **APPROVE_WITH_CHANGES**

The D+E hybrid architecture is sound in principle but the document:

1. **Skips simpler, higher-value first steps** (prompt engineering + bug fixes) that should be implemented before the full D+E infrastructure
2. **Has critical bugs in the current code** that will undermine ANY output extraction strategy
3. **Underexplores Approach C** (`sessions_send`) which is confirmed to exist and could be the cleanest solution
4. **Conflates "no tools" with "comprehensive output"** in its Approach A analysis, incorrectly dismissing prompt-based solutions

**Recommended implementation order:**
1. Fix the three bugs (PhaseOutput, polling race, history limit)
2. Implement A2 prompt engineering (work freely, report completely)
3. Empirically test `sessions_send` on completed sessions
4. If `sessions_send` works → implement Approach C
5. If not → implement D+E hybrid as designed (with the marker and validation improvements noted above)

The architecture document should be updated to reflect this phased approach rather than jumping straight to D+E.
