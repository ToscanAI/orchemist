/**
 * RunStatusBadge — coloured status pill for a pipeline run's current state.
 *
 * Accepts a free-form status string (backend `RunStatus` values, SSE-derived
 * statuses, or custom strings) and maps it to the correct `Badge` variant.
 * Comparison is case-insensitive.
 *
 * @module
 */

import React from 'react';
import { Badge } from '@/components/ui/Badge';
import type { BadgeVariant } from '@/components/ui/Badge';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface RunStatusBadgeProps {
  /** Pipeline run status string. Accepts any casing; compared lowercase. */
  status: string;
  /** Optional additional Tailwind class overrides forwarded to Badge. */
  className?: string;
}

// ── Status → variant mapping ──────────────────────────────────────────────────

/**
 * Maps a status string to a `BadgeVariant`.
 *
 * Covers all backend `RunStatus` values plus SSE-derived statuses
 * (`connecting`, `running`, `aborted`). Falls back to `neutral`.
 */
function statusToVariant(status: string): BadgeVariant {
  switch (status.toLowerCase()) {
    case 'connecting':
    case 'pending':
      return 'neutral';
    case 'running':
      return 'info';
    case 'paused':
      return 'warning';
    case 'completed':
    case 'success':
      return 'success';
    case 'error':
    case 'failed':
    case 'aborted':
    case 'cancelled':
    case 'crashed':
    case 'scoring_failed':
      return 'error';
    default:
      return 'neutral';
  }
}

/**
 * Returns extra Tailwind classes for a given status.
 *
 * Currently only `running` gets `animate-pulse` to signal active progress.
 */
function statusExtraClass(status: string): string {
  switch (status.toLowerCase()) {
    case 'running':
      return 'animate-pulse';
    default:
      return '';
  }
}

/**
 * Returns a human-readable label for a status string.
 *
 * Capitalises the first letter; replaces underscores with spaces.
 */
function statusLabel(status: string): string {
  return status
    .replace(/_/g, ' ')
    .replace(/^./, (c) => c.toUpperCase());
}

// ── Component ─────────────────────────────────────────────────────────────────

/**
 * Displays a pipeline run status as a colored `Badge`.
 *
 * Status → Badge variant mapping:
 * - connecting / pending  → neutral (muted)
 * - running               → info (pulsing)
 * - paused                → warning
 * - completed / success   → success
 * - error / failed / aborted / cancelled / crashed / scoring_failed → error
 *
 * @example
 * <RunStatusBadge status="running" />
 * // → pulsing "Running" badge
 *
 * @example
 * <RunStatusBadge status="completed" className="ml-2" />
 * // → green "Completed" badge with margin
 *
 * @example
 * <RunStatusBadge status="scoring_failed" />
 * // → red "Scoring failed" badge
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
