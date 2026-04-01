# Acceptance Test Summary

## Overview

11 behavioral acceptance tests written from `behavioral.md` contracts.
Tests are derived **exclusively** from the spec — no implementation was consulted.
Tests import from `/home/toscan/orchestration-engine/src` and use `pytest` + `unittest.mock`.

---

## Behavioral Contracts (one per test)

### Contract 1 — Daemon startup suppresses OpenClaw when DAEMON_ENABLED is unset
**Test:** `TestDaemonStartupSuppression::test_openclaw_suppressed_when_daemon_flag_unset`
**Rationale:** Validates the core happy-path contract: when `NOTIFY_OPENCLAW_DAEMON_ENABLED` is unset,
a `NotificationDispatcher` created via `from_env()` after the env override (`NOTIFY_OPENCLAW_ENABLED=""`)
does NOT call `_dispatch_openclaw`. This is the fundamental suppression behavior.

### Contract 2 — All daemon dispatchers suppressed via process-level env override
**Test:** `TestDaemonStartupSuppression::test_openclaw_env_forced_to_empty_suppresses_all_daemon_dispatchers`
**Rationale:** Validates that the suppression applies to ALL `from_env()` calls in the same process
(not just the first one). Three consecutive dispatchers are created and all must be suppressed.
This covers the "future code path" guarantee from the edge cases section.

### Contract 3 — Parent environment is unaffected by daemon subprocess suppression
**Test:** `TestDaemonStartupSuppression::test_parent_env_unaffected_by_daemon_suppression`
**Rationale:** Validates process isolation — when the daemon child process overrides
`NOTIFY_OPENCLAW_ENABLED`, the parent process's copy of the env var remains unchanged.
Tested via a real subprocess to exercise OS-level isolation.

### Contract 4 — Truthy NOTIFY_OPENCLAW_DAEMON_ENABLED values preserve OpenClaw
**Test:** `TestDaemonFlagOptIn::test_openclaw_enabled_when_daemon_flag_truthy`
**Rationale:** Validates the opt-in contract: values "1", "true", "yes" for
`NOTIFY_OPENCLAW_DAEMON_ENABLED` skip suppression and allow the OpenClaw backend to fire.
Parametrized across all three accepted truthy values.

### Contract 5 — Falsy/absent NOTIFY_OPENCLAW_DAEMON_ENABLED suppresses OpenClaw
**Test:** `TestDaemonFlagOptIn::test_openclaw_suppressed_when_daemon_flag_falsy`
**Rationale:** Validates that "false", "0", "no", "off", and absent all result in suppression.
The spec says: "any value other than '1', 'true', or 'yes'" — this parametrized test covers the boundary.

### Contract 6 — Telegram backend fires when OpenClaw is suppressed
**Test:** `TestOtherBackendsUnaffected::test_telegram_fires_when_openclaw_suppressed`
**Rationale:** Validates the spec requirement that Telegram is unaffected by OpenClaw suppression.
A dispatcher with `openclaw_enabled=False` and `telegram_enabled=True` must call Telegram, not OpenClaw.

### Contract 7 — Webhook backend fires when OpenClaw is suppressed
**Test:** `TestOtherBackendsUnaffected::test_webhook_fires_when_openclaw_suppressed`
**Rationale:** Same as above but for the Webhook backend. Validates Webhook independence from OpenClaw.

### Contract 8 — Both Telegram and Webhook fire together when OpenClaw is suppressed
**Test:** `TestOtherBackendsUnaffected::test_telegram_and_webhook_both_fire_when_openclaw_suppressed`
**Rationale:** Validates the combined case from the spec: "NOTIFY_TELEGRAM_ENABLED=true and
NOTIFY_WEBHOOK_ENABLED=true alongside the suppression, those backends are unaffected."

### Contract 9 — Dispatcher with openclaw_enabled=False never calls OpenClaw (any event)
**Test:** `TestDispatcherOpenClawSuppressedDirectly::test_dispatcher_with_openclaw_false_skips_openclaw_backend`
**Rationale:** Direct unit-level contract from the acceptance criteria. Tests `human_review`,
`auto_merge`, and `pipeline_failed` events to ensure suppression is event-agnostic.

