/**
 * PhaseEventRow — a single phase completion/error row in the run detail view.
 *
 * Renders:
 *   - Status icon (✓ success / ✗ error)
 *   - Phase name and ID
 *   - Token counts, cost, elapsed time
 *   - Collapsible output preview
 */

"use client";

import { useState } from "react";
import type {
  SsePhaseCompleteEvent,
  SsePhaseErrorEvent,
} from "@/lib/types";
import { Badge } from "@/components/ui/Badge";

interface Props {
  event: SsePhaseCompleteEvent | SsePhaseErrorEvent;
  /** Full output text for this phase (from /api/run/:id/outputs) */
  output?: string;
}

/**
 * Render a phase progress row with expandable output.
 */
export function PhaseEventRow({ event, output }: Props) {
  const [expanded, setExpanded] = useState(false);

  const isError = event.type === "phase_error";
  const preview =
    "output_preview" in event ? event.output_preview : null;
  const errorMessage =
    "error_message" in event ? event.error_message : null;

  const hasOutput = Boolean(output || preview);

  return (
    <div
      className={[
        "rounded-md border px-4 py-3 text-sm",
        isError
          ? "border-status-error/30 bg-status-error/5"
          : "border-border bg-surface-card",
      ].join(" ")}
    >
      {/* Row header */}
      <div className="flex flex-wrap items-start gap-3">
        {/* Status icon */}
        <span
          className={[
            "mt-0.5 shrink-0 font-mono text-base leading-none",
            isError ? "text-status-error" : "text-status-success",
          ].join(" ")}
          aria-label={isError ? "Failed" : "Completed"}
        >
          {isError ? "✗" : "✓"}
        </span>

        {/* Phase name + ID */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-text-primary">
              {event.phase_name || event.phase_id}
            </span>
            {event.phase_name !== event.phase_id && (
              <code className="rounded bg-surface-elevated px-1 py-0.5 font-mono text-xs text-text-muted">
                {event.phase_id}
              </code>
            )}
            <Badge variant={isError ? "error" : "success"}>
              {event.status}
            </Badge>
          </div>

          {/* Error message */}
          {isError && errorMessage && (
            <p className="mt-1 text-xs text-status-error">{errorMessage}</p>
          )}
        </div>

        {/* Metrics */}
        <div className="flex shrink-0 flex-wrap gap-3 text-xs text-text-muted">
          {"tokens_in" in event && (
            <span title="Tokens in / out">
              ↑{event.tokens_in.toLocaleString()} ↓
              {event.tokens_out.toLocaleString()}
            </span>
          )}
          {"cost_usd" in event && event.cost_usd > 0 && (
            <span>${event.cost_usd.toFixed(6)}</span>
          )}
          {"elapsed_seconds" in event && (
            <span>{event.elapsed_seconds}s</span>
          )}
        </div>
      </div>

      {/* Expandable output */}
      {!isError && hasOutput && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs text-text-muted hover:text-text-secondary transition-colors"
          >
            {expanded ? "▲ Hide output" : "▼ Show output"}
          </button>

          {expanded && (
            <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-surface-elevated p-3 text-xs text-text-secondary whitespace-pre-wrap break-words">
              {output ?? preview}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
