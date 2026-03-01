/**
 * Run detail page — live SSE progress view.
 *
 * Route: /runs/[id]
 *
 * Subscribes to the SSE stream at GET /api/run/:id/status and renders:
 *   - Live phase progress (start → complete / error)
 *   - Token and cost metrics per phase
 *   - Pipeline summary when complete
 *   - Resume / Edit & Resume controls when paused
 */

"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { resumeRun, getRunOutputs, ApiError } from "@/lib/api";
import { useRunEvents } from "@/lib/sse";
import type {
  SseEvent,
  SsePhaseCompleteEvent,
  SsePhaseErrorEvent,
  SsePipelineCompleteEvent,
  PhaseOutputs,
} from "@/lib/types";
import { RunStatusBadge } from "@/components/pipeline/RunStatusBadge";
import { PhaseEventRow } from "@/components/pipeline/PhaseEventRow";
import { Button } from "@/components/ui/Button";

// NOTE: In Next.js 15, `params` becomes a Promise and must be unwrapped with
// `React.use(params)` in client components (or `await params` in async server
// components).  When upgrading to Next.js 15, change this to:
//   const { id: runId } = React.use(params);
// Required for static export with dynamic routes: tells Next.js there are no
// build-time params to pre-render.  At runtime the client-side router handles
// all /runs/[id] paths via the generated shell HTML.
export function generateStaticParams() {
  return [];
}

interface Props {
  // Next.js 14: params is still synchronous.  Next.js 15 wraps it in a Promise.
  params: { id: string };
}

export default function RunDetailPage({ params }: Props) {
  const runId = params.id;
  const { events, status, connected } = useRunEvents(runId);

  const [outputs, setOutputs] = useState<PhaseOutputs>({});
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  // Load phase outputs once the run completes or when paused
  useEffect(() => {
    if (status === "completed" || status === "paused") {
      getRunOutputs(runId).then(setOutputs).catch(() => {
        // Ignore — outputs are optional
      });
    }
  }, [runId, status]);

  // Extract pipeline_complete summary event if present
  const summaryEvent = events.find(
    (e): e is SsePipelineCompleteEvent => e.type === "pipeline_complete"
  );

  // Derive phase events for display
  const phaseEvents = events.filter(
    (e): e is SsePhaseCompleteEvent | SsePhaseErrorEvent =>
      e.type === "phase_complete" || e.type === "phase_error"
  );

  async function handleResume() {
    setResuming(true);
    setResumeError(null);
    try {
      await resumeRun(runId);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.body : String(err);
      setResumeError(message);
    } finally {
      setResuming(false);
    }
  }

  return (
    <div className="space-y-8">
      {/* Back link */}
      <Link href="/" className="text-sm text-text-secondary hover:text-text-primary no-underline">
        ← Back to templates
      </Link>

      {/* Run header */}
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-text-primary">Run</h1>
        <code className="rounded bg-surface-elevated px-2 py-0.5 font-mono text-xs text-text-secondary">
          {runId}
        </code>
        <RunStatusBadge status={status} />
        {connected && (
          <div className="flex items-center gap-1.5 text-xs text-text-muted">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-status-running" />
            Live
          </div>
        )}
      </div>

      {/* Paused — resume controls */}
      {status === "paused" && (
        <div className="rounded-md border border-status-warning/30 bg-status-warning/10 p-4">
          <p className="text-sm font-medium text-status-warning">
            Pipeline paused — waiting for review
          </p>
          <p className="mt-1 text-xs text-text-secondary">
            Review the phase output below, then click Resume to continue.
          </p>
          {resumeError && (
            <p className="mt-2 text-xs text-status-error">{resumeError}</p>
          )}
          <div className="mt-3 flex gap-2">
            <Button
              onClick={handleResume}
              disabled={resuming}
              loading={resuming}
              size="sm"
            >
              {resuming ? "Resuming…" : "Resume"}
            </Button>
          </div>
        </div>
      )}

      {/* Phase events */}
      {phaseEvents.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
            Phase Progress
          </h2>
          <div className="space-y-2">
            {phaseEvents.map((event, idx) => (
              <PhaseEventRow
                key={`${event.phase_id}-${idx}`}
                event={event}
                output={outputs[event.phase_id]}
              />
            ))}
          </div>
        </section>
      )}

      {/* In-progress indicator */}
      {connected && phaseEvents.length === 0 && (
        <div className="flex items-center gap-3 text-text-secondary">
          <div className="spinner h-5 w-5" />
          <span className="text-sm">Waiting for first phase…</span>
        </div>
      )}

      {/* Pipeline summary */}
      {summaryEvent && (
        <section className="card space-y-3">
          <h2 className="text-sm font-semibold text-text-primary">
            Pipeline Summary
          </h2>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3 text-sm">
            <SummaryItem
              label="Status"
              value={<RunStatusBadge status={summaryEvent.status} />}
            />
            <SummaryItem
              label="Phases"
              value={`${summaryEvent.completed} / ${summaryEvent.total_phases} completed`}
            />
            {summaryEvent.failed > 0 && (
              <SummaryItem
                label="Failed"
                value={
                  <span className="text-status-error">
                    {summaryEvent.failed}
                  </span>
                }
              />
            )}
            <SummaryItem
              label="Total Tokens"
              value={summaryEvent.total_tokens.toLocaleString()}
            />
            <SummaryItem
              label="Cost"
              value={`$${summaryEvent.total_cost.toFixed(6)}`}
            />
            <SummaryItem
              label="Elapsed"
              value={`${summaryEvent.total_elapsed}s`}
            />
          </dl>
        </section>
      )}

      {/* Raw event stream (collapsed) */}
      {events.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer select-none text-xs text-text-muted hover:text-text-secondary">
            Raw events ({events.length})
          </summary>
          <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-surface-elevated p-3 text-xs text-text-secondary">
            {events.map((e) => JSON.stringify(e)).join("\n")}
          </pre>
        </details>
      )}
    </div>
  );
}

/** Simple definition-list row */
function SummaryItem({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div>
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium text-text-primary">{value}</dd>
    </div>
  );
}
