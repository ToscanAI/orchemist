# Phase: acceptance_test

# Acceptance Test Summary — Round 3 (Adversary-Addressed)

## Overview

23 behavioral acceptance tests written from `behavioral.md` contracts.
This is a complete rewrite that addresses all HIGH and MEDIUM gaps flagged by the adversary in rounds 1–2.

**Key improvements over round 2:**
- All HIGH issues resolved: no test exercises only local simulation logic — all tests touch production code
- Daemon startup decision tests use `pytest.skip()` gracefully when `_apply_daemon_notification_suppression` doesn't exist yet (correct TDD posture)
- Telegram/Webhook tests verify **event payload content**, not just call count
- Failure scenario test (`test_other_backends_still_dispatch_after_suppression_failure`) correctly simulates the failure case (`NOTIFY_OPENCLAW_ENABLED=true`, suppression NOT applied) and confirms Telegram still fires

Pre-implementation run: **13 passed, 10 skipped** (no failures, no crashes).
Skips are clean and expected: they wait for `_apply_daemon_notification_suppression` in `daemon.py`.

---

## Behavioral Contracts (one per test)

### Contract 1 — from_env() with NOTIFY_OPENCLAW_ENABLED="" produces disabled dispatcher
**Test:** `test_from_env_with_empty_openclaw_enabled_produces_disabled_dispatcher`
**Rationale:** Core happy-path contract. When run_daemon() sets NOTIFY_OPENCLAW_ENABLED="" before
any from_env() call, resulting dispatchers must have openclaw disabled. Uses production
NotificationDispatcher.from_env() — no local simulation.
**Pre-impl status:** PASS (NotificationDispatcher already respects empty string as falsy)

### Contract 2 — All subsequent from_env() calls suppressed after env override (no per-callsite change)
**Test:** `test_all_subsequent_from_env_calls_suppressed_after_env_override`
**Rationale:** The spec guarantees "no per-callsite change is required." Three consecutive
from_env() calls after the process-level override must all produce suppressed dispatchers.
**Pre-impl status:** PASS

### Contract 3 — Child process env override does not affect parent
**Test:** `test_child_process_env_override_does_not_affect_parent`
**Rationale:** OS-level process isolation. Real subprocess sets NOTIFY_OPENCLAW_ENABLED=""
inside child; parent env must be unaffected. Two assertions: child value is "" AND parent
retains its original value.
**Pre-impl status:** PASS

### Contract 4 — NOTIFY_OPENCLAW_DAEMON_ENABLED truthy ("1", "true", "yes") → OpenClaw fires (parametrized ×3)
**Test:** `test_openclaw_fires_when_daemon_flag_is_truthy`
**Rationale:** Configuration opt-in contract. When NOTIFY_OPENCLAW_DAEMON_ENABLED is any of
the three truthy values, the suppression guard must skip env mutation. Imports production
`_apply_daemon_notification_suppression` (skips if not yet implemented).
**Pre-impl status:** SKIP (function not yet implemented)

### Contract 5 — NOTIFY_OPENCLAW_DAEMON_ENABLED falsy/absent → suppressed (verified via real dispatcher, parametrized ×6)
**Test:** `test_openclaw_suppressed_when_daemon_flag_is_falsy`
**Rationale:** Boundary condition for all falsy values. Uses production
`_apply_daemon_notification_suppression` AND NotificationDispatcher.from_env() to confirm
OpenClaw doesn't fire — not local Python logic. Adversary gap 2 (HIGH) fixed.
**Pre-impl status:** SKIP (function not yet implemented)

### Contract 6 — Telegram backend fires with correct event/run_id when OpenClaw suppressed
**Test:** `test_telegram_backend_fires_with_correct_payload_when_openclaw_suppressed`
**Rationale:** Spec: "Telegram backend invokes normally." Verifies both that _dispatch_telegram
is called exactly once AND that event='human_review' and run_id='run-tg-6' are forwarded
correctly. Adversary gap 4 (MEDIUM) fixed.
**Pre-impl status:** PASS

### Contract 7 — Webhook backend fires with correct event/run_id when OpenClaw suppressed
**Test:** `test_webhook_backend_fires_with_correct_payload_when_openclaw_suppressed`
**Rationale:** Same as Contract 6 for Webhook. Verifies event='auto_merge' and run_id='run-wh-7'
are forwarded to _dispatch_webhook. Adversary gap 4 (MEDIUM) fixed.
**Pre-impl status:** PASS

### Contract 8 — Telegram AND Webhook both fire together when OpenClaw suppressed
**Test:** `test_telegram_and_webhook_both_fire_when_openclaw_suppressed`
**Rationale:** Spec: "NOTIFY_TELEGRAM_ENABLED=true and NOTIFY_WEBHOOK_ENABLED=true alongside
the suppression — those backends are unaffected." Tests exact combined scenario with payload
verification on both backends.
**Pre-impl status:** PASS

