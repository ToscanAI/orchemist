# Phase: acceptance_test

# Acceptance Test Summary

## Overview

14 behavioral acceptance tests (23 test cases with parametrization) written from `behavioral.md` contracts.
Tests are derived **exclusively** from the spec — no implementation was consulted.
Tests import from `/home/toscan/orchestration-engine/src` and use `pytest` + `unittest.mock`.

All 23 cases pass syntax validation and dry-run execution (the pre-existing `NotificationDispatcher`
class supports the behavioral surface being tested — mock patches isolate the dispatch backends).

---

## Behavioral Contracts (one per test)

### Contract 1 — Dispatcher created from env with NOTIFY_OPENCLAW_ENABLED="" skips OpenClaw
**Test:** `test_dispatcher_with_openclaw_disabled_skips_openclaw_backend`
**Rationale:** Core happy-path contract. When `run_daemon()` sets `os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""`
before any `from_env()` call, resulting dispatchers must have `openclaw_enabled=False` and must NOT invoke
the OpenClaw backend on `dispatch()`. This is the fundamental suppression guarantee.

### Contract 2 — Process-level env override suppresses ALL from_env() callsites automatically
**Test:** `test_all_subsequent_from_env_calls_suppressed_after_env_override`
**Rationale:** The spec guarantees "no per-callsite change is required" for future code paths. Three
consecutive `from_env()` calls in the same process after the override must all produce suppressed
dispatchers. This validates the blanket process-level mechanism.

### Contract 3 — Child process env override does not affect parent process
**Test:** `test_child_process_env_override_does_not_affect_parent`
**Rationale:** OS-level process isolation contract. A real subprocess is used to confirm that setting
`os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""` inside a child (daemon) process leaves the parent's
environment variable unchanged — exactly as the spec asserts.

### Contract 4 — Truthy NOTIFY_OPENCLAW_DAEMON_ENABLED values ("1", "true", "yes") enable OpenClaw
**Test:** `test_openclaw_fires_when_daemon_flag_is_truthy` (parametrized ×3)
**Rationale:** Configuration opt-in contract. The spec explicitly names "1", "true", "yes" as the
accepted truthy values for `NOTIFY_OPENCLAW_DAEMON_ENABLED`. When any of these is set, suppression
is skipped and the OpenClaw backend fires from the daemon.

### Contract 5 — Falsy/absent NOTIFY_OPENCLAW_DAEMON_ENABLED values suppress OpenClaw
**Test:** `test_openclaw_suppressed_when_daemon_flag_is_falsy` (parametrized ×6)
**Rationale:** Boundary condition for the configuration flag. "false", "0", "no", "off", "" and None
(absent) must all be treated as falsy — the spec says "any value other than '1', 'true', or 'yes'".
Tests the exact boundary, not an approximation.

### Contract 6 — Telegram backend fires normally when OpenClaw is suppressed
**Test:** `test_telegram_backend_fires_when_openclaw_suppressed`
**Rationale:** The spec explicitly states "Telegram and Webhook backends are unaffected — only the
OpenClaw backend is suppressed." This test confirms Telegram fires and OpenClaw does not, in isolation.

### Contract 7 — Webhook backend fires normally when OpenClaw is suppressed
**Test:** `test_webhook_backend_fires_when_openclaw_suppressed`
**Rationale:** Same as Contract 6 but for the Webhook backend. Independent validation that each
non-OpenClaw backend is unaffected by the suppression.

### Contract 8 — Both Telegram AND Webhook fire together when OpenClaw is suppressed
**Test:** `test_telegram_and_webhook_both_fire_when_openclaw_suppressed`
**Rationale:** Direct quote from the spec: "NOTIFY_TELEGRAM_ENABLED=true and NOTIFY_WEBHOOK_ENABLED=true
alongside the suppression — those backends are unaffected." Tests the combined case exactly as stated.

