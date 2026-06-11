'use client';

/**
 * Run Cockpit — screen 2 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/02-run-cockpit.svg
 *
 * Two-column layout:
 *   - Left  (col-span-4): vertical Phase rail with progress bar + 10 phase cards
 *                          (Phase 0 inventory included; live indicator on active)
 *   - Right (col-span-8): active-phase detail panel + live SSE tool-call stream +
 *                          artifacts list + cost/confidence + Jaccard drift
 *
 * Post-#888 (harness graduation): the page assumes a reachable engine.
 * `EngineOfflineGuard` at the layout level renders the offline error UI when
 * `/api/v1/health` rejects, so this component never has to handle the
 * engine-offline case. Phase rail metadata still hydrates from
 * `GET /api/v1/phases`; before that resolves the rail renders an empty
 * skeleton.
 */

import { useState, useEffect, useMemo, Suspense } from 'react';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { useRunEvents } from '@/lib/sse';
import {
  getRun,
  resumeRun,
  cancelRun,
  ApiError,
  listRunArtifacts,
  getRunPhase0,
  listPhases,
  type RunArtifactListEntry,
  type RunPhase0,
} from '@/lib/api';
import type { RunRecord, SsePhaseCompletedEvent } from '@/lib/types';
import { derivePhaseDefs, type PhaseDef } from '@/lib/phaseLabels';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';
import { StatusDot } from '@/components/harness/StatusDot';
import { useStaticExportParam } from '@/hooks/useStaticExportParam';

// Phase rail metadata hydrates from `GET /api/v1/phases` at component mount
// via `listPhases()`. Per #888 there is no demo `FALLBACK_PHASES` fallback —
// the EngineOfflineGuard at the layout level takes over when the engine is
// unreachable, and on a reachable-engine 404 the rail renders empty until
// the phases endpoint succeeds.

/**
 * Run statuses that indicate the pipeline has reached a terminal state.
 * Module-scope (issue #774) so the array identity is stable across renders.
 */
const TERMINAL_STATUSES = [
  'success',
  'failed',
  'cancelled',
  'budget_exceeded',
  'scoring_failed',
] as const;

/**
 * Run statuses where the current phase is considered "failed" rather than
 * "active" in the phase rail rendering.
 */
const FAILED_RUN_STATUSES: ReadonlySet<string> = new Set(['failed', 'budget_exceeded', 'scoring_failed']);

function phaseStatus(phaseId: string, completed: readonly string[], current: string | null, runStatus: string): 'done' | 'active' | 'queued' | 'failed' {
  if (completed.includes(phaseId)) return 'done';
  if (current === phaseId) {
    if (FAILED_RUN_STATUSES.has(runStatus)) return 'failed';
    return 'active';
  }
  return 'queued';
}

// ── Resolve run id ──
// Production (`output: 'export'`): the engine's FastAPI catch-all serves
// `out/runs/_/index.html` for any unknown `/runs/<id>` path. The browser's
// `window.location.pathname` still carries the real id, and `useParams`
// returns the URL segment intact. Shared `useStaticExportParam` hook (#774)
// centralises that resolution.
//
// Development (`next dev`): the dev server is strict about generateStaticParams
// and would 500 on any id not in the placeholder set. Our `next.config.js`
// rewrite sends `/runs/:id` → `/runs/_?run=:id`, so the real id arrives via
// useSearchParams instead. The dev-mode query-param fallback lives here.

