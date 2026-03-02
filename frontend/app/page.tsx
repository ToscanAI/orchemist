'use client';

/**
 * Dashboard home page — template grid.
 *
 * Fetches all available pipeline templates from the backend and renders them
 * as a responsive card grid. Handles loading, error, and empty states.
 *
 * `'use client'` is required here because this component uses `useState` and
 * `useEffect`. The app uses `output: 'export'` (static export) in next.config.js,
 * so all data fetching must be client-side.
 */

import { useState, useEffect } from 'react';
import { listTemplates, ApiError } from '@/lib/api';
import type { TemplateSummary } from '@/lib/types';
import { TemplateCard } from '@/components/pipeline/TemplateCard';

// ---------------------------------------------------------------------------
// HomePage
// ---------------------------------------------------------------------------

/**
 * Dashboard page component.
 *
 * On mount, fetches the list of pipeline templates and renders:
 *  - A spinner while loading
 *  - An error card on failure (with a hint to check `orch serve`)
 *  - A guidance card when no templates are found
 *  - A responsive 1–3 column grid of `TemplateCard` components
 */
export default function HomePage() {
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Guard against state updates after unmount to avoid React warnings.
    let cancelled = false;

    listTemplates()
      .then((data) => {
        if (!cancelled) {
          setTemplates(data);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          if (err instanceof ApiError) {
            setError(err.message);
          } else if (err instanceof Error) {
            setError(err.message);
          } else {
            setError('An unexpected error occurred.');
          }
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex flex-col gap-8">
      {/* Page header */}
      <section aria-labelledby="dashboard-heading">
        <h1
          id="dashboard-heading"
          className="text-2xl font-semibold tracking-tight text-zinc-100"
        >
          Dashboard
        </h1>
        <p className="mt-1 text-sm text-zinc-400">
          Available pipeline templates.
        </p>
      </section>

      {/* Template grid — loading / error / empty / data states */}
      <section aria-label="Pipeline templates">
        {loading && (
          <div
            className="flex flex-col items-center justify-center gap-4 py-16 text-zinc-400"
            role="status"
            aria-live="polite"
          >
            {/* Accessible spinner via animate-spin SVG */}
            <svg
              className="h-8 w-8 animate-spin text-sky-500"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
              />
            </svg>
            <span className="text-sm">Loading templates...</span>
          </div>
        )}

        {!loading && error !== null && (
          <div
            className="card border-red-500/50 bg-red-900/10"
            role="status"
            aria-live="polite"
          >
            <p className="text-sm font-medium text-red-400">{error}</p>
            <p className="mt-1 text-xs text-zinc-500">Is orch serve running?</p>
          </div>
        )}

        {!loading && error === null && templates.length === 0 && (
          <div
            className="card flex flex-col items-center justify-center gap-2 py-12 text-center"
            role="status"
            aria-live="polite"
          >
            <p className="text-sm text-zinc-400">No templates found.</p>
            <p className="text-xs text-zinc-500">
              Place YAML template files in your{' '}
              <code className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-zinc-300">
                templates/
              </code>{' '}
              directory and restart <code className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-zinc-300">orch serve</code>.
            </p>
          </div>
        )}

        {!loading && error === null && templates.length > 0 && (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {templates.map((template) => (
              <TemplateCard key={template.id} template={template} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
