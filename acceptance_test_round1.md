# Acceptance Test Phase — Issue #660

## Summary

Wrote 12 behavioral acceptance tests across 4 test classes, derived purely from the behavioral contracts in `behavioral.md`. Tests are pre-implementation and should be run after the implement phase.

## Behavioral Contracts Tested

### Happy Path (4 tests)

| # | Test | Contract | Rationale |
|---|------|----------|-----------|
| 1 | `test_dispatcher_openclaw_disabled_when_env_empty` | Given `NOTIFY_OPENCLAW_ENABLED=""`, `from_env()` returns `openclaw_enabled=False` | Core suppression mechanism — validates the env-to-flag translation |
| 2 | `test_dispatcher_openclaw_enabled_when_env_true` | Given `NOTIFY_OPENCLAW_ENABLED=true`, `from_env()` returns `openclaw_enabled=True` | Baseline sanity — confirms the flag works in both directions |
| 3 | `test_telegram_backend_unaffected_by_openclaw_suppression` | Telegram backend fires normally when OpenClaw is suppressed | Validates selective suppression — only OpenClaw is disabled |
| 4 | `test_webhook_backend_unaffected_by_openclaw_suppression` | Webhook backend fires normally when OpenClaw is suppressed | Validates selective suppression — webhooks remain active |

### Configuration (4 tests)

| # | Test | Contract | Rationale |
|---|------|----------|-----------|
| 5 | `test_daemon_enabled_true_preserves_openclaw` | `NOTIFY_OPENCLAW_DAEMON_ENABLED=true` skips suppression | Validates the opt-in escape hatch for daemon OpenClaw notifications |
| 6 | `test_daemon_enabled_absent_triggers_suppression` | Absent `NOTIFY_OPENCLAW_DAEMON_ENABLED` → suppression active | Default behavior — daemon suppresses without explicit opt-in |
| 7 | `test_daemon_enabled_falsy_values_trigger_suppression` | Empty, "0", "false", "no" etc. all trigger suppression | Boundary: ensures all non-truthy values are treated as falsy |
| 8 | `test_daemon_enabled_truthy_values_preserve_openclaw` | "1", "true", "yes" all preserve OpenClaw | Boundary: validates the exact truthy value set from the spec |

### Error Handling (1 test)

| # | Test | Contract | Rationale |
|---|------|----------|-----------|
| 9 | `test_suppression_failure_is_nonfatal` | Env-setting failure → WARNING log, no abort | Resilience: notification config errors must not crash pipeline runs |

### Edge Cases (3 tests)

| # | Test | Contract | Rationale |
|---|------|----------|-----------|
| 10 | `test_process_level_env_isolation` | Subprocess env changes don't affect parent | OS-level isolation: confirms concurrent daemons are independent |
| 11 | `test_from_env_called_multiple_times_respects_env` | Multiple `from_env()` calls all honor the env var | Future-proofing: new callsites automatically suppressed |
| 12 | `test_dispatcher_does_not_dispatch_openclaw_when_disabled` | Dispatcher with `openclaw_enabled=False` never calls OpenClaw backend | End-to-end: the flag actually prevents the dispatch, not just the flag |

## Assumptions Made (Spec Ambiguities)

1. **`NotificationDispatcher` attribute name**: The spec says `openclaw_enabled=False` — we assume the dispatcher exposes this as a public attribute `openclaw_enabled`. If it's a property or method, tests may need minor adjustment.

2. **`telegram_enabled` / `webhook_enabled` attributes**: We assume the dispatcher similarly exposes these as public attributes to verify selective suppression.

3. **Suppression function location**: The spec says suppression happens in `run_daemon()` initialization. We check for standalone helper functions (`suppress_openclaw_notifications` / `_suppress_openclaw_notifications`) but fall back to env-based testing if suppression is inline.

4. **Truthy value case sensitivity**: The spec says `"1"`, `"true"`, `"yes"` are truthy. We test these exact lowercase strings. If case-insensitive matching is intended (e.g., "TRUE", "Yes"), additional tests may be needed — but the spec lists only lowercase values.

5. **Dispatch method signature**: We don't know the exact signature of the dispatcher's `dispatch()` / `notify()` method. Tests handle `TypeError` gracefully if the signature doesn't match our guess.

## Test File Location

- Tests: `/tmp/output-660-v4/acceptance_tests.py`
- Results: `/tmp/output-660-v4/acceptance_results.json`

## Running

```bash
cd /home/toscan/orchestration-engine
pytest /tmp/output-660-v4/acceptance_tests.py -v
```

Tests should be run AFTER the implementation phase completes.