// ── Page ──
function RunCockpitInner() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();

  // First try the route param (resolves placeholder via the shared hook),
  // then fall back to the dev-mode `?run=...` search-param.
  const resolvedFromPath = useStaticExportParam(id);
  const searchRun = search?.get('run') ?? null;

  // Compute runId in a way that produces the SAME value on SSR and the first
  // client render (both: ignore window). After mount, useEffect populates the
  // real id from useStaticExportParam if needed. Without this two-step approach
  // we hit a React hydration mismatch on `/runs/<id>` paths: server renders the
  // placeholder `_`, client renders the real id, React complains.
  const [runId, setRunId] = useState<string>(() => {
    if (id && id !== '_') return id;
    if (searchRun) return searchRun;
    return '';
  });
  useEffect(() => {
    const candidate =
      resolvedFromPath && resolvedFromPath !== '_'
        ? resolvedFromPath
        : (searchRun ?? '');
    if (candidate && candidate !== runId) setRunId(candidate);
  }, [resolvedFromPath, searchRun, runId]);

  const [run, setRun] = useState<RunRecord | null>(null);
  const [runFetched, setRunFetched] = useState<boolean>(false);
  const [artifacts, setArtifacts] = useState<readonly RunArtifactListEntry[] | null>(null);
  const [phase0, setPhase0] = useState<RunPhase0 | null>(null);
  // Phase rail metadata — hydrated from `GET /api/v1/phases` at mount.
  // Renders empty until the live response arrives. Per #888 no demo
  // FALLBACK_PHASES — the EngineOfflineGuard at the layout level takes
  // over when the engine is unreachable.
  const [phases, setPhases] = useState<readonly PhaseDef[]>([]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    // Fetch all four concurrently — getRun is the only required call;
    // artifacts and phase0 may legitimately 404 (run not started, or
    // skip-spec pipeline with no Phase 0); listPhases failures leave the
    // phase rail empty.
    Promise.allSettled([
      getRun(runId),
      listRunArtifacts(runId),
      getRunPhase0(runId),
      listPhases(),
    ]).then(([runRes, artRes, phase0Res, phasesRes]) => {
      if (cancelled) return;
      if (runRes.status === 'fulfilled') {
        setRun(runRes.value);
      }
      setRunFetched(true);
      if (artRes.status === 'fulfilled') setArtifacts(artRes.value.files);
      if (phase0Res.status === 'fulfilled') setPhase0(phase0Res.value);
      if (phasesRes.status === 'fulfilled') {
        setPhases(derivePhaseDefs(phasesRes.value.phases));
      }
    });
    return () => { cancelled = true; };
  }, [runId]);

  // SSE is enabled only when we have a real run record from the engine AND
  // the run has not reached a terminal state. Consuming the module-level
  // `TERMINAL_STATUSES` keeps the array reference stable across renders (#774).
  const isTerminal = run !== null && (TERMINAL_STATUSES as readonly string[]).includes(run.status);
  const { events } = useRunEvents(runId, run !== null && !isTerminal);

  // Derive completed phases + current from the real run record (no demo fallback).
  const completed: readonly string[] = run ? run.completed_phases : [];
  const currentPhase: string | null = run ? run.current_phase : null;
  const status: string = run ? run.status : 'pending';

  const completedCount = completed.length;
  const progressPct = phases.length > 0
    ? Math.round((completedCount / phases.length) * 100)
    : 0;

  const totalCostUsd = events.reduce((acc, ev) => {
    if (ev.type === 'phase_completed' && ev.cost_usd !== null) return acc + (ev.cost_usd ?? 0);
    return acc;
  }, 0);

  const phaseCompletedEvents = useMemo<readonly SsePhaseCompletedEvent[]>(
    () => events.filter((e): e is SsePhaseCompletedEvent => e.type === 'phase_completed'),
    [events],
  );

  return (
    <HarnessShell
      title="Pipeline run · coding-pipeline-standard v4.2"
      screenIndex={2}
      breadcrumb={[
        { label: 'Fleet', href: '/' },
        { label: 'orchemist' },
        { label: 'runs', href: '/runs' },
        { label: runId.slice(0, 8) || '_' },
      ]}
      actions={
        <>
          <button type="button" className="h-pill h-pill-success">
            <StatusDot tone="success" pulse />
            LIVE · SSE
          </button>
          <button type="button" className="h-button">Pause</button>
          <button type="button" className="h-button h-button-primary" style={{ borderColor: '#F59E0B', color: '#F59E0B' }}>Escalate</button>
          <button
            type="button"
            className="h-button h-button-danger"
            onClick={async () => {
              if (!runId) return;
              try { await cancelRun(runId); } catch {}
            }}
          >
            Cancel
          </button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        {/* Phase rail */}
        <div className="lg:col-span-4">
          <div className="h-card p-5">
            <h2 className="text-[14px] font-bold text-harness-text">Phases · {completedCount} of {phases.length} done</h2>
            <div className="mt-1 text-[11px] text-harness-muted">
              runtime — · spend ${totalCostUsd.toFixed(2)} / budget $8.00
            </div>
            <div className="mt-4 h-1.5 w-full rounded bg-harness-border">
              <div
                className="h-1.5 rounded"
                style={{ width: `${progressPct}%`, background: 'linear-gradient(90deg, #7C5CFC 0%, #2DD4BF 100%)' }}
              />
            </div>

            <ul className="mt-6 relative">
              <div className="absolute left-[15px] top-2 bottom-2 w-[2px] bg-harness-border" />
              {phases.map((phase) => {
                const st = phaseStatus(phase.id, completed, currentPhase, status);
                return (
                  <li
                    key={phase.id}
                    data-testid={`phase-${phase.id}`}
                    className="relative pl-12 pb-5"
                  >
                    <span
                      className={[
                        'absolute left-[6px] top-0 inline-flex h-5 w-5 items-center justify-center rounded-full text-[9px] font-bold',
                        st === 'done'  ? 'bg-harness-teal text-[#0B0D10]' :
                        st === 'active' ? 'bg-harness-purple text-white animate-pulse-soft ring-2 ring-[#0B0D10]' :
                        st === 'failed' ? 'bg-harness-danger text-[#0B0D10]' :
                        'border-2 border-harness-dim text-harness-dim',
                      ].join(' ')}
                    >
                      {st === 'done' ? '✓' : st === 'failed' ? '✗' : phase.id.slice(0, 1).toUpperCase()}
                    </span>
                    <div className={[
                      'text-[13px] leading-tight font-semibold',
                      st === 'queued' ? 'text-harness-muted' : 'text-harness-text',
                    ].join(' ')}>
                      {phase.label}
                    </div>
                    {phase.subtitle && (
                      <div className="mt-0.5 text-[10px] text-harness-dim">{phase.subtitle}</div>
                    )}
                  </li>
                );
              })}
            </ul>

            <div className="mt-2 text-[10px] text-harness-dim">→ Phase 0 inventory feeds every downstream phase (v4.2)</div>
          </div>
        </div>

        {/* Detail panel */}
        <div className="lg:col-span-5 flex flex-col gap-4">
          <div className="h-card h-card-purple p-5">
            <h3 className="text-[16px] font-bold text-harness-text">
              {currentPhase ?? '—'} — sub-check 7d enforced from Phase 0
            </h3>
            <div className="mt-2 text-[11px] text-harness-muted">
              model · claude-sonnet-4-6 · tier sonnet · thinking high
            </div>
            <div className="text-[11px] text-harness-muted">
              subagent · orchemist-implementer · fresh context
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <span className="h-pill h-pill-success">CONSUME · {phase0 ? phase0.verdicts.CONSUME : '—'}</span>
              <span className="h-pill h-pill-purple">EXTEND · {phase0 ? phase0.verdicts.EXTEND : '—'}</span>
              <span className="h-pill h-pill-warning" style={{ background: '#3B2E1F' }}>DIVERGENT · {phase0 ? phase0.verdicts.DIVERGENT : '—'}</span>
              <span className="h-pill" style={{ borderColor: '#5A6371', color: '#8A93A2' }}>NEW-OK · {phase0 ? phase0.verdicts.NEW_OK : '—'}</span>
            </div>
            <div className="mt-4 flex flex-wrap gap-4 text-[11px]">
              <Link href={`/runs/${runId}/artifacts/spec.md`} className="h-link">view spec.md</Link>
              <Link href={`/runs/${runId}/artifacts/behavioral.md`} className="h-link">behavioral.md</Link>
              <Link href={`/runs/${runId}/artifacts/acceptance_tests`} className="h-link">acceptance_tests</Link>
            </div>
          </div>

          <SectionCard
            title="Live tool-call stream"
            subtitle={<span>SSE · 23 calls / ~100 est · MAX_TOOL_ITERATIONS=100 · language-agnostic exec (#794)</span>}
            testId="section-tool-stream"
          >
            <div className="font-mono text-[11px] text-harness-muted leading-relaxed h-scroll overflow-y-auto max-h-72">
              {phaseCompletedEvents.length > 0 ? (
                phaseCompletedEvents.map((ev, i) => (
                  <div key={i}>
                    <span className="text-harness-dim">[{ev.elapsed_seconds?.toFixed(0) ?? '—'}s]</span>{' '}
                    <span className="text-harness-text">{ev.phase_name ?? ev.phase_id} completed</span>{' '}
                    <span className="text-harness-teal">→ ${(ev.cost_usd ?? 0).toFixed(2)} · {ev.tokens_in ?? 0} in / {ev.tokens_out ?? 0} out</span>
                  </div>
                ))
              ) : (
                <div className="flex items-center gap-2 py-2">
                  <StatusDot tone="info" pulse size={6} />
                  <span className="text-harness-muted">
                    {run ? 'awaiting next tool call' : runFetched ? 'no run data — check the run id' : 'loading run events…'}
                  </span>
                </div>
              )}
            </div>
          </SectionCard>

          <SectionCard
            title="Phase 0 inventory · this run"
            subtitle={
              phase0
                ? <span>{
                    phase0.sections.ui_primitives.count + phase0.sections.shared_libs.count +
                    phase0.sections.adjacent_patterns.count + phase0.sections.workspace_barrels.count
                  } symbols across 4 sections · drives sub-check 7d at every phase</span>
                : <span>no Phase 0 artifact for this run</span>
            }
            testId="section-phase0"
          >
            <div className="grid grid-cols-2 gap-3 text-[11px]">
              <div>
                <div className="h-section-label">UI PRIMITIVES ({phase0 ? phase0.sections.ui_primitives.count : '—'})</div>
                <div className="mt-1 text-harness-text truncate" title={phase0?.sections.ui_primitives.entries.join(' · ')}>
                  {phase0 && phase0.sections.ui_primitives.entries.length > 0
                    ? phase0.sections.ui_primitives.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.ui_primitives.entries.length > 4 ? ' · …' : '')
                    : '—'}
                </div>
              </div>
              <div>
                <div className="h-section-label">SHARED LIBS ({phase0 ? phase0.sections.shared_libs.count : '—'})</div>
                <div className="mt-1 text-harness-text font-mono text-[10px] truncate" title={phase0?.sections.shared_libs.entries.join(' · ')}>
                  {phase0 && phase0.sections.shared_libs.entries.length > 0
                    ? phase0.sections.shared_libs.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.shared_libs.entries.length > 4 ? ' · …' : '')
                    : '—'}
                </div>
              </div>
              <div>
                <div className="h-section-label">ADJACENT PATTERNS ({phase0 ? phase0.sections.adjacent_patterns.count : '—'})</div>
                <div className="mt-1 text-harness-text truncate" title={phase0?.sections.adjacent_patterns.entries.join(' · ')}>
                  {phase0 && phase0.sections.adjacent_patterns.entries.length > 0
                    ? phase0.sections.adjacent_patterns.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.adjacent_patterns.entries.length > 4 ? ' · …' : '')
                    : '—'}
                </div>
              </div>
              <div>
                <div className="h-section-label">WORKSPACE BARRELS ({phase0 ? phase0.sections.workspace_barrels.count : '—'})</div>
                <div className="mt-1 text-harness-text font-mono text-[10px] truncate" title={phase0?.sections.workspace_barrels.entries.join(' · ')}>
                  {phase0 && phase0.sections.workspace_barrels.entries.length > 0
                    ? phase0.sections.workspace_barrels.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.workspace_barrels.entries.length > 4 ? ' · …' : '')
                    : '—'}
                </div>
              </div>
            </div>
            <div className="mt-3 text-[10px] text-harness-dim">
              → duplicates-audit referenced: <Link href="/admin#duplicates" className="h-link">DUPLICATES.md group 1 (verdict)</Link>
            </div>
          </SectionCard>
        </div>

        {/* Right: artifacts + cost/conf + drift */}
        <div className="lg:col-span-3 flex flex-col gap-4">
          <SectionCard title="Run artifacts" subtitle={<>.orchemist/runs/{runId.slice(0, 8) || '_'}/</>}>
            {artifacts === null ? (
              <div className="text-[11px] text-harness-muted">loading artifacts…</div>
            ) : artifacts.length === 0 ? (
              <div className="text-[11px] text-harness-muted">no artifacts yet</div>
            ) : (
              <ul className="space-y-1.5 text-[11px]">
                {artifacts.map((f) => {
                  const kB = f.size_bytes / 1024;
                  return (
                    <li key={f.name} className="flex items-center justify-between">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-harness-teal">✓</span>
                        <Link
                          href={`/runs/${runId}/artifacts/${encodeURIComponent(f.name)}`}
                          className="truncate text-harness-text underline decoration-harness-dim"
                          title={f.name}
                        >
                          {f.name}
                        </Link>
                      </div>
                      <span className="text-harness-dim text-[10px] ml-2 shrink-0">
                        {kB.toFixed(1)} kB
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </SectionCard>

          <SectionCard title="Cost & confidence">
            <div className="h-section-label">CONFIDENCE TREND</div>
            <svg viewBox="0 0 280 80" className="mt-2 w-full">
              <polyline points="0,68 35,58 70,42 105,40 140,36 175,30 210,22 245,16 270,12" fill="none" stroke="#2DD4BF" strokeWidth="2"/>
              <circle cx="270" cy="12" r="4" fill="#2DD4BF"/>
            </svg>
            <div className="text-right text-[14px] font-bold text-harness-teal">0.91</div>
            <div className="mt-3 text-[11px] text-harness-muted">${totalCostUsd.toFixed(2)} / $8.00 budget</div>
            <div className="mt-1 h-1.5 w-full rounded bg-harness-border">
              <div
                className="h-1.5 rounded"
                style={{ width: `${Math.min(100, (totalCostUsd / 8) * 100)}%`, background: 'linear-gradient(90deg, #7C5CFC 0%, #2DD4BF 100%)' }}
              />
            </div>
          </SectionCard>

          <SectionCard title="Jaccard drift (last 5 turns)" subtitle={<span>threshold 0.95 · 2 consecutive → converged</span>}>
            <ul className="space-y-2 text-[11px]">
              <li className="flex items-center gap-3">
                <span className="w-16 text-harness-text">R1 ↔ R2</span>
                <span className="flex-1 h-3 rounded bg-harness-border overflow-hidden">
                  <span className="block h-3 bg-harness-warning" style={{ width: '62%' }} />
                </span>
                <span className="w-10 text-right text-harness-warning font-medium">0.62</span>
              </li>
              <li className="flex items-center gap-3">
                <span className="w-16 text-harness-text">R2 ↔ R3</span>
                <span className="flex-1 h-3 rounded bg-harness-border overflow-hidden">
                  <span className="block h-3 bg-harness-teal" style={{ width: '93%' }} />
                </span>
                <span className="w-10 text-right text-harness-teal font-medium">0.93</span>
              </li>
            </ul>
          </SectionCard>
        </div>
      </div>

    </HarnessShell>
  );
}

// `useSearchParams` requires a Suspense boundary under Next 14 static export.
export default function RunCockpitClient() {
  return (
    <Suspense fallback={null}>
      <RunCockpitInner />
    </Suspense>
  );
}
