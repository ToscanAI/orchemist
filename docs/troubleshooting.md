# Troubleshooting Guide

Common issues, their root causes, and how to fix them.

---

## Preflight Fails: "Working tree has N uncommitted change(s)"

**Symptoms:**
- Pipeline fails immediately on launch with `Preflight FAILED (Definition of Ready not met)`
- Error: `[git_clean] Working tree has 1 uncommitted change(s)`
- `git status --short` shows clean when checked manually

**Root Cause:**
The daemon writes `.orch-daemon.pid` and `.orch-daemon.log` to `output_dir` **before** running the preflight `git_clean` check. When `output_dir` is set to the same path as the repo being checked, these daemon artifacts appear as uncommitted changes to `git status --porcelain`. The PID file is cleaned up on daemon exit, so checking manually after the failure shows a clean tree — making this hard to diagnose.

**Fix:**
- **Never set `output_dir` to the repo path.** Use `/tmp/output-{issue}` or a unique directory outside the repo.
- This is the same principle as "Stale Output Directory Contamination" below — `output_dir` should always be a fresh, isolated directory.

**Example:**
```bash
# BAD — output_dir equals repo_path, daemon artifacts trip preflight
orch launch template.yaml --output-dir /home/user/my-repo

# GOOD — output_dir is separate from repo
orch launch template.yaml --output-dir /tmp/output-638
```

**History:** This caused 4 consecutive preflight failures on 2026-03-22. Every previous successful coding pipeline had used `/tmp/output-*` as the output directory. The bug was latent — it only triggers when `output_dir == repo_path`.

---

## Stale Output Directory Contamination

**Symptoms:**
- Spec agent reads files instead of writing specs
- Agent output looks like it's "dumping" old content
- Pipeline scores on incomplete work
- Same issue succeeds on retry with a different output directory

**Root Cause:**
Output directories from previous pipeline runs contain phase output files (`spec.md`, `spec_adversary.md`, `implement.md`, etc.). When a new run reuses the same directory, agents read stale files from the previous run and get confused.

The spec prompt includes instructions like "If a prior adversary review exists at `{output_dir}/spec_adversary.md`, read it" — this is designed for adversary iteration *within the same run*, but when stale files exist from a *different run*, the agent reads the wrong context.

**Fix:**
- **Always use unique output directories.** Never reuse a directory from a previous run.
- **Clean `/tmp/output-*` between runs** if reusing similar directory names.
- Use `mkdir -p /tmp/output-{issue}-{timestamp}` or let the CLI generate unique dirs.

**Example of the problem:**
```bash
# Run 1: completes, writes spec.md, implement.md, etc.
orch launch template.yaml --output-dir /tmp/output-468

# Run 2: reuses same dir — spec agent reads Run 1's stale files
orch launch template.yaml --output-dir /tmp/output-468  # BAD

# Run 2: fresh dir — spec agent starts clean
orch launch template.yaml --output-dir /tmp/output-468-v2  # GOOD
```

**History:** This caused 5 consecutive "failures" on issue #468 (2026-03-17). The first retry reused the output directory from the previous day's completed run. The stale `spec_adversary.md` and `spec.md` files caused the agent to read old content instead of producing a new spec. The issue was misdiagnosed as Sonnet capability issues, files_context quality, and pipeline architecture problems before the real root cause was found.

---

## Agent Dumps File Contents Instead of Synthesizing

**Symptoms:**
- Sub-agent output is raw source code instead of a spec/review
- TUI shows the agent reading files and outputting them verbatim

**Possible Causes:**
1. **Stale output directory** (see above) — most common
2. **Insufficient time** — spec agents can take 5-10 minutes on complex issues; don't kill before that
3. **Tool call output captured as result** — the executor captures the full conversation including tool call results; this is normal but can look like "dumping" in the TUI

**Diagnosis:**
- Check if the output directory has files from a previous run: `ls -la /tmp/output-{issue}/`
- Check the daemon log for actual completion: `orch logs {run_id} | grep "phase.*completed"`
- Check token count — if >0 tokens consumed, the agent is working (tool calls don't show as token progress)

---

## "No Token Progress for 60s" Warning

**Symptoms:**
- Log shows: `Session xxx: no token progress for 60s — possible rate limit`
- Pipeline appears stuck

**Possible Causes:**
1. **Agent is using tools** — file reads, web searches, etc. don't produce output tokens. This is normal.
2. **Anthropic API outage** — check [status.claude.com](https://status.claude.com)
3. **Actual rate limiting** — HTTP 429 responses (rare with subscription tokens)

**Diagnosis:**
- Wait at least 5 minutes before assuming failure
- If the agent eventually produces output, the warning was a false alarm
- Check Anthropic status page for active incidents
- This warning is known to be misleading (see issue #581)

---

## Pipeline Scores Incomplete Work

**Symptoms:**
- Score seems too low or too high for the actual output
- Pipeline says "SUCCESS: N phases completed" but N is less than expected

**Possible Causes:**
1. **Phase failed with no `failed` transition** — pipeline terminates and scores whatever it has
2. **Stale output directory** — scorer evaluates old files mixed with new ones

**Fix:**
- Ensure all phases have `failed` transitions (fixed in coding-pipeline-v1 as of 2026-03-17, issue #602)
- Always use clean output directories

---

## Writing Good Pipeline Inputs

See [templates/CONTEXT_GUIDE.md](../templates/CONTEXT_GUIDE.md) for detailed guidance on writing effective `files_context`.

**Key rule:** If the spec agent would need to `cat` a file to understand an interface, put that interface in `files_context` instead.

**Minimum for success:**
- Actual function/class signatures (not descriptions)
- Import patterns used in the codebase
- Test patterns to follow
- Line numbers for code that needs modification
