# Pipeline Monitoring Guide

> This guide is the command reference for checking on pipeline runs after they start.  
> For how to start a run, see [docs/tutorial.md](tutorial.md).

Orchemist maintains a persistent run database so you can check status, stream live output, and retrieve logs for any pipeline run — even after your terminal session has ended.

---

## Finding Your Run ID

Most monitoring commands take a run ID. If you don't know yours:

```bash
orch status
```

With no arguments, `orch status` lists the last 10 pipeline runs:

```
Example output (illustrative):
Recent Pipeline Runs (last 10)
────────────────────────────────────────────────────────────────────────
RUN ID     | TEMPLATE         | STATUS    | CREATED AT          | ELAPSED
abc123     | hello-pipeline   | completed | 2026-03-18 12:00:00 | 3.1s
def456     | content-pipeline | running   | 2026-03-18 11:58:00 | 2m14s
```

Copy the run ID from the first column and use it in subsequent commands.

---

## Run Status

### List recent runs

```bash
orch status
```

Returns the 10 most recent pipeline runs with their IDs, template names, statuses, and elapsed time. Use this when you want to find a run ID or quickly scan recent activity.

### Phase breakdown for a specific run

```bash
orch status abc123
```

Returns a phase-by-phase breakdown for run `abc123`, including the status, timing, and result summary for each phase:

```
Example output (illustrative):
Run: abc123
Template: hello-pipeline
Status: completed
─────────────────────────────────────────
Phase       | Status    | Elapsed | Model
hello       | completed | 2.8s    | haiku-4-5
─────────────────────────────────────────
Total: 2.8s
```

Use this to pinpoint which phase is slow, failed, or still running.

---

## Real-Time Watching

```bash
orch watch abc123
```

Streams live phase transitions, scoring results, warnings, and errors as they happen. The command exits automatically when the pipeline reaches a terminal state (completed, failed, or cancelled).

Use `orch watch` when:
- A pipeline is actively running and you want to follow progress without polling `orch status` manually
- You want to see errors as they happen rather than after the fact

```
Example output (illustrative):
[12:01:03] 🔄 Phase 'research' started
[12:01:18] ✓  Phase 'research' completed (15.2s)
[12:01:18] 🔄 Phase 'write' started
[12:01:34] ✓  Phase 'write' completed (16.1s)
[12:01:34] ✅ Pipeline completed in 31.3s
```

For machine-readable output (e.g. in CI):

```bash
orch watch abc123 --json
```

---

## Logs

### All logs for a run

```bash
orch logs abc123
```

Prints the full daemon log for run `abc123`. This includes phase-level output, executor messages, retry attempts, and any errors.

### Follow logs in real time

```bash
orch logs abc123 --follow
```

Tails the log file continuously (like `tail -f`). Press `Ctrl+C` to stop. Useful when you want the raw daemon log stream rather than the structured watch view.

> **Note:** The spec for this guide listed a `--phase <name>` option for `orch logs`. After verifying against the CLI source, the actual flag is `--follow` (tail mode), not `--phase`. Phase-specific output is available in the output directory at `phase_outputs/<phase-name>.md` after the run completes.

---

## Human Review Queue

```bash
orch reviews list
```

Lists pipeline runs that are paused pending human review. The review queue is populated when a pipeline phase is configured with `human_review: true` and the pipeline has reached that phase.

```
Example output (illustrative):
Pending reviews: 2 total  (showing 2  offset=0)

RUN ID  | TEMPLATE          | CREATED AT          | SCORE  | TIER
abc123  | content-pipeline  | 2026-03-18 10:00:00 | 0.7812 | sonnet
def456  | coding-pipeline   | 2026-03-18 09:45:00 | n/a    | haiku
```

To approve or reject a review:

```bash
orch reviews approve abc123
orch reviews reject abc123 "reason text"
```

---

## Chained Run Status

```bash
orch chain abc123
```

Shows the full chain of pipeline runs starting from the root of the chain that contains run `abc123`. Useful when one pipeline triggers another (e.g. a coding pipeline spawns a documentation run).

```
Example output (illustrative):
(Showing chain from root: abc123)
abc123 → def456 → ghi789
[root: completed] → [child: completed] → [grandchild: running]
```

To list all currently active chains across all runs:

```bash
orch chain --active
```

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `orch status` | List the last 10 pipeline runs |
| `orch status abc123` | Phase-by-phase status for run `abc123` |
| `orch watch abc123` | Stream live updates for a running pipeline |
| `orch watch abc123 --json` | Machine-readable live event stream |
| `orch logs abc123` | Print full daemon log for a run |
| `orch logs abc123 --follow` | Tail the log file in real time |
| `orch reviews list` | Show pending human review queue |
| `orch reviews approve abc123` | Approve a run pending review |
| `orch reviews reject abc123 "reason"` | Reject a run pending review |
| `orch chain abc123` | Show full chain status for a run |
| `orch chain --active` | List all currently active chains |

> All run IDs in this guide (e.g. `abc123`, `def456`) are **illustrative placeholders**. Your actual run IDs will be different.

---

## Where to Go Next

- **Starting a pipeline:** [docs/tutorial.md](tutorial.md)
- **Authoring pipeline templates:** [docs/template-authoring.md](template-authoring.md)
- **Full CLI reference:** [docs/api-reference.md](api-reference.md)
- **Troubleshooting:** [docs/GETTING_STARTED.md#troubleshooting](GETTING_STARTED.md#troubleshooting)
