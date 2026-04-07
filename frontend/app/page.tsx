'use client';

/**
 * Dashboard home page — overview of pipeline activity.
 *
 * Shows at-a-glance stats (templates, recent runs, active runs) and
 * quick-access cards for recent runs and templates.
 */

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { listTemplates, listRuns, ApiError } from '@/lib/api';
import type { TemplateSummary, RunRecord } from '@/lib/types';
import { Badge } from '@/components/ui/Badge';

// ---------------------------------------------------------------------------
// Status badge helper
// ---------------------------------------------------------------------------

function statusVariant(status: string): 'success' | 'error' | 'warning' | 'info' | 'neutral' {
  switch (status) {
    case 'success': return 'success';
    case 'failed': case 'crashed': return 'error';
    case 'running': return 'warning';
    case 'pending': return 'info';
    default: return 'neutral';
  }
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export default function DashboardPage() {
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [totalRuns, setTotalRuns] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    Promise.all([
      listTemplates(),
      listRuns({ limit: 5 }).then((r) => { setTotalRuns(r.total); return r.items; }).catch(() => [] as RunRecord[]),
    ])
      .then(([tpls, rns]) => {
        if (!cancelled) {
          setTemplates(tpls);
          setRuns([...rns]);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load dashboard.');
          setLoading(false);
        }
      });

    return () => { cancelled = true; };
  }, []);

  const activeRuns = runs.filter((r) => r.status === 'running' || r.status === 'pending');
  const recentRuns = runs.slice(0, 5);

  return (
    <div className="flex flex-col gap-8">
      {/* Header */}
      <section>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-100">
          Dashboard
        </h1>
        <p className="mt-1 text-sm text-zinc-400">
          Pipeline activity overview.
        </p>
      </section>

      {/* Loading */}
      {loading && (
        <div className="flex flex-col items-center justify-center gap-4 py-16 text-zinc-400" role="status">
          <svg className="h-8 w-8 animate-spin text-sky-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
          <span className="text-sm">Loading dashboard...</span>
        </div>
      )}

      {/* Error */}
      {!loading && error && (
        <div className="card border-red-500/50 bg-red-900/10" role="status">
          <p className="text-sm font-medium text-red-400">{error}</p>
        </div>
      )}

      {/* Stats cards */}
      {!loading && !error && (
        <>
          <section className="grid gap-4 sm:grid-cols-3">
            <Link
              href="/templates"
              className="card flex flex-col gap-1 transition-colors hover:border-zinc-600"
            >
              <span className="text-xs font-medium uppercase tracking-wider text-zinc-500">
                Templates
              </span>
              <span className="text-3xl font-bold text-zinc-100">
                {templates.length}
              </span>
              <span className="text-xs text-zinc-500">available pipelines</span>
            </Link>

            <Link
              href="/runs"
              className="card flex flex-col gap-1 transition-colors hover:border-zinc-600"
            >
              <span className="text-xs font-medium uppercase tracking-wider text-zinc-500">
                Total Runs
              </span>
              <span className="text-3xl font-bold text-zinc-100">
                {totalRuns}
              </span>
              <span className="text-xs text-zinc-500">pipeline executions</span>
            </Link>

            <div className="card flex flex-col gap-1">
              <span className="text-xs font-medium uppercase tracking-wider text-zinc-500">
                Active Now
              </span>
              <span className={`text-3xl font-bold ${activeRuns.length > 0 ? 'text-amber-400' : 'text-zinc-100'}`}>
                {activeRuns.length}
              </span>
              <span className="text-xs text-zinc-500">running or pending</span>
            </div>
          </section>

          {/* Recent runs */}
          <section>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-lg font-medium text-zinc-200">Recent Runs</h2>
              <Link href="/runs" className="text-xs text-sky-400 hover:text-sky-300">
                View all →
              </Link>
            </div>

            {recentRuns.length === 0 ? (
              <div className="card py-8 text-center">
                <p className="text-sm text-zinc-500">
                  No runs yet. Go to{' '}
                  <Link href="/templates" className="text-sky-400 hover:underline">
                    Templates
                  </Link>{' '}
                  to launch your first pipeline.
                </p>
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {recentRuns.map((run) => (
                  <Link
                    key={run.run_id}
                    href={`/runs/${encodeURIComponent(run.run_id)}`}
                    className="card flex items-center justify-between gap-4 transition-colors hover:border-zinc-600"
                  >
                    <div className="flex flex-col gap-0.5 min-w-0">
                      <span className="text-sm font-medium text-zinc-200 truncate">
                        {run.template_id}
                      </span>
                      <span className="text-xs text-zinc-500 font-mono truncate">
                        {run.run_id}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 flex-shrink-0">
                      <span className="text-xs text-zinc-500">
                        {run.mode}
                      </span>
                      <Badge variant={statusVariant(run.status)}>
                        {run.status}
                      </Badge>
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </section>

          {/* Quick launch — top 3 templates */}
          <section>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-lg font-medium text-zinc-200">Quick Launch</h2>
              <Link href="/templates" className="text-xs text-sky-400 hover:text-sky-300">
                All templates →
              </Link>
            </div>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {templates.slice(0, 3).map((t) => (
                <Link
                  key={t.id}
                  href={`/templates/${encodeURIComponent(t.id)}`}
                  className="card group flex flex-col gap-2 transition-colors hover:border-zinc-600"
                >
                  <span className="text-sm font-semibold text-zinc-100 group-hover:text-white">
                    {t.name}
                  </span>
                  <div className="flex gap-1.5">
                    <Badge variant="neutral">v{t.version}</Badge>
                    <Badge variant="info">{t.category}</Badge>
                  </div>
                  <span className="text-xs text-zinc-400 line-clamp-2">
                    {t.description}
                  </span>
                </Link>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
