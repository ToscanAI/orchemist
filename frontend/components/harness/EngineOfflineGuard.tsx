'use client';

/**
 * Top-level engine-reachability guard (#888 — harness graduation).
 *
 * The harness REQUIRES a reachable engine for v1. When `GET /api/v1/health`
 * rejects (network error, 5xx, DNS failure, ...) this guard short-circuits
 * the page body and renders a stark "Engine unreachable" error UI with a
 * Retry button and a docs link.
 *
 * Replaces the v0 "graceful degradation" behaviour where every page rendered
 * demo data on engine-offline. Per #888, that was misleading — operators
 * pointing the harness at a misconfigured port saw a polished UI rendering
 * fake data and could not tell the engine was broken.
 *
 * Composed in `frontend/app/layout.tsx` so all six harness routes are
 * protected uniformly. Per-page demo-data constants (FALLBACK_PHASES,
 * FALLBACK_CARDS, DEMO_*) are deleted in the same change set.
 *
 * @module
 */

import { ReactNode, useState, useCallback } from 'react';
import { getHealth } from '@/lib/api';
import { useApi } from '@/lib/useApi';
import type { HealthResponse } from '@/lib/types';

/**
 * Where the offline error UI sends the operator for help. Pointed at the
 * project's quickstart so the first-time-broken case has a remediation path
 * ("you need to start `orch serve`"). Externalised as a const so tests can
 * assert against it without duplicating the string.
 */
export const ENGINE_OFFLINE_DOCS_URL =
  'https://github.com/ToscanAI/orchemist#quickstart';

interface EngineOfflineGuardProps {
  readonly children: ReactNode;
}

/**
 * Resolve the API base URL for display in the error UI. Falls back through
 * `NEXT_PUBLIC_API_BASE_URL` (build-time, the canonical configuration knob),
 * then `window.location.origin` (same-origin / static export), then a
 * placeholder string. The visible text is always informative enough for the
 * operator to recognise a misconfigured port.
 */
function resolveBaseUrl(): string {
  const fromEnv =
    typeof process !== 'undefined'
      ? process.env['NEXT_PUBLIC_API_BASE_URL']
      : undefined;
  if (fromEnv && fromEnv.length > 0) return fromEnv;
  if (typeof window !== 'undefined') return window.location.origin;
  return '(same-origin)';
}

/**
 * Engine-reachability guard. Probes `/api/v1/health` on mount; renders the
 * offline error UI if the probe rejects, otherwise renders children.
 *
 * State machine (mirrors `useApi`'s `engineUp`):
 *   - null  → probe in flight; renders children (treat the brief loading
 *             state as "engine probably up" — children may fetch their own
 *             data and surface their own per-page errors).
 *   - true  → engine reachable; renders children unchanged.
 *   - false → engine offline; renders error UI (children NOT rendered).
 */
export function EngineOfflineGuard({ children }: EngineOfflineGuardProps) {
  // Bumping `retryKey` re-runs the `useApi` effect, which fires a fresh
  // `getHealth()` call. The retry handler is a stable `useCallback` so the
  // button doesn't change identity between renders.
  const [retryKey, setRetryKey] = useState(0);
  const handleRetry = useCallback(() => setRetryKey((k) => k + 1), []);

  const { engineUp, error } = useApi<HealthResponse>(
    () => getHealth(),
    [retryKey],
  );

  if (engineUp === false) {
    const baseUrl = resolveBaseUrl();
    const errorDetail =
      error?.message && error.message.length > 0
        ? error.message
        : 'No response from /api/v1/health';

    // Inline styles match the harness palette without pulling in extra
    // Tailwind utility chains. The error region is role="alert" so screen
    // readers announce on mount (a11y contract C8).
    return (
      <div
        data-testid="engine-offline-guard"
        role="alert"
        aria-live="assertive"
        style={{
          minHeight: '100vh',
          width: '100%',
          backgroundColor: '#0B0D10',
          color: '#E6EAF2',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '2rem',
          boxSizing: 'border-box',
        }}
      >
        <div
          style={{
            maxWidth: '640px',
            width: '100%',
            border: '1px solid #2A313D',
            borderRadius: '0.75rem',
            backgroundColor: '#161A21',
            padding: '2rem 2.25rem',
            boxShadow: '0 18px 56px rgba(0,0,0,0.45)',
          }}
        >
          <div
            style={{
              fontSize: '0.7rem',
              letterSpacing: '0.18em',
              textTransform: 'uppercase',
              color: '#EF4444',
              fontWeight: 700,
              marginBottom: '0.6rem',
            }}
          >
            ENGINE OFFLINE
          </div>

          <h1
            style={{
              fontSize: '1.4rem',
              fontWeight: 700,
              margin: 0,
              lineHeight: 1.3,
              color: '#E6EAF2',
              wordBreak: 'break-all',
            }}
          >
            Engine unreachable at {baseUrl}
          </h1>

          <p
            style={{
              marginTop: '1rem',
              marginBottom: '0.4rem',
              color: '#8A93A2',
              fontSize: '0.9rem',
              lineHeight: 1.55,
            }}
          >
            The harness requires a running Orchemist engine. Start it with
            <code
              style={{
                margin: '0 0.35em',
                padding: '0.1rem 0.4rem',
                background: '#0B0D10',
                border: '1px solid #2A313D',
                borderRadius: '0.25rem',
                color: '#2DD4BF',
                fontFamily: 'var(--font-geist-mono, ui-monospace, monospace)',
                fontSize: '0.82rem',
              }}
            >
              orch serve
            </code>
            and click Retry once it is listening.
          </p>

          <p
            style={{
              marginTop: '0.4rem',
              marginBottom: '1.4rem',
              color: '#5A6371',
              fontSize: '0.78rem',
              fontFamily: 'var(--font-geist-mono, ui-monospace, monospace)',
              wordBreak: 'break-word',
            }}
          >
            {errorDetail}
          </p>

          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: '0.75rem',
              alignItems: 'center',
            }}
          >
            <button
              type="button"
              data-testid="engine-offline-retry"
              onClick={handleRetry}
              aria-label="Retry"
              style={{
                appearance: 'none',
                background: '#7C5CFC',
                color: '#FFFFFF',
                border: '1px solid #7C5CFC',
                borderRadius: '0.4rem',
                padding: '0.55rem 1.1rem',
                fontSize: '0.85rem',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              Retry
            </button>

            <a
              data-testid="engine-offline-docs-link"
              href={ENGINE_OFFLINE_DOCS_URL}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                color: '#2DD4BF',
                textDecoration: 'underline',
                textDecorationColor: '#2A313D',
                fontSize: '0.85rem',
                fontWeight: 500,
              }}
            >
              docs · orch serve quickstart
            </a>
          </div>
        </div>
      </div>
    );
  }

  // engineUp === null (loading) OR engineUp === true → render children
  return <>{children}</>;
}
