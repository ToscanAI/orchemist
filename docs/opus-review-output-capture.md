# Architectural Review: Sub-Agent Output Capture in the Orchestration Engine

**Author:** Toscan (Architectural Review)  
**Date:** 2026-02-24  
**Scope:** OpenClaw Orchestration Engine v0.3.0 — sub-agent output extraction and phase chaining  
**Status:** PROPOSAL

---

## 1. Problem Analysis

### 1.1 The Core Gap

The orchestration engine's `OpenClawExecutor._run_session()` polls `sessions_history` and extracts **only the text blocks from the last assistant message**. For agents that primarily *talk about* what they're doing (analysts, reviewers), this captures the useful output. For agents that primarily *do things via tools* (builders, researchers), the conversational text is often a fragment like "I've written the implementation" while the actual code was written to the filesystem via `Write` tool calls.

### 1.2 The Double Problem

There are actually **two distinct issues**:

**Issue A — Text-only extraction misses tool-based work:**
```
Agent does: Write("src/foo.py", "...300 lines of code...")
Agent says: "I've implemented the feature in src/foo.py"
Orchestrator captures: "I've implemented the feature in src/foo.py"  ← useless for next phase
```

**Issue B — Phase output wrapping is lossy:**
The sequencer stores `result.model_dump()` (the full TaskResult dict) as the phase output, then wraps it with `_PhaseOutput(str(pout))`. This means downstream phases receive a Python dict repr like `{'task_id': '...', 'state': 'success', 'result': {'text': '...'}, ...}` — not clean text. The actual content is buried inside `result.text`.

### 1.3 Why This Matters

The code-development-pipeline.yaml has this chain:
```
requirements.output → implement prompt
implement.output → code_review prompt  
code_review.output → fix prompt
```

If `implement.output` is just "I wrote the code in these files" instead of the actual code, the code reviewer has nothing to review. The pipeline degrades to agents talking to each other about work that was done, rather than passing actual artifacts.

---

## 2. Analysis of Viable Approaches

### Approach A: Prompt Engineering — Force Text-Only Output

**Concept:** Modify phase prompts to instruct agents: "Output ALL results as text in your response. Do not write to files; instead, include all code, analysis, and artifacts directly in your message."

**How it works:**
- Add a standard suffix to every phase prompt: `"IMPORTANT: Your entire output must be in your response text. Do not use tools to write files."`
- Or add a template-level `output_instructions` field

**Tradeoffs:**

| Dimension | Assessment |
|-----------|-----------|
| Reliability | ⚠️ Medium — LLMs may still use tools, especially for complex multi-file implementations |
| Generality | ✅ Works for all pipeline types |
| Capability | ❌ **Severely limits agents** — they can't run tests, read existing code for context, check git status, etc. |
| Complexity | ✅ Trivial to implement |
| Token cost | ❌ Increases output tokens (agent must reproduce everything inline) |
| Fidelity | ⚠️ Agent summaries may lose detail vs actual tool outputs |

**Verdict:** This is the "easy but wrong" answer. It turns powerful tool-using agents into glorified chat completions. The whole point of OpenClaw agents is that they have tools.

---

### Approach B: History Mining — Extract Tool Call Results from Session History

**Concept:** After the session completes, fetch the full history (with `includeTools: true`) and parse both assistant text and tool call results to reconstruct the complete output.

**How it works:**
1. Poll `sessions_history` with `includeTools: true`
2. Walk all messages in order
3. For each assistant message, extract both `type: "text"` blocks and `type: "toolCall"` blocks
4. For each `role: "tool"` response, extract the returned content
5. Build a structured output: `{text: "...", tool_actions: [{tool, args, result}, ...], files_written: [...], files_read: [...]}`

