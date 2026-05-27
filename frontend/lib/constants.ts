/**
 * Shared timing / polling constants.
 *
 * Replaces inline magic numbers (`30_000`, `10_000`) used by the dashboard
 * health-check timer and the runs list auto-refresh timer.
 *
 * Issue: #774
 */

/** Dashboard health-check polling interval in milliseconds. */
export const HEALTH_CHECK_INTERVAL_MS = 30_000;

/** Runs-list auto-refresh interval in milliseconds. */
export const RUNS_REFRESH_INTERVAL_MS = 10_000;
