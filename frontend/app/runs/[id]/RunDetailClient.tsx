'use client';

/**
 * Run detail page — `/runs/[id]`.
 *
 * Subscribes to the live SSE stream for a pipeline run and renders real-time
 * phase progress, a paused-run resume banner, a post-completion summary, and
 * a collapsible raw event log.
 *
 * Client component: all data arrives via `useRunEvents` (SSE) at runtime.
 * `generateStaticParams` returns `[]` to satisfy the static export requirement
 * for dynamic `[id]` segments without pre-rendering any specific run IDs.
 *
 * @module
 */

import { useState, useMemo } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useRunEvents } from '@/lib/sse';
import { resumeRun, cancelRun, ApiError } from '@/lib/api';
import { RunStatusBadge } from '@/components/pipeline/RunStatusBadge';
import { PhaseEventRow } from '@/components/pipeline/PhaseEventRow';
import type { PhaseEventRowProps } from '@/components/pipeline/PhaseEventRow';
import { Button } from '@/components/ui/Button';
import { LogViewer } from '@/components/pipeline/LogViewer';
import type { SseStatusChangedEvent } from '@/lib/types';

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Compute elapsed seconds between two ISO timestamp strings.
 * Returns `null` when either timestamp is missing or invalid.
 */
function computeElapsed(startIso: string | null, endIso: string | null): number | null {
  if (!startIso || !endIso) return null;
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  if (isNaN(start) || isNaN(end) || end < start) return null;
  return (end - start) / 1000;
}

// ---------------------------------------------------------------------------
// RunDetailPage
// ---------------------------------------------------------------------------

/**
 * Live SSE-powered run detail page.
 *
 * Renders:
 * - Run ID heading with `RunStatusBadge` derived from SSE stream state.
 * - Paused banner (shown when `status_changed` event carries `status === 'paused'`).
 *   Resume button calls `POST /api/v1/runs/{id}/resume` with a `finally` cleanup.
 * - Phase timeline: accumulated `PhaseEventRow` entries from `phase_completed`
 *   events, plus a live spinner for the currently executing phase.
 * - Terminal summary: total cost, token count, phases completed, optional error.
 * - Collapsible raw event log showing all SSE events as pretty-printed JSON.
 */
