## User Story
As a pipeline operator, I want the pipeline daemon to never push `sessions_send` events to the Claude Code session so that intermediate pipeline events (`human_review`, `auto_merge`) don't trigger rogue agent spawning in the TUI or IDE.

## Context
When `orch launch` runs the background daemon, `NotificationDispatcher._dispatch_openclaw()` in `src/orchestration_engine/notifications.py` calls the gateway's `POST /api/v1/sessions/send` endpoint targeting session `agent:main:main` — the Claude Code / TUI session. Claude Code receives these messages as actionable context and autonomously spawns agents that interfere with the running pipeline, causing merge conflicts and corrupted repository state. This has happened multiple times.

The daemon sends two event types via this path:
1. `human_review` — dispatched at `src/orchestration_engine/daemon.py` line 1517 inside `_dispatch_routing_action()` when the routing engine selects the `human_review` tier.
2. `auto_merge` — dispatched at `src/orchestration_engine/daemon.py` line 1722 inside the auto-merge success path after `gh pr merge`.

Both call `NotificationDispatcher.from_env()` which reads `NOTIFY_OPENCLAW_ENABLED` from the environment. The daemon inherits this env var from the parent process (the `orch launch` CLI) via `subprocess.Popen` without an `env=` override. There is no mechanism to disable the OpenClaw notification backend specifically for the daemon while keeping it enabled for interactive use.

The fix: at daemon startup, force-clear `NOTIFY_OPENCLAW_ENABLED` so all `NotificationDispatcher.from_env()` calls within the daemon process suppress the OpenClaw `sessions_send` backend. Telegram and Webhook backends remain active — those notify humans, not AI agents. An opt-in env var `NOTIFY_OPENCLAW_DAEMON_ENABLED` allows operators to re-enable OpenClaw notifications from daemon runs if explicitly desired.

Note: `TelegramCallbackHandler._notify_openclaw()` at `src/orchestration_engine/notifications.py` line 397 is intentionally NOT changed — that sends human-initiated confirmations (Telegram button presses) to Claude Code, which is correct behavior.

### Current flow (broken)

[Claude Code / TUI] ←── sessions_send ── [Daemon: NotificationDispatcher._dispatch_openclaw()]
↑ reacts, spawns rogue agents │
human_review / auto_merge events

### Target flow (fixed)

[Claude Code / TUI] (no sessions_send from daemon)
[Daemon: NotificationDispatcher]
│ openclaw backend disabled at process level
├─ Telegram backend → still active (notifies humans)
└─ Webhook backend → still active (notifies humans)

## Integration points
- Modifies: `src/orchestration_engine/daemon.py` — add env var suppression near top of `run_daemon()`, after logging setup but before PID file write (approx. line 186)
- Modifies: `src/orchestration_engine/notifications.py` — add `NOTIFY_OPENCLAW_DAEMON_ENABLED` to the `from_env()` docstring (documentation only, no logic change)
- New files: `tests/test_daemon_notifications.py` — unit tests for suppression, opt-in, backend selectivity, and process isolation
