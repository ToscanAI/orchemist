'use client';

/**
 * React hook for consuming live SSE pipeline events.
 *
 * Wraps the browser `EventSource` lifecycle in a React-idiomatic interface:
 * accumulated `events[]`, a derived `status` string, and a `connected` flag.
 *
 * Usage:
 * ```tsx
 * import { useRunEvents } from '@/lib/sse';
 *
 * function RunProgress({ runId }: { runId: string }) {
 *   const { events, status, connected } = useRunEvents(runId);
 *   return <div>{status} — {events.length} events</div>;
 * }
 * ```
 *
 * @module
 */

import { useState, useEffect, useRef } from 'react';
import type { SseEvent, SseStatusChangedEvent, RunStatus } from '@/lib/types';

// ── Public types ──────────────────────────────────────────────────────────────

/**
 * Derived status values returned by the hook.
 *
 * These are distinct from `RunStatus` (the backend enum) and represent
 * the hook's view of the stream:
 *
 * - `connecting`  — `EventSource` opened but `onopen` not yet fired.
 * - `running`     — at least one `phase_started` or `phase_completed` received.
 * - `completed`   — `status_changed` with `status === 'success'`.
 * - `error`       — `status_changed` with `status` in `{'failed','crashed','scoring_failed'}`.
 * - `aborted`     — `status_changed` with `status === 'cancelled'`.
 */
export type RunEventStatus =
  | 'connecting'
  | 'running'
  | 'completed'
  | 'error'
  | 'aborted';

/** Return shape of `useRunEvents`. */
export interface UseRunEventsResult {
  /** All SSE events received so far, in arrival order. */
  readonly events: readonly SseEvent[];
  /** Derived summary status (see `RunEventStatus`). */
  readonly status: RunEventStatus;
  /** `true` once the `EventSource.onopen` callback fires. */
  readonly connected: boolean;
}

// ── Constants ─────────────────────────────────────────────────────────────────

/** Base URL (same-origin by default; overridable via env var). */
const BASE_URL: string =
  (typeof process !== 'undefined' &&
    process.env['NEXT_PUBLIC_API_BASE_URL']) ||
  '';

/**
 * `RunStatus` values that indicate the pipeline has reached a terminal state.
 * When a `status_changed` event carries one of these, the `EventSource` is
 * closed automatically.
 */
const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'success',
  'failed',
  'cancelled',
  'crashed',
  'scoring_failed',
]);

/** Named SSE event types emitted by `GET /api/v1/runs/{run_id}/stream`. */
const SSE_EVENT_TYPES = [
  'phase_started',
  'phase_completed',
  'status_changed',
  'error',
] as const;

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Map a terminal `RunStatus` to the hook's `RunEventStatus` vocabulary.
 *
 * @param runStatus  The `status` field from a `status_changed` SSE event.
 * @returns          Corresponding `RunEventStatus`.
 */
function deriveStatus(runStatus: SseStatusChangedEvent['status']): RunEventStatus {
  switch (runStatus) {
    case 'success':
      return 'completed';
    case 'failed':
    case 'crashed':
    case 'scoring_failed':
      return 'error';
    case 'cancelled':
      return 'aborted';
    default:
      // Non-terminal statuses (pending/running) are handled externally;
      // this branch is only reached for unexpected values.
      return 'running';
  }
}

/**
 * Parse a raw SSE message into a typed `SseEvent`.
 *
 * @param eventType  The named SSE event type (e.g. `'phase_started'`).
 * @param data       Raw JSON string from the `MessageEvent.data` field.
 * @returns          Typed `SseEvent`, or `null` when parsing fails.
 */
function parseRawEvent(eventType: string, data: string): SseEvent | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(data) as Record<string, unknown>;
  } catch {
    return null;
  }

  switch (eventType) {
    case 'phase_started':
      return { ...parsed, type: 'phase_started' } as SseEvent;
    case 'phase_completed':
      return { ...parsed, type: 'phase_completed' } as SseEvent;
    case 'status_changed':
      return { ...parsed, type: 'status_changed' } as SseEvent;
    case 'error':
      return { ...parsed, type: 'error' } as SseEvent;
    default:
      return null;
  }
}

// ── Hook ──────────────────────────────────────────────────────────────────────

/**
 * Subscribe to live SSE events for a pipeline run.
 *
 * Opens a `GET /api/v1/runs/{runId}/stream` `EventSource` connection and
 * accumulates events into `events[]`.  The connection is closed automatically
 * when the run reaches a terminal state, and on component unmount.
 *
 * Re-mounts (i.e. a new `EventSource`) whenever `runId` changes.
 *
 * @param runId  Pipeline run ID (8-char UUID prefix).
 * @returns      `{ events, status, connected }`.
 */
export function useRunEvents(runId: string): UseRunEventsResult {
  const [events, setEvents] = useState<SseEvent[]>([]);
  const [status, setStatus] = useState<RunEventStatus>('connecting');
  const [connected, setConnected] = useState<boolean>(false);

  // Keep a ref to the EventSource so the cleanup closure always has the
  // latest instance even across strict-mode double-invocations.
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    // Reset state for the new runId.
    setEvents([]);
    setStatus('connecting');
    setConnected(false);

    const url = `${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/stream`;
    const es = new EventSource(url);
    esRef.current = es;

    // ── Connection open ──
    es.onopen = () => {
      setConnected(true);
    };

    // ── Connection error ──
    es.onerror = () => {
      setConnected(false);
      // Do not change `status` here — the server may reconnect; a terminal
      // status_changed event is the authoritative signal to stop.
    };

    // ── Named event listeners ──
    for (const eventType of SSE_EVENT_TYPES) {
      es.addEventListener(eventType, (e: MessageEvent) => {
        const typed = parseRawEvent(eventType, e.data as string);
        if (typed === null) return;

        // Accumulate events.
        setEvents((prev) => [...prev, typed]);

        // Update derived status.
        if (typed.type === 'phase_started' || typed.type === 'phase_completed') {
          setStatus('running');
        } else if (typed.type === 'status_changed') {
          const newStatus = deriveStatus(typed.status);
          setStatus(newStatus);

          // Auto-close on terminal status.
          if (TERMINAL_RUN_STATUSES.has(typed.status)) {
            setConnected(false);
            es.close();
          }
        }
      });
    }

    // ── Cleanup (unmount or runId change) ──
    return () => {
      es.close();
      esRef.current = null;
      setConnected(false);
    };
  }, [runId]);

  return { events, status, connected };
}
