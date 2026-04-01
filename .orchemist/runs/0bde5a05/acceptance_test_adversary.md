REQUEST_CHANGES

[trivial_satisfaction] test_suppression_failure_logs_warning_and_continues: **STILL UNFIXED FROM ROUND 1.** The test defines a local `simulate_daemon_startup_with_suppression_failure()` function, then tests THAT function. Zero production code is invoked. A production implementation that omits the try/except guard entirely (crashing on env mutation failure) passes this test. The error-handling contract in behavioral.md requires the *system* to log the WARNING and continue — this test only proves a hand-written local function does so. Must be rewritten to either: (a) call the production suppression guard with `os.environ.__setitem__` mocked to raise, then assert WARNING + continuation, or (b) at minimum, import the guard function and test it. As-is, this test provides zero assurance.

[trivial_satisfaction] test_openclaw_suppressed_when_daemon_flag_is_falsy: **NEW — entire test body is local Python logic, not production code.** The parametrized test for falsy DAEMON_ENABLED values ("false", "0", "no", "off", "", None) contains ONLY this logic: `daemon_enabled = (daemon_flag_value is not None and daemon_flag_value.lower() in ("1", "true", "yes")); assert daemon_enabled is False`. No dispatcher is created, no dispatch() is called, no production code is exercised. A production implementation that treats "0" or "off" as truthy (suppression bypassed) would NOT be caught. This must create a dispatcher under the appropriate env conditions and assert OpenClaw is actually suppressed — similar to how test_openclaw_fires_when_daemon_flag_is_truthy tests the positive case with a real dispatcher.

[coverage] No test for run_daemon() (or an extracted startup helper) actually performing the suppression decision. Every daemon-side test manually pre-sets `NOTIFY_OPENCLAW_ENABLED=""` and verifies `from_env()` respects it. This means an implementation that correctly implements `from_env()` but never adds the suppression guard to `run_daemon()` passes all tests. The first behavioral contract says: "when run_daemon() initialises, the system sets os.environ['NOTIFY_OPENCLAW_ENABLED'] = ''". A TDD test for this could call the startup/suppression function (once built) and assert `os.environ["NOTIFY_OPENCLAW_ENABLED"] == ""`. Acknowledged: TDD context makes dispatcher-level tests valid pre-build contracts, but the daemon startup logic is a distinct behavioral contract that remains untested.

[specificity] test_telegram_backend_fires_when_openclaw_suppressed, test_webhook_backend_fires_when_openclaw_suppressed, test_telegram_and_webhook_both_fire_when_openclaw_suppressed: **STILL UNFIXED FROM ROUND 1.** These tests assert `len(telegram_calls) == 1` / `len(webhook_calls) == 1` but never verify that event data (event name, run_id, or any payload) was forwarded to the backend. The behavioral contracts say Telegram/Webhook "invokes normally" — a backend that fires with empty/wrong payloads satisfies `len(calls) == 1`. Should assert at minimum: `telegram_calls[0]["event"] == "human_review"` and `telegram_calls[0]["run_id"] == "run-tg-test"` (or equivalent via `mock.call_args`).

[trivial_satisfaction] test_dispatch_continues_after_suppression_failure: The docstring claims it validates "even when suppression fails (env override could not be applied)" but the test sets `NOTIFY_OPENCLAW_ENABLED=""` — which is the SUCCESS case (suppression worked). In a real failure scenario, the env override would NOT have been applied, so `NOTIFY_OPENCLAW_ENABLED` would retain its inherited value (e.g., "true"). The test should either: (a) set `NOTIFY_OPENCLAW_ENABLED="true"` (simulating failed suppression) and verify Telegram still fires alongside OpenClaw, or (b) be removed since it duplicates test_telegram_backend_fires_when_openclaw_suppressed without adding failure-scenario coverage.

[leakage] Minor, spec-sanctioned but fragile: all 14 tests mock `_dispatch_openclaw`, `_dispatch_telegram`, `_dispatch_webhook` — private methods (underscore prefix). behavioral.md does reference `_dispatch_openclaw()` by name, so this coupling is spec-sanctioned. However, a method rename (e.g., `_send_openclaw`) breaks every test despite identical behavior. At least one integration-level test mocking the transport layer (HTTP calls, sessions_send) rather than internal dispatch methods would add resilience. This is LOW severity given the spec sanctions it.

---

**Summary of gaps:**

| # | Category | Severity | Status | Description |
|---|----------|----------|--------|-------------|
| 1 | trivial_satisfaction | **HIGH** | Round 1 unfixed | Suppression failure test is self-referential — tests its own local function, not production code |
| 2 | trivial_satisfaction | **HIGH** | New in round 2 | test_openclaw_suppressed_when_daemon_flag_is_falsy tests only local Python truthiness, no production code exercised |
| 3 | coverage | **MEDIUM** | Round 1 unfixed | No test for run_daemon() actually performing the suppression decision (checking DAEMON_ENABLED, setting OPENCLAW_ENABLED) |
| 4 | specificity | **MEDIUM** | Round 1 unfixed | Event payload forwarding to Telegram/Webhook backends never verified — only call count checked |
| 5 | trivial_satisfaction | **MEDIUM** | New in round 2 | test_dispatch_continues_after_suppression_failure uses success-case env (suppression applied) while claiming to test failure scenario |
| 6 | leakage | **LOW** | Round 1 acknowledged | All tests couple to private method names (spec-sanctioned but fragile) |

**Required fixes (HIGH):** Items 1 and 2 — both tests exercise zero production code and provide no behavioral assurance.

**Recommended fixes (MEDIUM):** Items 3, 4, and 5 — strengthen coverage, specificity, and failure-scenario accuracy.

**Note on round-over-round progress:** The phantom test (`test_warning_always_logged`) from round 1 was correctly removed. Tests were restructured from class-based to function-based (cleaner). However, two of three HIGH issues from round 1 remain unfixed, and a new HIGH issue was introduced (item 2).

VERDICT: REQUEST_CHANGES
COMMENT: Two HIGH gaps: (1) suppression failure test still self-referential (unfixed from round 1), (2) new test_openclaw_suppressed_when_daemon_flag_is_falsy tests only local Python logic with zero production code. Three MEDIUM gaps persist: no test for daemon startup decision, event payload specificity weak, failure-scenario test uses wrong env state.
