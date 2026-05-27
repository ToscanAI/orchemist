/**
 * Shared time-formatting helpers.
 *
 * Consolidates the duplicated relative / elapsed time formatters that used
 * to live inline in `app/runs/page.tsx` and `app/gates/page.tsx`. The two
 * functions exposed here intentionally use DIFFERENT vocabularies that match
 * each callsite's pre-existing label strings — they are NOT interchangeable:
 *
 *   - `formatRelative` — "Xmin / Xh / Xd ago" / "just now" / "—"
 *     (Trust & Gates audit row vocabulary.)
 *   - `formatElapsed`  — "Xs" / "Xm Ys" / "Xh Ym"
 *     (Runs list "Elapsed" cell vocabulary.)
 *
 * Both functions are PURE — no DOM access, no console output, no thrown
 * exceptions on hostile inputs (nullish / non-parseable timestamps map to
 * the sentinel "—" or "0s" rather than throwing).
 *
 * @module
 */

// ── formatRelative ────────────────────────────────────────────────────────────

/** Threshold below which any positive delta is rendered as "just now". */
const JUST_NOW_MS = 60_000;

/** One hour in milliseconds. */
const HOUR_MS = 60 * 60 * 1000;

/** One day in milliseconds. */
const DAY_MS = 24 * HOUR_MS;

/**
 * Render a timestamp as a coarse relative-time label.
 *
 * Behaviour:
 * - `null` / empty / non-parseable → `"—"`.
 * - Future timestamp (delta < 0)   → `"just now"`.
 * - delta < 60 s                   → `"just now"`.
 * - delta < 1 hour                 → `"<N> min ago"` (e.g. `"5 min ago"`).
 * - delta < 24 hours               → `"<N>h ago"`  (e.g. `"3h ago"`).
 * - delta ≥ 24 hours               → `"<N>d ago"`  (e.g. `"5d ago"`).
 *
 * Pure — `Date.now()` is the only external dependency.
 *
 * @param iso  ISO-8601 timestamp, or `null` / `undefined` / empty / garbage.
 * @returns    A non-empty label string.
 */
export function formatRelative(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return '—';

  const diffMs = Date.now() - t;
  if (diffMs < JUST_NOW_MS) return 'just now';
  if (diffMs < HOUR_MS) {
    const min = Math.floor(diffMs / 60_000);
    return `${min} min ago`;
  }
  if (diffMs < DAY_MS) {
    const h = Math.floor(diffMs / HOUR_MS);
    return `${h}h ago`;
  }
  const days = Math.floor(diffMs / DAY_MS);
  return `${days}d ago`;
}

// ── formatElapsed ─────────────────────────────────────────────────────────────

/**
 * Render a `(start, end)` interval as a compact elapsed-time label.
 *
 * Behaviour:
 * - `startIso` nullish / non-parseable → `"—"`.
 * - `endIso` nullish or omitted        → uses `Date.now()` as the end value.
 * - Negative delta (end < start)       → clamps to `"0s"` (never negative).
 * - seconds < 60                       → `"<N>s"`        (e.g. `"45s"`).
 * - seconds < 3 600                    → `"<N>m <M>s"`  (e.g. `"5m 30s"`).
 * - seconds ≥ 3 600                    → `"<N>h <M>m"`  (e.g. `"2h 15m"`).
 *
 * Pure — `Date.now()` is the only external dependency, and only consulted
 * when `endIso` is nullish.
 *
 * @param startIso  Start timestamp, or `null` / `undefined`.
 * @param endIso    End timestamp, or `null` / `undefined` for "now".
 * @returns         A non-empty label string.
 */
export function formatElapsed(
  startIso: string | null | undefined,
  endIso?: string | null,
): string {
  if (startIso === null || startIso === undefined || startIso === '') return '—';
  const start = new Date(startIso).getTime();
  if (!Number.isFinite(start)) return '—';

  let end: number;
  if (endIso === null || endIso === undefined || endIso === '') {
    end = Date.now();
  } else {
    end = new Date(endIso).getTime();
    if (!Number.isFinite(end)) end = Date.now();
  }

  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (minutes < 60) return `${minutes}m ${secs}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}
