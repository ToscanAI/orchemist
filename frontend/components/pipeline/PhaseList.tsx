/**
 * PhaseList — ordered list of pipeline phase execution steps.
 *
 * Pure presentational component — receives a readonly array of `PhaseDetail`
 * objects and renders them as a numbered ordered list. No internal state,
 * no API calls, no `'use client'` directive (server-component-compatible).
 *
 * Used in `frontend/app/templates/[id]/page.tsx` to display the execution
 * plan for a pipeline template.
 *
 * @module
 */

import type { PhaseDetail } from '@/lib/types';
import { Badge } from '@/components/ui/Badge';
import type { BadgeVariant } from '@/components/ui/Badge';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PhaseListProps {
  /** Ordered list of phases to render. */
  phases: readonly PhaseDetail[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Map a `model_tier` string to the appropriate Badge variant.
 *
 * Colour semantics mirror cost/capability:
 * - opus   → error   (red — highest tier, most expensive)
 * - sonnet → warning (amber — mid tier)
 * - haiku  → info    (blue — lightweight / fast)
 * - other  → neutral (grey — unknown / custom)
 */
function tierVariant(tier: string): BadgeVariant {
  if (tier === 'opus') return 'error';
  if (tier === 'sonnet') return 'warning';
  if (tier === 'haiku') return 'info';
  return 'neutral';
}

// ---------------------------------------------------------------------------
// PhaseList component
// ---------------------------------------------------------------------------

/**
 * Renders an ordered list of pipeline phases with model tier badges,
 * task type metadata, and dependency references.
 *
 * Empty state: displays a text message instead of an empty `<ol>`.
 *
 * @example
 * <PhaseList phases={template.phases} />
 */
export function PhaseList({ phases }: PhaseListProps) {
  // Empty state
  if (phases.length === 0) {
    return (
      <p className="text-sm text-content-tertiary">
        No phases defined for this template.
      </p>
    );
  }

  return (
    <ol className="flex flex-col gap-3" aria-label="Phase execution plan">
      {phases.map((phase, index) => (
        <li
          key={phase.id}
          className="card flex flex-col gap-2"
          aria-label={`Phase ${index + 1}: ${phase.name}`}
        >
          {/* Phase header: 1-based index + name + model tier badge */}
          <div className="flex items-center gap-2">
            <span
              className="flex h-6 w-6 items-center justify-center rounded-full bg-surface-3 text-xs font-semibold text-content-primary shrink-0"
              aria-hidden="true"
            >
              {index + 1}
            </span>
            <span className="text-sm font-semibold text-content-primary">
              {phase.name}
            </span>
            <Badge variant={tierVariant(phase.model_tier)}>
              {phase.model_tier}
            </Badge>
          </div>

          {/* Description (optional) */}
          {phase.description && (
            <p className="text-xs text-content-secondary leading-relaxed">
              {phase.description}
            </p>
          )}

          {/* Metadata row: task_type + depends_on */}
          <div className="flex flex-wrap items-center gap-3 text-xs text-content-tertiary">
            <span>
              Type:{' '}
              <span className="text-content-primary">{phase.task_type}</span>
            </span>
            {phase.depends_on.length > 0 && (
              <span>
                Depends on:{' '}
                <span className="text-content-primary">
                  {phase.depends_on.join(', ')}
                </span>
              </span>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