export default function RunDetailClient() {
  const params = useParams<{ id: string }>();
  const runId = decodeURIComponent(params.id);

  // ── SSE stream ────────────────────────────────────────────────────────────
  const { events, status, connected } = useRunEvents(runId);

  // ── Resume action state ───────────────────────────────────────────────────
  const [resuming, setResuming] = useState<boolean>(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  // ── Event log toggle ──────────────────────────────────────────────────────
  const [showEventLog, setShowEventLog] = useState<boolean>(false);

  // ── Tab state ─────────────────────────────────────────────────────────────
  const [activeTab, setActiveTab] = useState<'timeline' | 'logs'>('timeline');

  // ── Cancel action state ───────────────────────────────────────────────────
  const [cancelling, setCancelling] = useState<boolean>(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  // ── Derive display data from accumulated events ───────────────────────────

  /**
   * Processes the raw SSE event list into structured UI data:
   * - `completedPhases`: ordered list of `PhaseEventRowProps` for `PhaseEventRow`.
   * - `currentPhase`: phase_id of the most recently started (not yet completed) phase.
   * - `finalStatusEvent`: the `status_changed` event, when present.
   * - `isPaused`: true when `status_changed.status` is the string `'paused'`
   *   (runtime check; `'paused'` is not in the `RunStatus` union).
   * - `totalCostUsd`: sum of `cost_usd` across all completed phases.
   * - `totalTokens`: sum of `tokens_consumed` across all completed phases.
   */
  const {
    completedPhases,
    currentPhase,
    finalStatusEvent,
    isPaused,
    totalCostUsd,
    totalTokens,
  } = useMemo(() => {
    // Track phase start timestamps keyed by phase_id for elapsed time calc.
    const startTimes = new Map<string, string>();

    const completedPhases: PhaseEventRowProps[] = [];
    let currentPhase: string | null = null;
    let finalStatusEvent: SseStatusChangedEvent | null = null;
    let isPaused = false;
    let totalCostUsd = 0;
    let totalTokens = 0;

    for (const event of events) {
      if (event.type === 'phase_started') {
        // Track this phase as currently executing.
        currentPhase = event.phase_id ?? 'unknown';
        if (event.phase_id && event.created_at) {
          startTimes.set(event.phase_id, event.created_at);
        }
      } else if (event.type === 'phase_completed') {
        const phaseName = event.phase_id ?? 'unknown';

        // The phase has finished — clear from in-progress tracking.
        if (currentPhase === phaseName) currentPhase = null;

        // Compute wall-clock elapsed time using start/end timestamps.
        const elapsedSeconds = computeElapsed(
          event.phase_id ? (startTimes.get(event.phase_id) ?? null) : null,
          event.created_at,
        );

        // Derive phase status from the `state` field.
        // The SSE spec does not define a `phase_error` event type; errors are
        // signalled via `phase_completed` with `state === 'failed'`.
        const phaseStatus: 'completed' | 'error' =
          event.state === 'failed' ? 'error' : 'completed';

        // Accumulate totals.
        if (event.cost_usd !== null) totalCostUsd += event.cost_usd;
        if (event.tokens_consumed !== null) totalTokens += event.tokens_consumed;

        completedPhases.push({
          phaseName,
          phaseStatus,
          // SSE only provides `tokens_consumed` (combined); treat as tokensIn.
          tokensIn: event.tokens_consumed,
          tokensOut: null,
          costUsd: event.cost_usd,
          elapsedSeconds,
          outputPreview: null,
        });
      } else if (event.type === 'status_changed') {
        finalStatusEvent = event;

        // 'paused' is not in the RunStatus union but may appear at runtime.
        // Use a string comparison to avoid TypeScript union narrowing errors.
        if ((event.status as string) === 'paused') {
          isPaused = true;
        } else {
          // Any non-paused terminal status clears the paused flag.
          isPaused = false;
        }
      }
    }

    return {
      completedPhases,
      currentPhase,
      finalStatusEvent,
      isPaused,
      totalCostUsd,
      totalTokens,
    };
  }, [events]);

  // Convenience flag: stream has reached a terminal state.
  const isTerminal =
    status === 'completed' || status === 'error' || status === 'aborted';

  // ── Resume handler ────────────────────────────────────────────────────────

  /**
   * Sends a resume request for the current run.
   * Uses a `finally` block to always reset the `resuming` spinner,
   * even when the API call throws.
   */
  async function handleResume() {
    setResuming(true);
    setResumeError(null);
    try {
      await resumeRun(runId);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setResumeError(err.message);
      } else if (err instanceof Error) {
        setResumeError(err.message);
      } else {
        setResumeError('Failed to resume run.');
      }
    } finally {
      setResuming(false);
    }
  }

  // ── Cancel handler ────────────────────────────────────────────────────────
  async function handleCancel() {
    setCancelling(true);
    setCancelError(null);
    try {
      await cancelRun(runId);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setCancelError(err.message);
      } else {
        setCancelError('Failed to cancel run.');
      }
    } finally {
      setCancelling(false);
    }
  }

  // Can cancel: only when running or pending
  const canCancel = !isTerminal && !isPaused;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col gap-8">

      {/* ── Back navigation ─────────────────────────────────────────────── */}
      <Link
        href="/"
        className="text-sm text-sky-400 hover:text-sky-300 self-start"
      >
        ← Back to dashboard
      </Link>

      {/* ── Header: run ID + live status badge ──────────────────────────── */}
      <section aria-labelledby="run-heading">
        <div className="flex flex-wrap items-center gap-3">
          <h1
            id="run-heading"
            className="text-2xl font-semibold tracking-tight text-zinc-100 font-mono"
          >
            Run {runId}
          </h1>
          <RunStatusBadge status={status} />
          {/* Connection indicator — shown while connecting before first events */}
          {!connected && !isTerminal && (
            <span className="text-xs text-zinc-500" aria-live="polite">
              Connecting…
            </span>
          )}
          {/* Cancel button */}
          {canCancel && (
            <button
              onClick={handleCancel}
              disabled={cancelling}
              className="ml-auto rounded-md border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-sm text-red-400 hover:bg-red-500/20 disabled:opacity-50"
            >
              {cancelling ? 'Cancelling…' : 'Cancel Run'}
            </button>
          )}
        </div>
        {cancelError && (
          <p className="mt-1 text-xs text-red-400">{cancelError}</p>
        )}
      </section>

      {/* ── Tabs ──────────────────────────────────────────────────────────────────── */}
      <div className="flex gap-1 border-b border-zinc-800">
        <button
          onClick={() => setActiveTab('timeline')}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === 'timeline'
              ? 'border-b-2 border-sky-400 text-sky-400'
              : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          Timeline
        </button>
        <button
          onClick={() => setActiveTab('logs')}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === 'logs'
              ? 'border-b-2 border-sky-400 text-sky-400'
              : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          Logs
        </button>
      </div>

      {/* ── Paused banner ────────────────────────────────────────────────── */}
      {isPaused && (
        <div
          className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-yellow-500/50 bg-yellow-900/10 px-4 py-3"
          role="alert"
          aria-live="polite"
        >
          <p className="text-sm text-yellow-300">
            Run is paused awaiting approval.
          </p>
          <div className="flex flex-col items-end gap-1">
            <Button
              variant="primary"
              size="sm"
              loading={resuming}
              disabled={resuming}
              onClick={handleResume}
            >
              {resuming ? 'Resuming…' : 'Resume'}
            </Button>
            {resumeError !== null && (
              <p className="text-xs text-red-400" role="alert">
                {resumeError}
              </p>
            )}
          </div>
        </div>
      )}

      {/* ── Logs tab ────────────────────────────────────────────────────── */}
      {activeTab === 'logs' && (
        <LogViewer runId={runId} />
      )}

      {/* ── Phase timeline ───────────────────────────────────────────────── */}
      {activeTab === 'timeline' && (
      <section aria-labelledby="phases-heading">
        <h2
          id="phases-heading"
          className="mb-3 text-base font-semibold text-zinc-200"
        >
          Phase Timeline
        </h2>

        <div className="flex flex-col gap-2" role="list" aria-label="Phase timeline">
          {/* Completed phase rows */}
          {completedPhases.map((phase, idx) => (
            <PhaseEventRow
              key={`${phase.phaseName}-${idx}`}
              {...phase}
            />
          ))}

          {/* In-progress phase spinner — hidden once terminal */}
          {currentPhase !== null && !isTerminal && (
            <div
              className="flex items-center gap-3 rounded-lg border border-surface-3 bg-surface-2 px-4 py-3"
              role="listitem"
              aria-live="polite"
            >
              <svg
                className="h-4 w-4 animate-spin flex-shrink-0 text-sky-400"
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
              <span className="font-mono text-sm text-content-primary">
                {currentPhase}
              </span>
              <RunStatusBadge status="running" />
            </div>
          )}

          {/* Empty state while awaiting first events */}
          {completedPhases.length === 0 && currentPhase === null && !isTerminal && (
            <p className="text-sm italic text-zinc-500">
              Waiting for phase events…
            </p>
          )}
        </div>
      </section>
      )}

      {/* ── Terminal summary ─────────────────────────────────────────────── */}
      {isTerminal && finalStatusEvent !== null && (
        <section className="card" aria-labelledby="summary-heading">
          <h2
            id="summary-heading"
            className="mb-3 text-base font-semibold text-zinc-200"
          >
            Run Summary
          </h2>

          <dl className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-xs text-zinc-500">Status</dt>
              <dd className="mt-1">
                <RunStatusBadge status={finalStatusEvent.status} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-zinc-500">Phases completed</dt>
              <dd className="mt-1 font-mono text-zinc-200">
                {completedPhases.length}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-zinc-500">Total tokens</dt>
              <dd className="mt-1 font-mono text-zinc-200">
                {totalTokens > 0
                  ? totalTokens.toLocaleString('en-US', { maximumFractionDigits: 0 })
                  : '—'}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-zinc-500">Total cost</dt>
              <dd className="mt-1 font-mono text-zinc-200">
                {totalCostUsd > 0 ? `$${totalCostUsd.toFixed(4)}` : '—'}
              </dd>
            </div>

            {/* Error message — spans full width when present */}
            {finalStatusEvent.error_message !== null &&
              finalStatusEvent.error_message !== undefined && (
                <div className="col-span-full mt-1">
                  <dt className="text-xs text-zinc-500">Error</dt>
                  <dd className="mt-1 rounded-lg bg-red-900/20 px-3 py-2 font-mono text-xs text-red-400">
                    {finalStatusEvent.error_message}
                  </dd>
                </div>
              )}
          </dl>
        </section>
      )}

      {/* ── Raw event log — collapsible ──────────────────────────────────── */}
      <section>
        <details
          onToggle={(e) =>
            setShowEventLog((e.currentTarget as HTMLDetailsElement).open)
          }
        >
          <summary
            className="cursor-pointer select-none text-sm text-zinc-400 hover:text-zinc-200"
            aria-expanded={showEventLog}
          >
            Raw event log ({events.length}{' '}
            {events.length === 1 ? 'event' : 'events'})
          </summary>

          {showEventLog && (
            <pre className="mt-2 max-h-96 overflow-auto rounded-lg border border-zinc-700 bg-zinc-900 p-4 text-xs text-zinc-300">
              {events.length === 0
                ? '(no events yet)'
                : events
                    .map((e) => JSON.stringify(e, null, 2))
                    .join('\n\n')}
            </pre>
          )}
        </details>
      </section>

    </div>
  );
}
