/**
 * RunStatusBadge — coloured status pill for a pipeline run's current state.
 *
 * Status → variant mapping is **exhaustive** on the canonical `RunStatus`
 * union (lib/types.ts). The switch covers every backend status emitted by
 * the engine; TypeScript will surface a compile-time error if a new status
 * is added to the union without a mapping here. There is no silent
 * fall-through to neutral for unknown values (cleanup #811).
 *
 * The function still accepts any string (SSE-derived statuses like
 * `connecting` / `aborted` arrive as strings) — those map to neutral or
 * error explicitly.
 *
 * @module
 */

import React from 'react';
import { Badge } from '@/components/ui/Badge';
import type { BadgeVariant } from '@/components/ui/Badge';
import type { RunStatus } from '@/lib/types';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface RunStatusBadgeProps {
  /** Pipeline run status string. Accepts any casing; compared lowercase. */
  status: string;
  /** Optional additional Tailwind class overrides forwarded to Badge. */
  className?: string;
}

// ── Status → variant mapping ──────────────────────────────────────────────────

/** Canonical `RunStatus` mapping. Exhaustive — every union member listed. */
const STATUS_VARIANT: Record<RunStatus, BadgeVariant> = {
  pending: 'neutral',
  running: 'info',
  success: 'success',
  failed: 'error',
  cancelled: 'error',
  crashed: 'error',
  budget_exceeded: 'warning',
  scoring_failed: 'error',
  pending_review: 'warning',
  rejected: 'error',
  escalated: 'warning',
};

/** SSE-derived and legacy statuses that may arrive as strings but aren't in `RunStatus`. */
const NON_CANONICAL_VARIANT: Record<string, BadgeVariant> = {
  // SSE stream lifecycle (lib/sse.ts)
  connecting: 'neutral',
  aborted: 'error',
  // Daemon-side states that may surface in older records
  paused: 'warning',
  completed: 'success', // legacy alias for 'success'
  error: 'error',
  permanently_failed: 'error',
};

function statusToVariant(status: string): BadgeVariant {
  const key = status.toLowerCase();
  if (key in STATUS_VARIANT) {
    return STATUS_VARIANT[key as RunStatus];
  }
  if (key in NON_CANONICAL_VARIANT) {
    return NON_CANONICAL_VARIANT[key]!;
  }
  // Unknown status — log once in dev so it's discoverable; render error
  // (not neutral) so the operator sees that something unexpected happened.
  if (typeof console !== 'undefined' && process.env['NODE_ENV'] !== 'production') {
    console.warn(`[RunStatusBadge] unknown status: "${status}"`);
  }
  return 'error';
}

/** Active-state pulse animation. */
function statusExtraClass(status: string): string {
  return status.toLowerCase() === 'running' ? 'animate-pulse' : '';
}

/** Human-readable label: snake_case → "Title case". */
function statusLabel(status: string): string {
  return status
    .replace(/_/g, ' ')
    .replace(/^./, (c) => c.toUpperCase());
}

// ── Component ─────────────────────────────────────────────────────────────────

/**
 * Displays a pipeline run status as a colored `Badge`.
 *
 * Canonical statuses (from `RunStatus` in `lib/types.ts`):
 * - pending          → neutral
 * - running          → info (pulsing)
 * - success          → success
 * - failed           → error
 * - cancelled        → error
 * - crashed          → error
 * - budget_exceeded  → warning
 * - scoring_failed   → error
 * - pending_review   → warning
 * - rejected         → error
 * - escalated        → warning
 *
 * Non-canonical fall-back: SSE stream events (connecting / aborted),
 * legacy aliases (completed / error), and daemon transient states
 * (paused / permanently_failed). Unknown values render `error` and log
 * a console warning in non-production builds.
 */
export function RunStatusBadge({ status, className = '' }: RunStatusBadgeProps) {
  const variant = statusToVariant(status);
  const extra = statusExtraClass(status);
  const combined = [extra, className].filter(Boolean).join(' ');

  return (
    <Badge variant={variant} className={combined || undefined}>
      {statusLabel(status)}
    </Badge>
  );
}
