REQUEST_CHANGES

[trivial_satisfaction] `TestSuppressionFailureNonFatal::test_suppression_failure_logs_warning_and_continues` — This test defines a local helper function `simulate_suppression_guard()` and then tests THAT function. No production code is ever called. The test passes today with zero implementation and will pass forever regardless of what the daemon does. A correct test should call the actual daemon suppression function (or a testable extracted helper) with `os.environ.__setitem__` mocked to raise, then verify the WARNING log and non-propagation. As written, this test provides zero behavioral assurance.

[coverage] No test exercises the actual `run_daemon()` suppression decision logic. Every daemon-side test manually sets `NOTIFY_OPENCLAW_ENABLED=""` (simulating the outcome) and then verifies `from_env()` respects it. This means an implementation that correctly implements `from_env()` but never adds the 3-line suppression guard to `run_daemon()` passes all tests. The first behavioral contract ("when run_daemon() initialises, the system sets os.environ['NOTIFY_OPENCLAW_ENABLED'] = ''") needs a test that either: (a) calls `run_daemon()` (or its extracted startup function) and asserts the env var was set, or (b) imports and calls the suppression helper directly with appropriate env state and asserts `os.environ["NOTIFY_OPENCLAW_ENABLED"] == ""` afterward. This is the integration seam the test suite is missing.

[coverage] `TestAlwaysLogsWarning::test_warning_always_logged_regardless_of_backends` — There is no behavioral contract in behavioral.md that requires a WARNING log on every `dispatch()` call when all backends are disabled. The spec only requires a WARNING when the suppression env-var write itself fails (error handling contract). This test validates a phantom requirement. Remove it or replace it with a test that validates an actual contract.

[specificity] `TestHumanReviewEvent::test_human_review_telegram_not_blocked_by_openclaw_suppression` passes keyword arguments (`score=0.82`, `issue_number=660`, `pr_url=...`) to `dispatcher.dispatch()` but never asserts these arguments were forwarded to `_dispatch_telegram`. A Telegram backend that silently drops all event payload data would pass. The behavioral contracts say Telegram "invokes normally" — the test should verify the event data reaches the Telegram backend (e.g., `mock_tg.assert_called_once_with(...)` or at minimum check `mock_tg.call_args`).

[specificity] `TestAutoMergeEvent::test_auto_merge_webhook_not_blocked_by_openclaw_suppression` — Same issue: asserts `_dispatch_webhook` was called but never verifies the event type or run_id were passed through. A webhook backend that fires with empty payloads would pass.

[trivial_satisfaction] `TestDispatcherOpenClawSuppressedDirectly::test_dispatcher_with_openclaw_false_skips_openclaw_backend` — Tests 3 event types but only asserts `_dispatch_openclaw` was NOT called. A dispatcher that drops ALL events for ALL backends (a no-op `dispatch()`) trivially passes. This test should also assert that other enabled backends (Telegram/Webhook) ARE called to confirm it's selective suppression, not total silence. The parallel test `test_dispatcher_with_openclaw_true_calls_openclaw_backend` partially compensates but doesn't share the same "all backends disabled except openclaw" setup.

[leakage] Minor: all tests reference `_dispatch_openclaw`, `_dispatch_telegram`, `_dispatch_webhook` — private methods (underscore prefix). The behavioral contracts in behavioral.md do reference `_dispatch_openclaw()` by name, so this is spec-sanctioned. However, if the implementation renames these methods (e.g., to `_send_openclaw`), every test breaks despite behavior being identical. Consider adding at least one end-to-end test that mocks the actual HTTP/session transport layer rather than internal dispatch methods.

---

**Summary of significant gaps:**

| # | Category | Severity | Description |
|---|----------|----------|-------------|
| 1 | trivial_satisfaction | **HIGH** | Suppression failure test is self-referential — tests its own local function, not production code |
| 2 | coverage | **HIGH** | No test for `run_daemon()` actually performing the suppression decision (checking DAEMON_ENABLED and setting OPENCLAW_ENABLED) |
| 3 | coverage | **MEDIUM** | Phantom test (`test_warning_always_logged`) validates a requirement not in the spec |
| 4 | specificity | **MEDIUM** | Event payload forwarding to Telegram/Webhook backends never verified |
| 5 | trivial_satisfaction | **LOW** | `test_dispatcher_with_openclaw_false_skips_openclaw_backend` passable by a total no-op dispatcher |
| 6 | leakage | **LOW** | All tests couple to private method names (spec-sanctioned but fragile) |

**Required fixes (HIGH):** Items 1 and 2 must be addressed before these tests can serve as reliable behavioral contracts.

**Recommended fixes (MEDIUM):** Items 3 and 4 would strengthen the suite significantly.

VERDICT: REQUEST_CHANGES
COMMENT: Two high-severity gaps: (1) suppression failure test is self-referential and tests no production code, (2) no test validates that run_daemon() actually performs the suppression decision. One phantom test validates a nonexistent requirement. Event payload forwarding specificity is also weak.