**Implementation sketch:**
```python
def _extract_full_output(self, messages: List[dict]) -> dict:
    text_parts = []
    tool_actions = []
    files_written = []
    
    for msg in messages:
        if msg["role"] == "assistant":
            for c in msg.get("content", []):
                if c["type"] == "text":
                    text_parts.append(c["text"])
                elif c["type"] == "toolCall":
                    tool_actions.append({
                        "tool": c["name"],
                        "args": c.get("arguments", {}),
                    })
                    if c["name"] == "Write":
                        files_written.append(c["arguments"].get("path", ""))
        elif msg["role"] == "tool":
            # Attach result to last tool action
            if tool_actions:
                for tc in msg.get("content", []):
                    if tc["type"] == "text":
                        tool_actions[-1]["result"] = tc["text"]
    
    return {
        "text": "\n".join(text_parts),
        "tool_actions": tool_actions,
        "files_written": files_written,
    }
```

**Tradeoffs:**

| Dimension | Assessment |
|-----------|-----------|
| Reliability | ⚠️ Medium — history format may evolve; tool results can be huge |
| Generality | ✅ Works for any pipeline type |
| Capability | ✅ **Agents keep full tool access** |
| Complexity | ⚠️ Medium — need to parse multi-format history, handle edge cases |
| Token cost | ❌ **Explosion risk** — a builder agent that reads 10 files and writes 5 will have enormous history. Passing raw tool results to the next phase could exceed context limits |
| Fidelity | ✅ Perfect fidelity — captures everything |

**Verdict:** Technically correct but practically dangerous. A builder that reads 20 files generates megabytes of tool history. Naive forwarding to the next phase would blow up context windows. Needs intelligent filtering/summarization, which adds significant complexity.

---

### Approach C: Convention-Based File Output

**Concept:** Each phase is instructed to write its primary output to a known file path. The orchestrator reads that file after completion.

**How it works:**
1. The orchestrator creates a temp directory per pipeline run
2. Each phase prompt includes: `"Write your final output to /tmp/pipeline-{run_id}/phase-{phase_id}-output.md"`
3. After the session completes, the orchestrator reads that file
4. If the file doesn't exist, fall back to extracting text from history

**Template-level config:**
```yaml
phases:
  - id: implement
    output_file: "phase-output.md"  # relative to pipeline workspace
    prompt_template: |
      ... 
      Write your complete output (including all code) to: {output_path}
```

**Tradeoffs:**

| Dimension | Assessment |
|-----------|-----------|
| Reliability | ⚠️ Medium — agent might not write the file, or might write partial output |
| Generality | ✅ Works for any pipeline type |
| Capability | ✅ Agents keep full tool access AND can also write to filesystem |
| Complexity | ⚠️ Medium — need workspace management, file reading, cleanup |
| Token cost | ✅ **Excellent** — file can be any size, no context window concern |
| Fidelity | ⚠️ Depends on agent compliance |

**Verdict:** Elegant for file-producing phases (code, articles), but adds a convention that agents might not follow reliably. Also awkward for analysis/review phases that naturally produce text output.

---

### Approach D: Post-Completion Summary Request ⭐ RECOMMENDED

**Concept:** After the sub-agent completes its work, send a follow-up message to the same session asking for a structured summary of everything it did. The agent, having full context of its own conversation, can produce a comprehensive summary.

**How it works:**
1. Spawn the sub-agent, let it work normally (full tool access)
2. Detect completion (existing polling logic)
3. Send a follow-up message via `sessions_send`: "Provide a complete summary of your output. Include all code you wrote, all findings, all artifacts. Format as a self-contained document that someone without access to the filesystem could use."
4. Poll for the summary response
5. Use the summary as the phase output

**Implementation sketch:**
```python
def _run_session(self, prompt, model, thinking):
    # 1. Spawn and wait for completion (existing logic)
    session_key = self._spawn_session(prompt, model, thinking)
    self._wait_for_completion(session_key)
    
    # 2. Send summary request
    summary_prompt = self._build_summary_prompt(phase_type)
    self._invoke_tool("sessions_send", {
        "sessionKey": session_key,
        "message": summary_prompt,
    })
    
    # 3. Wait for summary response
    summary_text = self._wait_for_response(session_key)
    return summary_text
```