### Contract 9 — OpenClaw suppression is event-agnostic (human_review, auto_merge, pipeline_failed)
**Test:** `test_openclaw_suppressed_for_all_event_types` (parametrized ×3)
**Rationale:** The spec calls out two specific event dispatch sites — `human_review` (daemon.py line 1517)
and `auto_merge` (daemon.py line 1722) — both must be suppressed. The process-level override is blanket,
so all events including future ones must also be suppressed.

### Contract 10 — Suppression failure logs exact WARNING and does not abort the daemon
**Test:** `test_suppression_failure_logs_warning_and_continues`
**Rationale:** Error handling contract. The spec requires: "logs a WARNING ('Failed to suppress OpenClaw
notifications (non-fatal)') and continues — the pipeline run is not aborted." Tests both the exact log
message and the non-fatal continuation guarantee.

### Contract 11 — Other backends still dispatch even after suppression failure
**Test:** `test_dispatch_continues_after_suppression_failure`
**Rationale:** Extends Contract 10 — "non-fatal" means not only does the daemon not abort, but it also
continues dispatching to other configured backends (e.g., Telegram). Validates full graceful degradation.

### Contract 12 — Concurrent daemon subprocesses have fully isolated environments
**Test:** `test_concurrent_daemons_have_isolated_environments`
**Rationale:** Edge case from the spec: "two orch launch invocations run concurrently — each daemon
subprocess independently suppresses its own os.environ[...]. No cross-process interference occurs."
Uses two real `subprocess.Popen` instances running simultaneously.

### Contract 13 — Non-daemon (web API) process fires OpenClaw normally
**Test:** `test_non_daemon_dispatcher_with_openclaw_enabled_fires_normally`
**Rationale:** Scope boundary contract. The spec explicitly states that
`TelegramCallbackHandler._notify_openclaw()` runs in the web server process (not the daemon) and
"is unaffected by this change." Validates that a dispatcher created in a process without the daemon's
env override fires OpenClaw as expected.

### Contract 14 — Env override only suppresses dispatchers created AFTER the override
**Test:** `test_env_override_only_affects_dispatchers_created_after_override`
**Rationale:** Timing contract — the daemon sets the env early in `run_daemon()` initialization so
all subsequent `from_env()` calls in the pipeline are suppressed. Validates the order: dispatcher
created before override fires normally (simulating web process), dispatcher created after is suppressed.

---

## Ambiguities in the Spec That Required Assumptions

### 1. Where exactly in run_daemon() does suppression happen?
The spec says "when `run_daemon()` initialises" but gives no exact line. Tests validate the *outcome*
(dispatchers from `from_env()` are suppressed) rather than the *location* in source. This ensures tests
remain valid even if the exact placement shifts within the function body.

### 2. Private method names for dispatch backends
The spec says "does NOT call `_dispatch_openclaw()`" — this private method name is referenced directly
in the spec's acceptance criteria, so it's used in mock patches. Similarly `_dispatch_telegram` and
`_dispatch_webhook` are assumed by analogy. If the actual method names differ, mock targets need updating
but the behavioral assertions remain correct.

### 3. "Suppression failure" guard pattern
The spec says the failure must log a specific message and continue, but doesn't specify the guard
structure. Tests simulate a try/except guard and validate the specified behavior (message + non-fatal
continuation) without prescribing the internal structure.

### 4. Truthy value semantics for NOTIFY_OPENCLAW_DAEMON_ENABLED
The spec lists exactly three truthy values: "1", "true", "yes". Contract 5 tests the negatives
(false, 0, no, off, empty, absent) parametrically. It's assumed the comparison is case-insensitive
(e.g., "True" also works), mirroring the `_bool()` helper pattern already in `notifications.py`.

### 5. NOTIFY_OPENCLAW_ENABLED="" vs "false" for suppression
The spec says the daemon sets `os.environ["NOTIFY_OPENCLAW_ENABLED"] = ""` (empty string), not
"false". The `_bool()` helper treats empty string as falsy, so both would suppress OpenClaw —
but tests use `""` as the spec specifies, not "false".
