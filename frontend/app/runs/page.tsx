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
import { RunStatusBadge } from '@/components/pipeline/RunStatusBadge';
import { HarnessShell } from '@/components/harness/HarnessShell';

const PAGE_SIZE = 20;

const STATUS_OPTIONS = [
  'all',
  'pending',
  'running',
  'success',
  'failed',
  'cancelled',
  'crashed',
  'scoring_failed',
] as const;

/**
 * Format elapsed time from start to end (or now if still running).
 */
function formatElapsed(startedAt: string | null, completedAt: string | null, status: string): string {
  if (!startedAt) return '—';
  const start = new Date(startedAt).getTime();
  const end = completedAt
    ? new Date(completedAt).getTime()
    : (status === 'running' ? Date.now() : new Date(startedAt).getTime());
  const seconds = Math.floor((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (minutes < 60) return `${minutes}m ${secs}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

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
    // Auto-refresh every 10s for running pipelines
    const interval = setInterval(fetchRuns, 10000);
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
        <h1 className="text-[14px] font-semibold text-harness-text">Pipeline Runs</h1>
        <span className="text-[12px] text-harness-muted">{total} total</span>
      </div>

      {/* Filters */}
      <div className="mb-4 flex gap-3">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-sky-500 focus:outline-none"
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
          value={templateFilter}
          onChange={(e) => setTemplateFilter(e.target.value)}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 placeholder-zinc-500 focus:border-sky-500 focus:outline-none"
        />
      </div>

      {/* Error state */}
      {error && (
        <div className="mb-4 rounded-lg border border-red-500/20 bg-red-500/10 p-4 text-sm text-red-400" role="alert">
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && !runs.length && (
        <div className="py-12 text-center text-zinc-500">Loading runs...</div>
      )}

      {/* Empty state */}
      {!loading && !error && runs.length === 0 && (
        <div className="py-12 text-center">
          <p className="text-zinc-400">No runs found.</p>
          <p className="mt-1 text-sm text-zinc-600">
            Launch a pipeline from the{' '}
            <Link href="/" className="text-sky-400 hover:underline">
              Dashboard
            </Link>{' '}
            or via <code className="text-zinc-400">orch run</code>, or go to{' '}
            <Link href="/templates" className="text-sky-400 hover:underline">
              Templates
            </Link>.
          </p>
        </div>
      )}

      {/* Table */}
      {runs.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 bg-zinc-900/50">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-zinc-400">Run ID</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-400">Template</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-400">Mode</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-400">Status</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-400">Started</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-400">Elapsed</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {runs.map((run) => (
                <tr
                  key={run.run_id}
                  className="cursor-pointer transition-colors hover:bg-zinc-900/50"
                  onClick={() => router.push(`/runs/${run.run_id}`)}
                >
                  <td className="px-4 py-3">
                    <span className="font-mono text-sky-400">
                      {run.run_id}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-zinc-300">{run.template_id}</td>
                  <td className="px-4 py-3">
                    <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-xs text-zinc-400">
                      {run.mode}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <RunStatusBadge status={run.status} />
                  </td>
                  <td className="px-4 py-3 text-zinc-400">{formatDate(run.created_at)}</td>
                  <td className="px-4 py-3 text-zinc-400">
                    {formatElapsed(run.started_at, run.completed_at, run.status)}
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
            className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Previous
          </button>
          <span className="text-sm text-zinc-500">
            Page {currentPage} of {totalPages}
          </span>
          <button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= total}
            className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Next
          </button>
        </div>
      )}
      </div>
    </HarnessShell>
  );
}
