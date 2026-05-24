'use client';

/**
 * AutonomyRamp — vertical timeline showing current autonomy level and the
 * unlock criteria for the next level. Matches the Fleet Dashboard SVG (right
 * column, screen 1) and is reused as a sidebar widget elsewhere.
 *
 * Levels mirror ROADMAP.md:
 *   L3 — review-all (every PR human-reviewed)
 *   L4 — auto-merge at threshold ≥ 0.90
 *   L4.3 — current intermediate posture (after #794 / cross-model adversary track)
 *   L5 — dark factory: full Tuesday-morning scenario
 */

import Link from 'next/link';

interface Step {
  readonly level: string;
  readonly title: string;
  readonly subtitle?: string;
  readonly status: 'past' | 'current' | 'future';
}

const STEPS: readonly Step[] = [
  { level: '3', title: 'Level 3 · review-all', subtitle: 'reached 2026-03-04', status: 'past' },
  { level: '4', title: 'Level 4 · auto-merge ≥ 0.90', subtitle: 'reached 2026-04-21', status: 'past' },
  { level: '4.3', title: 'Level 4.3 · current', subtitle: 'unlock L5 needs fleet UI · stale detect · multi-repo', status: 'current' },
  { level: '5', title: 'Level 5 · dark factory', status: 'future' },
];

export function AutonomyRamp() {
  return (
    <div className="relative" data-testid="autonomy-ramp">
      <div className="absolute left-[11px] top-1 bottom-1 w-[2px] bg-harness-border" />
      <ul className="flex flex-col gap-7">
        {STEPS.map((step) => (
          <li key={step.level} className="relative pl-9">
            <span
              className={[
                'absolute left-[3px] top-0 inline-flex h-5 w-5 items-center justify-center rounded-full',
                step.status === 'past'
                  ? 'bg-harness-teal text-[#0B0D10]'
                  : step.status === 'current'
                  ? 'bg-harness-warning text-[#0B0D10] ring-2 ring-[#0B0D10] animate-pulse-soft'
                  : 'border-2 border-harness-dim text-harness-dim',
              ].join(' ')}
            >
              <span className="text-[9px] font-extrabold">{step.level}</span>
            </span>
            <div
              className={[
                'text-[13px] font-semibold leading-tight',
                step.status === 'current'
                  ? 'text-harness-warning'
                  : step.status === 'future'
                  ? 'text-harness-muted'
                  : 'text-harness-text',
              ].join(' ')}
            >
              {step.title}
            </div>
            {step.subtitle && (
              <div className="mt-1 text-[10px] text-harness-dim">{step.subtitle}</div>
            )}
          </li>
        ))}
      </ul>
      <Link
        href="/admin"
        className="mt-6 block h-button h-button-primary text-center w-full border-dashed"
        style={{ borderStyle: 'dashed' }}
        data-testid="autonomy-promote-link"
      >
        Request promotion to Level 5 · blocked
      </Link>
    </div>
  );
}