**Summary prompt variants by phase type:**
- **Analysis/Review:** "Provide your complete analysis as a self-contained document."
- **Code/Implementation:** "Provide all code you wrote, with file paths and complete contents. Include your rationale for key decisions."
- **Research:** "Provide your complete research findings with sources and confidence levels."
- **Generic:** "Provide a complete, self-contained summary of everything you produced in this session. Include all artifacts, code, findings, and decisions. The reader has no access to any files you created — include everything inline."

**Tradeoffs:**

| Dimension | Assessment |
|-----------|-----------|
| Reliability | ✅ **High** — the agent has full context of what it did; LLMs are excellent at summarization |
| Generality | ✅ Works for any pipeline type — just vary the summary prompt |
| Capability | ✅ **Agents keep full tool access** |
| Complexity | ✅ Low — one additional message + poll cycle |
| Token cost | ⚠️ Medium — summary request adds one more turn, but output is bounded |
| Fidelity | ✅ High — agent knows everything it did and can produce a coherent summary |
| Latency | ⚠️ Adds ~10-30s per phase for the summary turn |

**Verdict:** This is the sweet spot. It's how human engineers work — the builder does their work, then writes a handoff document for the reviewer. The agent has full context and can produce exactly what the next phase needs. No history parsing, no file conventions, no capability restrictions.

---

### Approach E: Hybrid — Summary Request + File Output Convention

**Concept:** Combine Approach D (summary request) with Approach C (file output) for maximum robustness.

**How it works:**
1. Agent works normally with full tool access
2. Phase prompt includes optional `output_path` instruction
3. After completion, send a tailored summary request
4. Orchestrator uses: file output (if exists) > summary response > last assistant text (fallback)

**Tradeoffs:**

| Dimension | Assessment |
|-----------|-----------|
| Reliability | ✅ Very high — multiple fallback layers |
| Generality | ✅ Works for everything |
| Complexity | ⚠️ Medium-high — more code paths, more edge cases |

**Verdict:** Over-engineered for v1. Start with Approach D, add file conventions later if needed.

---

### Approach F: Gateway-Level Output Capture (Upstream Change)

**Concept:** Modify the OpenClaw gateway to provide a `sessions_output` endpoint that returns a structured summary of what the agent produced, computed server-side.

**Tradeoffs:** Requires gateway changes (different repo, different release cycle). Not viable for near-term.

---

## 3. Tradeoffs Summary Table

| Approach | Reliability | Generality | Agent Capability | Complexity | Token Cost | Latency |
|----------|:-----------:|:----------:|:----------------:|:----------:|:----------:|:-------:|
| A. Prompt Engineering | ⚠️ | ✅ | ❌ Crippled | ✅ Trivial | ❌ High | ✅ None |
| B. History Mining | ⚠️ | ✅ | ✅ Full | ⚠️ Medium | ❌ Explosion risk | ✅ None |
| C. File Convention | ⚠️ | ✅ | ✅ Full | ⚠️ Medium | ✅ Low | ✅ None |
| **D. Summary Request** | **✅ High** | **✅** | **✅ Full** | **✅ Low** | **⚠️ Medium** | **⚠️ +10-30s** |
| E. Hybrid D+C | ✅ Very High | ✅ | ✅ Full | ⚠️ Med-High | ⚠️ Medium | ⚠️ +10-30s |
| F. Gateway Change | ✅ | ✅ | ✅ Full | ❌ Cross-repo | ✅ Low | ✅ None |

---

## 4. Recommended Approach: D — Post-Completion Summary Request

### 4.1 Why This Wins

1. **Mirrors the manual pattern.** When Toscan manually spawns a builder, the builder does its work then reports back. This is exactly that — automated.

2. **Zero capability loss.** Agents use all tools freely. The summary is an afterthought, not a constraint.

