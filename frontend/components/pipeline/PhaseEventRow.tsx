/**
 * PhaseEventRow — a single completed or failed phase entry in the run timeline.
 *
 * Renders phase name, status icon + badge, token/cost/duration metrics,
 * and an expandable output preview. Null metric values are shown as `—`.
 *
 * This component defines its own `PhaseEventRowProps` interface because the
 * SSE types in `frontend/lib/types.ts` do not carry enriched per-phase data
 * (e.g. `tokens_in`/`tokens_out`, `elapsed_seconds`, `output_preview`).
 * A parent component is expected to combine SSE events with template metadata
 * and pass the result as structured props here.
 *
 * @module
 */

import React from 'react';
import { Badge } from '@/components/ui/Badge';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface PhaseEventRowProps {
  /** Phase name (display label). */
  phaseName: string;
  /** `'completed'` or `'error'` — determines icon and badge variant. */
  phaseStatus: 'completed' | 'error';
  /** Tokens consumed going into the phase (input tokens). Null shown as `—`. */
  tokensIn: number | null;
  /** Tokens produced by the phase (output tokens). Null shown as `—`. */
  tokensOut: number | null;
  /** Monetary cost of the phase in USD. Null shown as `—`. */
  costUsd: number | null;
  /** Wall-clock duration in seconds. Null shown as `—`. */
  elapsedSeconds: number | null;
  /** Optional output text/content to show in the expandable preview. */
  outputPreview?: string | null;
}

// ── Formatting helpers ────────────────────────────────────────────────────────

/** Format an integer token count with thousands separators, or return `—`. */
function fmtTokens(n: number | null): string {
  if (n === null) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

/** Format a USD cost to 4 decimal places, or return `—`. */
function fmtCost(n: number | null): string {
  if (n === null) return '—';
  return `$${n.toFixed(4)}`;
}

/** Format elapsed seconds to one decimal place with `s` suffix, or return `—`. */
function fmtElapsed(n: number | null): string {
  if (n === null) return '—';
  return `${n.toFixed(1)}s`;
}

/** Maximum characters shown in the output preview before truncation. */
const PREVIEW_MAX_CHARS = 500;

// ── Component ─────────────────────────────────────────────────────────────────

/**
 * A single row in the run phase timeline.
 *
 * Layout:
 * ```
 * [icon] [phase name] [Badge]  tokens_in → tokens_out  $cost  Xs
 *   ↳ [expandable output preview]
 * ```
 *
 * The output preview is only rendered when `outputPreview` is a non-empty
 * string. Long previews are truncated to {@link PREVIEW_MAX_CHARS} characters
 * with an ellipsis appended.
 *
 * @example
 * <PhaseEventRow
 *   phaseName="research"
 *   phaseStatus="completed"
 *   tokensIn={1200}
 *   tokensOut={800}
 *   costUsd={0.0034}
 *   elapsedSeconds={12.4}
 *   outputPreview="Phase output text…"
 * />
 */
export function PhaseEventRow({
  phaseName,
  phaseStatus,
  tokensIn,
  tokensOut,
  costUsd,
  elapsedSeconds,
  outputPreview,
}: PhaseEventRowProps) {
  const isCompleted = phaseStatus === 'completed';

  const statusIcon = isCompleted ? (
    <span className="text-green-500 font-bold" aria-hidden="true">✓</span>
  ) : (
    <span className="text-red-500 font-bold" aria-hidden="true">✗</span>
  );

  const preview =
    outputPreview && outputPreview.length > PREVIEW_MAX_CHARS
      ? `${outputPreview.slice(0, PREVIEW_MAX_CHARS)}…`
      : outputPreview;

  return (
    <div
      className="bg-surface-2 border border-surface-3 rounded-lg overflow-hidden"
      role="listitem"
    >
      {/* Main row */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
        {/* Icon + phase name + badge */}
        <span className="flex items-center gap-2 min-w-0 flex-1">
          {statusIcon}
          <span
            className="font-mono text-sm text-content-primary truncate"
            title={phaseName}
          >
            {phaseName}
          </span>
          <Badge variant={isCompleted ? 'success' : 'error'}>
            {isCompleted ? 'completed' : 'error'}
          </Badge>
        </span>

        {/* Metrics: tokens in → out, cost, duration */}
        <span className="flex items-center gap-4 text-xs text-content-secondary whitespace-nowrap">
          {/* Token throughput */}
          <span title="tokens in → tokens out">
            {fmtTokens(tokensIn)}
            <span className="mx-1 opacity-50">→</span>
            {fmtTokens(tokensOut)}
          </span>

          {/* Cost */}
          <span title="cost (USD)">{fmtCost(costUsd)}</span>

          {/* Duration */}
          <span title="elapsed time">{fmtElapsed(elapsedSeconds)}</span>
        </span>
      </div>

      {/* Expandable output preview — only rendered when preview text exists */}
      {preview && (
        <details className="border-t border-surface-3">
          <summary className="px-4 py-2 text-xs text-content-secondary cursor-pointer hover:bg-surface-3 select-none">
            Output preview
          </summary>
          <pre className="px-4 py-3 text-xs text-content-secondary whitespace-pre-wrap break-words overflow-auto max-h-64 bg-surface-1">
            {preview}
          </pre>
        </details>
      )}
    </div>
  );
}
