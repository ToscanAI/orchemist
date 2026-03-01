/**
 * SSE (Server-Sent Events) hook for subscribing to live run progress.
 *
 * Encapsulates all EventSource lifecycle management so consumers don't need
 * to deal with global state or manual cleanup:
 *
 *   const { events, status } = useRunEvents(runId);
 *
 * Features:
 *   - Parses and types each event payload
 *   - Closes the connection automatically when pipeline is complete
 *   - Exposes a `close()` function for manual cleanup
 *   - Does nothing server-side (SSR safe)
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { RunStatus, SseEvent } from "./types";

// ─── State shape ─────────────────────────────────────────────────────────────

export interface UseRunEventsState {
  /** All events received so far, in order */
  events: SseEvent[];
  /** Current run status derived from events */
  status: RunStatus;
  /** Whether the SSE connection is currently open */
  connected: boolean;
  /** Close the connection manually */
  close: () => void;
}

// ─── Terminal event types ─────────────────────────────────────────────────────

/** Set of SSE event types that signal the pipeline has finished */
const TERMINAL_TYPES = new Set(["pipeline_complete", "error", "aborted"]);

// ─── Hook ─────────────────────────────────────────────────────────────────────

/**
 * Subscribe to SSE progress events for a pipeline run.
 *
 * @param runId - The run ID returned by POST /api/run, or null/undefined to
 *   skip connecting (useful when runId is not yet known).
 */
export function useRunEvents(runId: string | null | undefined): UseRunEventsState {
  const [events, setEvents] = useState<SseEvent[]>([]);
  const [status, setStatus] = useState<RunStatus>("starting");
  const [connected, setConnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  const close = useCallback(() => {
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
      setConnected(false);
    }
  }, []);

  useEffect(() => {
    if (!runId) return;
    // EventSource is browser-only; bail out during SSR.
    if (typeof EventSource === "undefined") return;

    const source = new EventSource(`/api/run/${encodeURIComponent(runId)}/status`);
    sourceRef.current = source;
    setConnected(true);
    setEvents([]);
    setStatus("starting");

    source.onmessage = (ev: MessageEvent<string>) => {
      let parsed: SseEvent;
      try {
        parsed = JSON.parse(ev.data) as SseEvent;
      } catch {
        // Ignore malformed events
        return;
      }

      setEvents((prev) => [...prev, parsed]);

      // Update run status from event type
      switch (parsed.type) {
        case "start":
          setStatus("running");
          break;
        case "paused":
          setStatus("paused");
          break;
        case "complete":
          setStatus("completed");
          break;
        case "aborted":
          setStatus("aborted");
          break;
        case "error":
          setStatus("error");
          break;
        case "pipeline_complete":
          setStatus(parsed.status);
          break;
        default:
          break;
      }

      // Close connection after terminal events
      if (TERMINAL_TYPES.has(parsed.type)) {
        source.close();
        sourceRef.current = null;
        setConnected(false);
      }
    };

    source.onerror = (ev: Event) => {
      // EventSource has built-in exponential-backoff reconnect logic.
      // Only close the connection on terminal errors (CLOSED readyState),
      // which means the browser has given up retrying.  For transient network
      // blips (CONNECTING / OPEN readyState at error time), leave the source
      // open so the browser can reconnect automatically.
      if (source.readyState === EventSource.CLOSED) {
        source.close();
        sourceRef.current = null;
        setConnected(false);
      }
    };

    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [runId]);

  return { events, status, connected, close };
}