3. **Self-contained output.** The summary is designed for downstream consumption. No parsing, no file reading, no history reconstruction.

4. **Low implementation risk.** It's one additional HTTP call per phase. The existing spawn/poll infrastructure handles it.

5. **General purpose.** The summary prompt can be parameterized per phase type or per template. Works for code, content, research, translations — anything.

### 4.2 Key Design Decisions

**Q: Does `sessions_send` exist in the gateway API?**  
A: Need to verify. If not, we can use `sessions_spawn` with the same session key, or find the equivalent. The gateway tools list mentions `sessions_spawn`, `sessions_history`, `sessions_list`. A `sessions_send` (or `sessions_message`) tool may need to be confirmed. **If it doesn't exist**, the fallback is to include the summary instruction in the original prompt as a final section: "When you are done, provide a complete summary of everything you produced."

**Q: What if the agent's summary is too long for the next phase's context?**  
A: The summary is bounded by the model's output limit (~8K-16K tokens typically). If this is still too much, we can add a `max_output_tokens` parameter per phase, or a summarization post-processing step.

**Q: What about the _PhaseOutput wrapping issue (Issue B)?**  
A: Fix this regardless of which approach we pick. `_PhaseOutput` should receive the clean output text, not `str(result.model_dump())`.

---

## 5. Implementation Plan

### 5.1 Phase 1: Fix the Output Wrapping Bug (Issue B)

**File:** `src/orchestration_engine/sequencer.py`

**Current (broken):**
```python
# In _execute_and_wait:
return result.model_dump()  # Returns full TaskResult dict

# In _build_phase_input:
phase_kwargs = {
    pid: _PhaseOutput(str(pout))  # str() of a dict → ugly repr
    for pid, pout in self.phase_outputs.items()
}
```

**Fix:** Extract the clean text output from the result dict before wrapping.

```python
# New helper method in PhaseSequencer:
@staticmethod
def _extract_phase_text(result_dict: dict) -> str:
    """Extract the clean text output from a phase result dict."""
    # Primary: result.result.text (OpenClaw executor format)
    result_payload = result_dict.get("result", {})
    if isinstance(result_payload, dict):
        text = result_payload.get("text", "")
        if text:
            return text
    # Fallback: result.result as string
    if isinstance(result_payload, str):
        return result_payload
    # Last resort: stringify
    return str(result_payload)

# In _build_phase_input, change:
phase_kwargs = {
    pid: _PhaseOutput(self._extract_phase_text(pout))
    for pid, pout in self.phase_outputs.items()
}
```

**Effort:** 1 small PR, ~30 min.  
**Risk:** Low — pure bug fix, backward compatible.

### 5.2 Phase 2: Add Summary Request to OpenClawExecutor

**File:** `src/orchestration_engine/openclaw_executor.py`

**Changes:**

1. **Add `_build_summary_prompt()` method** that generates a phase-type-appropriate summary request.

2. **Add `_send_summary_request()` method** that sends a follow-up message to a completed session and waits for the response.

3. **Modify `_run_session()`** to call the summary request after detecting completion.

4. **Add `summary_prompt` parameter** to `execute()` for template-level customization.

