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
import { BASE_URL } from '@/lib/api';

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
 * - `error`       — `status_changed` with `status` in `{'failed','budget_exceeded','scoring_failed'}`.
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

/**
 * `BASE_URL` is imported from `@/lib/api` (issue #773) — single source of
 * truth. Re-declaring it here would diverge the two modules when the env
 * variable changes.
 */

/**
 * `RunStatus` values that indicate the pipeline has reached a terminal state.
 * When a `status_changed` event carries one of these, the `EventSource` is
 * closed automatically.
 */
const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'success',
  'failed',
  'cancelled',
  'budget_exceeded',
  'scoring_failed',
]);

/** Named SSE event types emitted by `GET /api/v1/runs/{run_id}/stream`. */
const SSE_EVENT_TYPES = [
  'phase_started',
  'phase_completed',
  'status_changed',
  'error',
] as const;

/** Same set, used by the runtime validator for membership testing. */
const KNOWN_SSE_TYPES: ReadonlySet<string> = new Set(SSE_EVENT_TYPES);

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
    case 'budget_exceeded':
    case 'scoring_failed':
      return 'error';
    case 'cancelled':
      return 'aborted';
    case 'pending_review':
      // Awaiting human gate decision — treat as still running from the
      // hook's perspective so the EventSource doesn't close.
      return 'running';
    case 'pending':
    case 'running':
      return 'running';
  }
}

/**
 * Runtime validator for SSE events (issue #773).
 *
 * Returns the validated event or `null` if the raw payload is not a plain
 * object, lacks a non-empty string `run_id` (for run-scoped events), or has
 * a `type` not in the known set. Extra fields are tolerated.
 *
 * The stream-level `error` event (`SseStreamErrorEvent`) is the only type
 * that does not carry a `run_id` — the engine emits these before the run id
 * is established. For that type only, `run_id` is not required.
 *
 * EXPORTED for direct testing (acceptance tests call this; production code
 * reaches it via `parseRawEvent`).
 */
export function validateSseEvent(raw: unknown): SseEvent | null {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const obj = raw as Record<string, unknown>;
  const type = obj['type'];
  if (typeof type !== 'string' || !KNOWN_SSE_TYPES.has(type)) {
    return null;
  }
  // Stream-level error events do not carry run_id (existing engine behavior).
  if (type === 'error') {
    return obj as unknown as SseEvent;
  }
  // All other event types must have a non-empty string run_id.
  const runId = obj['run_id'];
  if (typeof runId !== 'string' || runId.length === 0) {
    return null;
  }
  return obj as unknown as SseEvent;
}

/**
 * Parse a raw SSE message body into a typed `SseEvent`.
 *
 * Accepts either:
 *  - A JSON string (e.g. `MessageEvent.data` from EventSource), in which case
 *    the body is JSON-parsed first.
 *  - An already-parsed object (e.g. a test fixture).
 *
 * The event MUST contain a non-empty string `run_id` and a known `type`. If
 * those constraints fail, the function returns `null` and emits a single
 * `console.warn` so consumers can skip the event without crashing on unsafe
 * casts (issue #773).
 *
 * EXPORTED for direct testing.
 */
export function parseRawEvent(data: string | unknown): SseEvent | null {
  let parsed: unknown;
  if (typeof data === 'string') {
    try {
      parsed = JSON.parse(data);
    } catch (err) {
      console.error('[sse] malformed JSON in SSE event body', err);
      return null;
    }
  } else {
    parsed = data;
  }
  const validated = validateSseEvent(parsed);
  if (validated === null) {
    console.warn('[sse] dropping invalid SSE event', parsed);
    return null;
  }
  return validated;
}

/**
 * Internal helper used by the EventSource named-event listener path. The
 * EventSource API splits the named event type from the data body, so this
 * variant attaches the type before validation (the type may be missing
 * from the JSON body itself).
 */
function parseNamedEvent(eventType: string, data: string): SseEvent | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(data) as Record<string, unknown>;
  } catch (err) {
    console.error('[sse] malformed JSON in SSE event body', err);
    return null;
  }
  // The named event type from the EventSource layer overrides any in-body
  // `type` field (the server is the source of truth here). After merging,
  // run the same validator so we never deliver an unsafe cast.
  const merged: Record<string, unknown> = { ...parsed, type: eventType };
  const validated = validateSseEvent(merged);
  if (validated === null) {
    console.warn('[sse] dropping invalid SSE event', merged);
    return null;
  }
  return validated;
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
 * @param runId    Pipeline run ID (8-char UUID prefix).
 * @param enabled  Whether to open the SSE connection. Default `true`.
 *                 Pass `false` for completed/historical runs to avoid
 *                 unnecessary HTTP connections.
 * @returns        `{ events, status, connected }`.
 */
export function useRunEvents(runId: string, enabled = true): UseRunEventsResult {
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

    if (!enabled) return;

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
        const typed = parseNamedEvent(eventType, e.data as string);
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
  }, [runId, enabled]);

  return { events, status, connected };
}
