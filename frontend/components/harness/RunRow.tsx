'use client';

/**
 * RunRow — single in-flight run row used in the Fleet Dashboard's runs table.
 * Renders repo, issue, current phase (with progress hint), model tier, cost,
 * ETA, and confidence trend indicator.
 *
 * Clicks navigate to the Run Cockpit.
 */

import Link from 'next/link';
import type { RunRecord } from '@/lib/types';
import { StatusDot } from './StatusDot';

interface RunRowProps {
  readonly run: RunRecord;
  /** Total phases in the run's template (best-effort; falls back to 10 if unknown). */
  readonly totalPhases?: number;
  /** Optional pre-computed confidence; engine emits this in SSE if present. */
  readonly confidence?: number;
  /** Optional model tier label for the phase currently running. */
  readonly modelTier?: string;
  /** Optional cumulative cost in USD. */
  readonly costUsd?: number;
  /** Optional ETA hint string. */
  readonly etaLabel?: string;
}

function phaseChipTone(state: string): 'info' | 'warning' | 'success' | 'danger' | 'neutral' {
  if (state.includes('adversary') || state.includes('review')) return 'warning';
  if (state.includes('test') || state.includes('verify')) return 'success';
  if (state.includes('inventory') || state.includes('phase0') || state === 'existing_symbols_inventory') return 'success';
  if (state.includes('implement') || state.includes('fix') || state.includes('spec')) return 'info';
  return 'neutral';
}

function chipClasses(tone: 'info' | 'warning' | 'success' | 'danger' | 'neutral'): string {
  switch (tone) {
    case 'info': return 'border-harness-purple bg-[#1F2C3B]';
    case 'warning': return 'border-harness-warning bg-[#3B2E1F]';
    case 'success': return 'border-harness-teal bg-[#1B2A1F]';
    case 'danger': return 'border-harness-danger bg-[#2A1F1F]';
    case 'neutral': return 'border-harness-border bg-harness-surface3';
  }
}

function confidenceTrend(conf?: number): { value: string; tone: 'success' | 'warning' | 'danger' | 'neutral'; arrow: string } {
  if (conf === undefined) return { value: '—', tone: 'neutral', arrow: '' };
  if (conf >= 0.85) return { value: conf.toFixed(2), tone: 'success', arrow: '▲' };
  if (conf >= 0.7) return { value: conf.toFixed(2), tone: 'warning', arrow: '▲' };
  return { value: conf.toFixed(2), tone: 'danger', arrow: '▼' };
}

export function RunRow({
  run,
  totalPhases = 10,
  confidence,
  modelTier,
  costUsd,
  etaLabel,
}: RunRowProps) {
  const completedCount = run.completed_phases.length;
  const phaseLabel = run.current_phase ?? 'idle';
  const phaseTone = phaseChipTone(phaseLabel);
  const conf = confidenceTrend(confidence);

  return (
    <Link
      href={`/runs/${encodeURIComponent(run.run_id)}`}
      className="group grid grid-cols-12 items-center gap-3 px-4 py-3 text-[12px] border-b border-harness-border hover:bg-harness-surface3 transition-colors"
      data-testid={`run-row-${run.run_id}`}
    >
      <div className="col-span-3 truncate text-harness-text">{run.template_id}</div>
      <div className="col-span-1 truncate text-harness-muted font-mono">{run.run_id.slice(0, 7)}</div>
      <div className="col-span-3">
        <span
          className={[
            'inline-flex items-center rounded px-2 py-1 text-[10px] font-medium border',
            chipClasses(phaseTone),
          ].join(' ')}
        >
          {completedCount}/{totalPhases} {phaseLabel}
        </span>
      </div>
      <div className="col-span-1 text-harness-muted">{modelTier ?? 'sonnet'}</div>
      <div className="col-span-1 text-harness-text">{costUsd !== undefined ? `$${costUsd.toFixed(2)}` : '—'}</div>
      <div className="col-span-1 text-harness-muted">{etaLabel ?? '—'}</div>
      <div className="col-span-2 text-right">
        <span className={[
          'inline-flex items-center gap-1 text-[12px] font-semibold',
          conf.tone === 'success' ? 'text-harness-teal' :
          conf.tone === 'warning' ? 'text-harness-warning' :
          conf.tone === 'danger' ? 'text-harness-danger' :
          'text-harness-muted',
        ].join(' ')}>
          {conf.value} {conf.arrow}
        </span>
      </div>
    </Link>
  );
}
