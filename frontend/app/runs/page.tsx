'use client';

/**
 * Runs list page — `/runs`.
 *
 * Fetches pipeline runs from the API with filtering and pagination.
 * Renders a table with status badges, clickable rows for drill-down.
 *
 * @module
 */

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { listRuns, ApiError } from '@/lib/api';
import type { RunRecord, RunStatus, RunsListResponse, ListRunsParams } from '@/lib/types';
import { formatElapsed } from '@/lib/timeFmt';
import { RunStatusBadge } from '@/components/pipeline/RunStatusBadge';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { ErrorBanner } from '@/components/ui/ErrorBanner';
import { RUNS_REFRESH_INTERVAL_MS } from '@/lib/constants';

const PAGE_SIZE = 20;

const STATUS_OPTIONS = [
  'all',
  'pending',
  'running',
  'success',
  'failed',
  'cancelled',
  'crashed',
  'budget_exceeded',
  'scoring_failed',
  'pending_review',
  'rejected',
  'escalated',
] as const;

/**
 * Format a date string for display.
 */
function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—';
  const d = new Date(dateStr);
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function RunsPage() {
  const router = useRouter();
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [templateFilter, setTemplateFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params: ListRunsParams = {
        limit: PAGE_SIZE,
        offset,
        ...(statusFilter !== 'all' ? { status: statusFilter as RunStatus } : {}),
        ...(templateFilter.trim() ? { template_id: templateFilter.trim() } : {}),
      };

      const data: RunsListResponse = await listRuns(params);
      setRuns([...data.items]);
      setTotal(data.total);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError('Cannot connect to API server. Ensure `orch serve` is running.');
      }
    } finally {
      setLoading(false);
    }
  }, [offset, statusFilter, templateFilter]);

  useEffect(() => {
    fetchRuns();
    // Auto-refresh for running pipelines. Issue #774 — centralised in
    // lib/constants.ts so the magic number lives in exactly one place.
    const interval = setInterval(fetchRuns, RUNS_REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchRuns]);

  // Reset offset when filters change
  useEffect(() => {
    setOffset(0);
  }, [statusFilter, templateFilter]);

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <HarnessShell
      title="All pipeline runs"
      screenIndex={2}
      breadcrumb={[{ label: 'Fleet', href: '/' }, { label: 'All runs' }]}
    >
      <div>
      <div className="mb-6 flex items-center justify-between">
        {/* h2, not h1 — the TopBar already renders the page's single h1. */}
        <h2 className="text-[14px] font-semibold text-harness-text">Pipeline Runs</h2>
        <span className="text-[12px] text-harness-muted">{total} total</span>
      </div>

      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Filter by status"
          className="rounded-md border border-default bg-surface-0 px-3 py-1.5 text-sm text-content-primary focus:border-sky-500 focus:outline-none"
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s === 'all' ? 'All statuses' : s.charAt(0).toUpperCase() + s.slice(1)}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Filter by template..."
          aria-label="Filter by template"
          value={templateFilter}
          onChange={(e) => setTemplateFilter(e.target.value)}
          className="rounded-md border border-default bg-surface-0 px-3 py-1.5 text-sm text-content-primary placeholder:text-content-tertiary focus:border-sky-500 focus:outline-none"
        />
      </div>

      {/* Error state */}
      <ErrorBanner message={error} className="mb-4" />

      {/* Loading state */}
      {loading && !runs.length && (
        <div className="py-12 text-center text-content-tertiary">Loading runs...</div>
      )}

      {/* Empty state */}
      {!loading && !error && runs.length === 0 && (
        <div className="py-12 text-center">
          <p className="text-content-secondary">No runs found.</p>
          <p className="mt-1 text-sm text-content-tertiary">
            Launch a pipeline from the{' '}
            <Link href="/" className="text-sky-400 hover:underline">
              Dashboard
            </Link>{' '}
            or via <code className="text-content-secondary">orch run</code>, or go to{' '}
            <Link href="/templates" className="text-sky-400 hover:underline">
              Templates
            </Link>.
          </p>
        </div>
      )}

      {/* Table — overflow-x-auto so narrow viewports scroll the table inside
          its own container instead of clipping columns (2026-06-11 UX audit:
          at 390px only the Run ID column survived). */}
      {runs.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-default">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="border-b border-default bg-surface-0/50">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-content-secondary">Run ID</th>
                <th className="px-4 py-3 text-left font-medium text-content-secondary">Template</th>
                <th className="px-4 py-3 text-left font-medium text-content-secondary">Mode</th>
                <th className="px-4 py-3 text-left font-medium text-content-secondary">Status</th>
                <th className="px-4 py-3 text-left font-medium text-content-secondary">Started</th>
                <th className="px-4 py-3 text-left font-medium text-content-secondary">Elapsed</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, ridx) => (
                <tr
                  key={run.run_id}
                  className={`cursor-pointer transition-colors hover:bg-surface-0/50 ${ridx === 0 ? '' : 'border-t border-default'}`}
                  onClick={() => router.push(`/runs/${run.run_id}`)}
                >
                  <td className="px-4 py-3">
                    <span className="font-mono text-sky-400">
                      {run.run_id}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-content-primary">{run.template_id}</td>
                  <td className="px-4 py-3">
                    <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-content-secondary">
                      {run.mode}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <RunStatusBadge status={run.status} />
                  </td>
                  <td className="px-4 py-3 text-content-secondary">{formatDate(run.created_at)}</td>
                  <td className="px-4 py-3 text-content-secondary">
                    {formatElapsed(run.started_at, run.completed_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="mt-4 flex items-center justify-between">
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
            className="rounded-md border border-default bg-surface-0 px-3 py-1.5 text-sm text-content-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            Previous
          </button>
          <span className="text-sm text-content-tertiary">
            Page {currentPage} of {totalPages}
          </span>
          <button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= total}
            className="rounded-md border border-default bg-surface-0 px-3 py-1.5 text-sm text-content-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            Next
          </button>
        </div>
      )}
      </div>
    </HarnessShell>
  );
}
