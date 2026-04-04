'use client';

/**
 * ObserverPanel — read-only heuristic observer for pipeline runs.
 *
 * Consumes SSE events and applies deterministic rules to surface anomalies,
 * slow phases, high token usage, and cost tracking. Zero mutation access.
 *
 * @module
 */

import { useState, useMemo } from 'react';
import type { SseEvent, SsePhaseCompletedEvent } from '@/lib/types';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Severity = 'info' | 'warning' | 'alert';

interface Observation {
  id: string;
  severity: Severity;
  message: string;
  timestamp: string;
}

// ---------------------------------------------------------------------------
// Heuristic thresholds
// ---------------------------------------------------------------------------

const SLOW_PHASE_RATIO = 2.0; // warn if >2x tier average
const HIGH_TOKENS_HAIKU = 30_000; // haiku phases >30k tokens
const HIGH_TOKENS_SONNET = 80_000; // sonnet phases >80k tokens
const HIGH_TOKENS_OPUS = 120_000; // opus phases >120k tokens

const TOKEN_THRESHOLDS: Record<string, number> = {
  haiku: HIGH_TOKENS_HAIKU,
  sonnet: HIGH_TOKENS_SONNET,
  opus: HIGH_TOKENS_OPUS,
};

// ---------------------------------------------------------------------------
// Severity icons
// ---------------------------------------------------------------------------

const SEVERITY_ICON: Record<Severity, string> = {
  info: 'ℹ️',
  warning: '⚠️',
  alert: '🔴',
};

const SEVERITY_COLOR: Record<Severity, string> = {
  info: 'text-sky-400',
  warning: 'text-amber-400',
  alert: 'text-red-400',
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ObserverPanelProps {
  events: readonly SseEvent[];
}

export function ObserverPanel({ events }: ObserverPanelProps) {
  const [expanded, setExpanded] = useState(false);

  const observations = useMemo(() => {
    const obs: Observation[] = [];
    let obsId = 0;

    // Track per-tier elapsed times for averaging
    const tierElapsed: Record<string, number[]> = {};
    let totalCost = 0;
    let totalTokens = 0;
    let phaseCount = 0;
    let failedPhases = 0;

    for (const event of events) {
      if (event.type !== 'phase_completed') continue;
      const e = event as SsePhaseCompletedEvent;
      phaseCount++;

      const tier = e.model_tier ?? 'unknown';
      const elapsed = e.elapsed_seconds;
      const tokens = e.tokens_consumed ?? 0;
      const cost = e.cost_usd ?? 0;
      const phaseId = e.phase_id ?? 'unknown';
      const phaseName = e.phase_name ?? phaseId;
      const timestamp = e.created_at ?? new Date().toISOString();

      totalCost += cost;
      totalTokens += tokens;

      // Track failed phases
      if (e.state === 'failed' || e.state === 'error') {
        failedPhases++;
        obs.push({
          id: String(++obsId),
          severity: 'alert',
          message: `Phase '${phaseName}' failed with state '${e.state}'.`,
          timestamp,
        });
      }

      // Slow phase detection
      if (elapsed !== null && elapsed !== undefined) {
        if (!tierElapsed[tier]) tierElapsed[tier] = [];

        // Check against existing average BEFORE adding this one
        const existing = tierElapsed[tier];
        if (existing.length >= 1) {
          const avg = existing.reduce((a, b) => a + b, 0) / existing.length;
          const ratio = elapsed / avg;
          if (ratio > SLOW_PHASE_RATIO) {
            obs.push({
              id: String(++obsId),
              severity: 'warning',
              message: `Phase '${phaseName}' took ${elapsed.toFixed(1)}s — ${ratio.toFixed(1)}x slower than the ${tier} average (${avg.toFixed(1)}s).`,
              timestamp,
            });
          }
        }

        tierElapsed[tier].push(elapsed);
      }

      // High token usage for tier
      const threshold = TOKEN_THRESHOLDS[tier];
      if (threshold && tokens > threshold) {
        obs.push({
          id: String(++obsId),
          severity: 'warning',
          message: `Token usage on '${phaseName}' (${tokens.toLocaleString()}) is unusually high for a ${tier}-tier phase (threshold: ${threshold.toLocaleString()}).`,
          timestamp,
        });
      }

      // Cost milestone alerts
      if (totalCost >= 1.0 && totalCost - cost < 1.0) {
        obs.push({
          id: String(++obsId),
          severity: 'info',
          message: `Pipeline cost has reached $${totalCost.toFixed(2)}.`,
          timestamp,
        });
      }
      if (totalCost >= 3.0 && totalCost - cost < 3.0) {
        obs.push({
          id: String(++obsId),
          severity: 'warning',
          message: `Pipeline cost has reached $${totalCost.toFixed(2)} — exceeding typical budget.`,
          timestamp,
        });
      }
    }

    // Terminal summary
    const statusEvent = events.find((e) => e.type === 'status_changed');
    if (statusEvent && statusEvent.type === 'status_changed') {
      const s = statusEvent;
      obs.push({
        id: String(++obsId),
        severity: s.status === 'success' ? 'info' : 'alert',
        message: `Pipeline ${s.status}. ${phaseCount} phases completed, ${failedPhases} failed. Total: ${totalTokens.toLocaleString()} tokens, $${totalCost.toFixed(4)}.`,
        timestamp: s.completed_at ?? new Date().toISOString(),
      });
    }

    return obs;
  }, [events]);

  return (
    <div className="relative">
      {/* Toggle button */}
      <button
        onClick={() => setExpanded(!expanded)}
        className={`flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
          expanded
            ? 'border-sky-500/30 bg-sky-500/10 text-sky-400'
            : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:text-zinc-200'
        }`}
      >
        <span>🔍</span>
        Observer
        {observations.length > 0 && (
          <span className="rounded-full bg-zinc-800 px-1.5 py-0.5 text-[10px]">
            {observations.length}
          </span>
        )}
      </button>

      {/* Panel */}
      {expanded && (
        <div className="mt-2 rounded-lg border border-zinc-800 bg-zinc-950 p-4">
          <h3 className="mb-3 text-sm font-medium text-zinc-300">
            Observer Feed
          </h3>

          {observations.length === 0 ? (
            <p className="text-xs text-zinc-600 italic">
              No observations yet. Monitoring pipeline events...
            </p>
          ) : (
            <div className="flex flex-col gap-2 max-h-80 overflow-y-auto">
              {observations.map((obs) => (
                <div
                  key={obs.id}
                  className="flex gap-2 rounded-md bg-zinc-900/50 px-3 py-2 text-xs"
                >
                  <span className="flex-shrink-0">{SEVERITY_ICON[obs.severity]}</span>
                  <div className="flex-1">
                    <p className={SEVERITY_COLOR[obs.severity]}>{obs.message}</p>
                    <p className="mt-0.5 text-[10px] text-zinc-600">
                      {new Date(obs.timestamp).toLocaleTimeString()}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
