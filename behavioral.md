## Behavioral Contracts

### Happy path
- Given the daemon starts (any mode: standalone, openclaw, or dry-run) and `NOTIFY_OPENCLAW_DAEMON_ENABLED` is unset, when `run_daemon()` initialises, the system sets `os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""` so that all subsequent `NotificationDispatcher.from_env()` calls in the daemon process create dispatchers with `openclaw_enabled=False`.
- Given the daemon dispatches a `human_review` event at `daemon.py` line 1517, the system invokes Telegram and Webhook backends normally but does NOT call `_dispatch_openclaw()` (no `sessions_send` to `agent:main:main`).
- Given the daemon dispatches an `auto_merge` event at `daemon.py` line 1722, the system invokes Telegram and Webhook backends normally but does NOT call `_dispatch_openclaw()`.
- Given `NOTIFY_OPENCLAW_ENABLED=true` is set in the parent shell environment, when the daemon subprocess starts, the system overrides it to `""` within the daemon process — the parent environment is unaffected.

### Configuration
- Given `NOTIFY_OPENCLAW_DAEMON_ENABLED=true` is explicitly set in the environment, the system skips the suppression — `NOTIFY_OPENCLAW_ENABLED` retains its inherited value and the OpenClaw backend fires from the daemon as it does today.
- Given `NOTIFY_OPENCLAW_DAEMON_ENABLED` is absent, empty, or set to any value other than `"1"`, `"true"`, or `"yes"`, the system suppresses OpenClaw notifications from the daemon.
- Given `NOTIFY_TELEGRAM_ENABLED=true` and `NOTIFY_WEBHOOK_ENABLED=true` alongside the suppression, those backends are unaffected — only the OpenClaw backend is suppressed.

### Error handling
- Given setting `os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""` raises an unexpected exception, the system logs a WARNING (`"Failed to suppress OpenClaw notifications (non-fatal)"`) and continues — the pipeline run is not aborted for a notification configuration error.

### Edge cases
- Given two `orch launch` invocations run concurrently, each daemon subprocess independently suppresses its own `os.environ["NOTIFY_OPENCLAW_ENABLED"]`. Process-level env vars are isolated by the OS; no cross-process interference occurs.
- Given a future code path in `daemon.py` adds a new `NotificationDispatcher.from_env()` call, the process-level env var override suppresses it automatically — no per-callsite change is required.
- Given `TelegramCallbackHandler._notify_openclaw()` at `notifications.py` line 397 is invoked (from the web API process, not the daemon), the system sends the `sessions_send` message to Claude Code as before — that code path runs in the web server process, not the daemon, and is unaffected by this change.

## Acceptance Criteria
- [ ] Daemon startup force-sets `NOTIFY_OPENCLAW_ENABLED=""` when `NOTIFY_OPENCLAW_DAEMON_ENABLED` is unset or falsy
- [ ] `NOTIFY_OPENCLAW_DAEMON_ENABLED=true` opt-in preserves inherited `NOTIFY_OPENCLAW_ENABLED` value (re-enables OpenClaw notifications from daemon)
- [ ] `NotificationDispatcher` with `openclaw_enabled=False` does not call `_dispatch_openclaw()` / `sessions_send` (unit test)
- [ ] Telegram backend still fires from daemon when OpenClaw backend is suppressed (unit test)
- [ ] Webhook backend still fires from daemon when OpenClaw backend is suppressed (unit test)
- [ ] Suppression failure is non-fatal: daemon logs WARNING and continues (unit test)
- [ ] `TelegramCallbackHandler._notify_openclaw()` behavior is unchanged (not in daemon process scope)
- [ ] All existing notification tests in `tests/test_review_queue.py` and `tests/test_hitl_telegram.py` pass without modification