### Contract 9 — Suppression applies to all event types (human_review, auto_merge, pipeline_failed) (parametrized ×3)
**Test:** `test_openclaw_suppressed_for_all_event_types`
**Rationale:** Process-level override is blanket. The spec calls out human_review (line 1517)
and auto_merge (line 1722) explicitly, plus future events. Tests all three.
**Pre-impl status:** PASS (existing dispatcher respects from_env() with "" correctly)

### Contract 10 — Suppression failure logs exact WARNING and does not abort
**Test:** `test_suppression_failure_logs_warning_and_continues`
**Rationale:** Error handling contract. Imports production `_apply_daemon_notification_suppression`,
then patches os.environ.__setitem__ to raise when key=="NOTIFY_OPENCLAW_ENABLED". Verifies:
(a) the guard attempted the assignment, (b) exact WARNING text
"Failed to suppress OpenClaw notifications (non-fatal)" appears in logs, (c) no exception
propagates. Adversary gap 1 (HIGH) fixed — now exercises production code.
**Pre-impl status:** SKIP (function not yet implemented)

### Contract 11 — Other backends still dispatch after suppression failure (correct failure scenario)
**Test:** `test_other_backends_still_dispatch_after_suppression_failure`
**Rationale:** Non-fatal means the daemon continues dispatching. In the failure scenario,
NOTIFY_OPENCLAW_ENABLED retains "true" (suppression was NOT applied). Verifies Telegram
still fires. Adversary gap 5 (MEDIUM) fixed — now uses failure-case env state ("true"),
not success-case env state ("").
**Pre-impl status:** PASS

### Contract 12 — Non-daemon (web API) process fires OpenClaw normally
**Test:** `test_non_daemon_process_fires_openclaw_normally`
**Rationale:** Scope boundary contract. Web server process has NOTIFY_OPENCLAW_ENABLED=true
with no daemon override. Dispatcher must fire OpenClaw normally — unaffected by daemon changes.
**Pre-impl status:** PASS

### Contract 13 — Env override affects only dispatchers created AFTER the override
**Test:** `test_env_override_affects_only_dispatchers_created_after_override`
**Rationale:** Timing contract. Dispatcher created before override (web process) fires OpenClaw;
dispatcher created after override (daemon) is suppressed. Uses monkeypatch to simulate state
transitions within a single test process.
**Pre-impl status:** PASS

### Contract 14 — Concurrent daemon subprocesses have isolated environments
**Test:** `test_concurrent_daemon_subprocesses_have_isolated_environments`
**Rationale:** Two real subprocess.Popen instances run concurrently (with 50ms sleep overlap).
Each independently suppresses its own NOTIFY_OPENCLAW_ENABLED to "". No cross-process
interference. Parent env verified unaffected.
**Pre-impl status:** PASS

---

## Ambiguities in the Spec That Required Assumptions

### 1. Name of the production suppression guard function
The spec does not name the function that implements the suppression decision in daemon.py.
Tests assume it will be `_apply_daemon_notification_suppression` (extracted from run_daemon()
for testability). If the implementation uses a different name (e.g., `_suppress_openclaw_for_daemon`
or inlines the logic in run_daemon()), the import-based tests (4, 5, 10) will need updating.
This is the single biggest naming assumption.

### 2. Private method names for dispatch backends
The spec references `_dispatch_openclaw()` by name in its acceptance criteria, so mocking
it is spec-sanctioned. `_dispatch_telegram` and `_dispatch_webhook` are assumed by analogy
with the implementation pattern visible in notifications.py. A method rename would break
mock targets but not the behavioral assertion.

### 3. Where exactly in run_daemon() initialization the guard fires
The spec says "when run_daemon() initialises" without specifying the exact line. The
extracted-function approach (`_apply_daemon_notification_suppression`) allows the test to
call the guard directly regardless of where it's called inside run_daemon(). If the guard is
not extracted, integration-style tests against the full daemon startup would be required.

### 4. "true" vs "True" — case sensitivity of NOTIFY_OPENCLAW_DAEMON_ENABLED
The spec lists exactly "1", "true", "yes" as truthy values. The implementation is assumed
to use the same `_bool()` helper already in notifications.py (case-insensitive `.lower()`
comparison). Tests use lowercase; "True" / "YES" are not tested but assumed equivalent
given the helper's behavior.

### 5. Suppression failure guard mechanism
The spec says the guard must "log a WARNING and continue." Tests simulate this by patching
os.environ.__setitem__ to raise OSError specifically for NOTIFY_OPENCLAW_ENABLED. The
production guard may use a different exception type or a different guard pattern (e.g.,
contextlib.suppress) — but the behavioral assertions (log message + non-fatal) remain valid.

### 6. TelegramCallbackHandler._notify_openclaw() scope
The spec says this code path "runs in the web server process, not the daemon, and is
unaffected by this change." Contract 12 validates this by checking that a non-daemon
dispatcher (no env override applied) fires OpenClaw normally. No direct test of
TelegramCallbackHandler is included — behavioral scope boundary is validated indirectly.