```python
# Constants
DEFAULT_SUMMARY_PROMPT = """Your work is complete. Now provide a comprehensive handoff document.

Include ALL of the following that apply:
- Complete code you wrote (with file paths and full contents)
- Analysis findings and recommendations  
- Review results and issues found
- Research results with sources
- Any other artifacts produced

Format this as a self-contained document. The reader cannot access any files 
you created — include everything inline. Be thorough but structured."""

SUMMARY_PROMPTS_BY_TYPE = {
    "code": "Your implementation is complete. Provide ALL code you wrote, organized by file path. Include the complete file contents, not snippets. Also include a brief summary of what you built and any decisions you made.",
    "review": "Your review is complete. Provide your full review findings in a structured format. Include severity, location, issue description, and suggested fix for each finding.",
    "analysis": "Your analysis is complete. Provide your full analysis as a structured document with clear sections, findings, and recommendations.",
    "research": "Your research is complete. Provide all findings with sources, confidence levels, and a synthesis section.",
}

def _run_session(self, prompt, model, thinking, phase_type=None, summary_prompt=None):
    # 1. Spawn session
    session_key = self._spawn_and_get_key(prompt, model, thinking)
    
    # 2. Wait for initial completion
    initial_text, tokens = self._poll_until_complete(session_key)
    
    # 3. Send summary request (if sessions_send is available)
    try:
        final_prompt = (
            summary_prompt 
            or SUMMARY_PROMPTS_BY_TYPE.get(phase_type, DEFAULT_SUMMARY_PROMPT)
        )
        self._invoke_tool("sessions_send", {
            "sessionKey": session_key,
            "message": final_prompt,
        })
        summary_text, summary_tokens = self._poll_until_complete(session_key)
        return summary_text, tokens + summary_tokens
    except Exception as exc:
        # Fallback: use initial text if summary request fails
        logger.warning(f"Summary request failed, using initial output: {exc}")
        return initial_text, tokens
```

**Effort:** 1 PR, ~2-3 hours.  
**Risk:** Medium — depends on `sessions_send` availability. Need fallback path.

### 5.3 Phase 3: Template-Level Output Configuration

**File:** `src/orchestration_engine/templates.py` (PhaseDefinition model)

**Add optional fields:**
```yaml
phases:
  - id: implement
    # ... existing fields ...
    output_strategy: summary  # summary | text_only | file | auto (default: auto)
    summary_prompt: |          # optional custom summary prompt
      Provide all code you wrote with file paths...
```

```python
# In PhaseDefinition:
class PhaseDefinition(BaseModel):
    # ... existing fields ...
    output_strategy: str = "auto"  # summary, text_only, file, auto
    summary_prompt: Optional[str] = None
```

**The `auto` strategy:**
- If phase has `output_strategy: summary` → always send summary request
- If phase has `output_strategy: text_only` → use current behavior (last assistant text)
- If phase has `output_strategy: file` → read from designated output file  
- If `auto` → use summary request for `code` and `research` task types; use text_only for `review` and `analysis` (which naturally produce text output)

**Effort:** 1 PR, ~1-2 hours.  
**Risk:** Low — additive, backward compatible (defaults to `auto`).

### 5.4 Phase 4: Refactor _run_session into Clean Stages

**File:** `src/orchestration_engine/openclaw_executor.py`

The current `_run_session()` is a single long method mixing spawning, polling, and parsing. Refactor into:

```python
def _run_session(self, prompt, model, thinking, phase_config=None):
    session_key = self._spawn_session(prompt, model, thinking)
    initial_output = self._poll_for_completion(session_key)
    
    if self._should_request_summary(phase_config):
        summary = self._request_summary(session_key, phase_config)
        return summary if summary else initial_output
    
    return initial_output

def _spawn_session(self, prompt, model, thinking) -> str:
    """Spawn and return session key."""
    ...

def _poll_for_completion(self, session_key) -> Tuple[str, int]:
    """Poll until agent completes, return (text, tokens)."""
    ...

def _should_request_summary(self, phase_config) -> bool:
    """Determine if this phase needs a summary request."""
    ...

def _request_summary(self, session_key, phase_config) -> Optional[Tuple[str, int]]:
    """Send summary request and wait for response."""
    ...
```

**Effort:** 1 PR, ~1-2 hours.  
**Risk:** Low — pure refactor, behavior preserved.

---

