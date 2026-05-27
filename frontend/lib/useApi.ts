'use client';

/**
 * Shared `useApi` data-fetching hook.
 *
 * Replaces the boilerplate `let cancelled = false` / `return () => { cancelled = true }`
 * pattern that was duplicated across 11+ pages in this codebase. The hook is
 * deliberately small — it covers the common single-fetch case and exposes a
 * derived `engineUp` flag (`null` while loading, `true` on success, `false`
 * on rejection) matching the convention used across the harness.
 *
 * Not in scope (out of #870):
 *   - Multi-call composition (`Promise.allSettled`-style aggregation). The
 *     dashboard and gates pages keep their hand-rolled closures because the
 *     `engineUp` heuristic over N parallel fetches with partial success has
 *     no clean single-hook generalisation.
 *   - Polling. The TopNav / BottomStrip `getHealth()` callers keep their
 *     `setInterval` loops.
 *   - Form prefill via downstream `setState` calls after the data resolves.
 *     Pages that need to populate several pieces of state from the resolved
 *     value (admin form, edit form, run-detail dashboard) keep their
 *     hand-rolled closures so the migration footprint stays small.
 *
 * Stale-fetch and unmount safety are implemented via the same
 * `let cancelled = false` discipline the closures used — no AbortController,
 * no external dependencies, no React-context plumbing.
 *
 * @module
 */

import { useEffect, useState } from 'react';

/** Return shape of the `useApi` hook. */
export interface UseApiResult<T> {
  /** Resolved value of the fetcher, or `null` while loading / on rejection. */
  readonly data: T | null;
  /** Rejection reason captured from the fetcher, or `null` otherwise. */
  readonly error: Error | null;
  /** `true` from mount or any `deps` change until the matching fetch settles. */
  readonly loading: boolean;
  /**
   * Convenience flag for the engine-reachability heuristic used across the
   * harness — `null` while loading, `true` after a successful fetch, `false`
   * after a rejection (engine offline, network failure, 4xx/5xx response).
   */
  readonly engineUp: boolean | null;
}

/**
 * Run an async fetcher inside a `useEffect` and expose its result.
 *
 * Re-invokes the fetcher whenever `deps` changes (same comparison semantics
 * as `useEffect`'s dependency array). A fetch that settles after the matching
 * dep set is superseded (by a new `deps` value or by unmount) is a no-op —
 * no state mutation happens, avoiding "setState after unmount" warnings.
 *
 * @param fetcher  Zero-argument async function. Re-invoked on every dep change.
 * @param deps     Dependency array. The fetcher re-runs when any entry changes
 *                 by reference (`Object.is`), matching `useEffect`.
 * @returns        `{ data, error, loading, engineUp }`.
 *
 * @example
 * ```tsx
 * const { data: templates, error, loading } = useApi(
 *   () => listTemplates(),
 *   [],
 * );
 * ```
 */
export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: readonly unknown[],
): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [engineUp, setEngineUp] = useState<boolean | null>(null);

  useEffect(() => {
    // Reset state for the new dep set so callers see a clean slate during
    // refetch — matches the visual behaviour of pre-existing hand-rolled
    // closures which set `template`/`data` to `null` between fetches.
    let cancelled = false;
    setData(null);
    setError(null);
    setLoading(true);
    setEngineUp(null);

    fetcher().then(
      (value) => {
        if (cancelled) return;
        setData(value);
        setError(null);
        setEngineUp(true);
        setLoading(false);
      },
      (err: unknown) => {
        if (cancelled) return;
        const captured: Error =
          err instanceof Error ? err : new Error(String(err));
        setError(captured);
        setEngineUp(false);
        setLoading(false);
      },
    );

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- caller-supplied deps array
  }, deps);

  return { data, error, loading, engineUp };
}