### Contract 10 — Suppression failure logs WARNING and continues (non-fatal)
**Test:** `TestSuppressionFailureNonFatal::test_suppression_failure_logs_warning_and_continues`
**Rationale:** Validates the error handling contract: if the env override raises an exception,
the system must log `"Failed to suppress OpenClaw notifications (non-fatal)"` at WARNING level
and NOT abort the daemon. Simulates the exception-catching guard pattern.

### Contract 11 — Pipeline continues after suppression failure (other backends unaffected)
**Test:** `TestSuppressionFailureNonFatal::test_notification_dispatch_continues_after_suppression_failure`
**Rationale:** Extends Contract 10 — even in a suppression failure scenario, the Telegram and
Webhook backends must still receive events. The run must not be aborted.

### Contract 12 — Concurrent daemon subprocesses have isolated environments
**Test:** `TestProcessIsolation::test_concurrent_daemons_have_isolated_env`
**Rationale:** Validates the edge case: two concurrent `orch launch` subprocesses each independently
suppress their own env vars via OS-level process isolation. Neither affects the other or the parent.
Uses real `subprocess.Popen` to exercise true OS isolation.

### Contract 13 — Future from_env() callsites auto-suppressed by env override
**Test:** `TestFutureCodePathsAutomaticallySuppressed::test_new_from_env_calls_auto_suppressed_by_env_var`
**Rationale:** Validates the "no per-callsite change required" guarantee from edge cases.
Three simulated future callsites all call `from_env()` and are all suppressed by the single
process-level env var override — no individual callsite logic needed.

### Contract 14 — Web API process (non-daemon) OpenClaw path fires normally
**Test:** `TestWebApiProcessUnaffected::test_non_daemon_dispatcher_with_openclaw_enabled_fires_normally`
**Rationale:** Validates that `TelegramCallbackHandler._notify_openclaw()` (web API, not daemon)
is unaffected. A dispatcher created in a process without the daemon's env override fires OpenClaw normally.
This ensures the fix is scoped to daemon processes only.

### Contract 15 — WARNING always logged regardless of backend configuration
**Test:** `TestAlwaysLogsWarning::test_warning_always_logged_regardless_of_backends`
**Rationale:** Validates the documented behavior: `dispatch()` always emits a WARNING-level log
entry regardless of which backends are enabled (including all-disabled). This is a safety net
for observability.

---

## Ambiguities in the Spec That Required Assumptions

### 1. Where exactly does the suppression happen in run_daemon()?
The spec says "when `run_daemon()` initialises" but doesn't specify the exact line number.
**Assumption:** The suppression happens early in `run_daemon()`, before any `NotificationDispatcher.from_env()` call. Tests validate the *outcome* (dispatchers from `from_env()` have `openclaw_enabled=False`) rather than the *location*.

### 2. What constitutes "truthy" for NOTIFY_OPENCLAW_DAEMON_ENABLED?
The spec says: "any value other than '1', 'true', or 'yes'" is falsy.
**Assumption:** This mirrors the existing `_bool()` helper pattern used for `NOTIFY_OPENCLAW_ENABLED`. Tests are parametrized to cover the boundary precisely.

### 3. The spec references `daemon.py` line numbers (1517, 1722) which may shift
**Assumption:** Tests are written against behavior (events named `human_review` and `auto_merge`), not line numbers. Line numbers are implementation details that may change.

### 4. "Suppression failure" mechanism
The spec says suppression failure must log a specific WARNING message and continue. The exact guard pattern (try/except around `os.environ[...] = ""`) is not specified.
**Assumption:** The test simulates the guard pattern and validates that the correct warning message and non-fatal behavior are produced, without dictating the guard's internal structure.

### 5. TelegramCallbackHandler._notify_openclaw() scope
The spec mentions this method at `notifications.py` line 397 is NOT in daemon scope.
**Assumption:** The test validates this by confirming that a `from_env()` dispatcher created in a non-daemon context (with `NOTIFY_OPENCLAW_ENABLED=true`) fires the OpenClaw backend normally.