## 6. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|:----------:|:------:|------------|
| `sessions_send` doesn't exist in gateway API | Medium | High | Verify API first. Fallback: embed summary instruction in original prompt as final section. |
| Agent ignores summary request or produces low-quality summary | Low | Medium | The summary prompt is carefully crafted. If output is poor, fall back to initial text. |
| Summary request adds latency | Certain | Low | ~10-30s per phase. Acceptable for pipelines that already take minutes per phase. |
| Summary output exceeds next phase's context window | Low | Medium | Add `max_output_tokens` to summary request. Or add a post-processing truncation step. |
| Breaking change to _PhaseOutput behavior | Low | Medium | The current behavior is already broken (passing dict repr). Fix is strictly an improvement. |
| Gateway tool API changes format | Low | Medium | Abstract tool invocation behind a clean interface. Version-detect if needed. |

---

## 7. Effort Estimate

| Phase | Scope | Effort | Priority |
|-------|-------|--------|----------|
| **Phase 1:** Fix _PhaseOutput wrapping | Bug fix in sequencer.py | 1 PR, 30 min | P0 — fix now |
| **Phase 2:** Summary request in executor | Core feature in openclaw_executor.py | 1 PR, 2-3 hours | P0 — core value |
| **Phase 3:** Template output_strategy | Schema extension in templates.py | 1 PR, 1-2 hours | P1 — nice to have |
| **Phase 4:** Refactor _run_session | Code quality in openclaw_executor.py | 1 PR, 1-2 hours | P2 — cleanup |
| **Total** | | **4 PRs, ~6 hours** | |

---

## 8. Verification Plan

### 8.1 Gateway API Check
Before implementing Phase 2, verify:
```bash
# Check if sessions_send exists
curl -X POST http://localhost:18789/tools/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool": "sessions_send", "args": {"sessionKey": "test", "message": "test"}}'
```

If `sessions_send` doesn't exist, check `sessions_message` or look at gateway `/tools/list`.

### 8.2 Integration Test
```python
def test_summary_request_captures_tool_output():
    """Verify that a builder agent's file writes are captured in the summary."""
    executor = OpenClawExecutor(gateway_url="http://localhost:18789")
    prompt = "Write a Python function that calculates fibonacci numbers. Save it to /tmp/test_fib.py"
    
    # With summary request
    output_with_summary = executor._run_session(prompt, "anthropic/claude-sonnet-4-6", None, phase_type="code")
    
    # The output should contain actual code, not just "I wrote the file"
    assert "def fibonacci" in output_with_summary[0] or "def fib" in output_with_summary[0]
```

### 8.3 Pipeline E2E Test
Run the code-development-pipeline with a simple task and verify that:
1. `implement.output` in the code_review prompt contains actual code
2. `code_review.output` in the fix prompt contains actual review findings
3. The fix phase can meaningfully act on the review

---

## 9. Future Considerations

### 9.1 Structured Output Protocol
Long-term, define a structured output format that agents produce:
```json
{
  "summary": "Brief description of what was done",
  "artifacts": [
    {"type": "code", "path": "src/foo.py", "content": "..."},
    {"type": "analysis", "content": "..."}
  ],
  "decisions": ["Chose X over Y because..."],
  "warnings": ["Watch out for..."]
}
```

This would require prompt standardization across all templates but would enable richer pipeline behavior (e.g., auto-committing code artifacts, generating diffs).

### 9.2 Shared Workspace
Instead of passing output via text, phases could share a workspace directory. The orchestrator would mount a shared volume and each phase would read/write to it. This is the Docker/CI approach and works well for code pipelines but poorly for content/research pipelines.

### 9.3 Gateway `sessions_output` Endpoint
Propose to the OpenClaw gateway team: a `sessions_output` tool that returns a structured summary of what the agent produced, computed from the session history server-side. This would eliminate the summary request latency and provide a standard interface.

---

## 10. Conclusion

**Recommended approach: Post-Completion Summary Request (Approach D).**

This is the minimum-viable change that closes the output capture gap while preserving full agent capability. It mirrors how human engineers hand off work, requires minimal code changes (one additional message per phase), and works for all pipeline types.

Start with Phase 1 (fix the wrapping bug — 30 min) and Phase 2 (summary request — 2-3 hours). These two changes together will transform the orchestration engine from "agents talking about work" to "agents doing work and handing off results."
