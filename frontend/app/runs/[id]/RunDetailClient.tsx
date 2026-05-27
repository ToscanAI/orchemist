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
 * Engine reachable → real `useRunEvents` SSE feed. Engine offline → demo phase
 * progression matching the SVG canon (active phase = implement).
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

// ── Phase rail metadata (#842) ──
// Hydrated from `GET /api/v1/phases` at component mount via `listPhases()`.
// FALLBACK_PHASES MUST match the canonical 12-phase pipeline AND the actual
// tier values — both `review` AND the adversary gate are at opus per
// VISION pillar 8 (issue #887). Drift here makes hydration visibly
// rerender — the phantom-phase problem this issue exists to fix.
const FALLBACK_PHASES: readonly PhaseDef[] = [
  { id: 'existing_symbols_inventory', label: '0 · existing_symbols_inventory', subtitle: 'sticky inventory · v4.2', tier: 'sonnet' },
  { id: 'spec',               label: '1a · spec',                tier: 'sonnet' },
  { id: 'behavioral',         label: '1b · behavioral',          tier: 'sonnet' },
  // ── Opus-tier phases per VISION pillar 8 (issue #887) ────────────────────────
  // The adversary gate (1c) and review gate (4) both run at the higher tier.
  // Tier values here mirror coding-pipeline-standard.yaml post-#887 bump.
  // See VISION.md §8. Do not revert without updating the frozen-state sentinel.
  { id: 'spec_adversary',     label: '1c · spec_adversary',      subtitle: 'OPUS · cross-model gate', tier: 'opus' },
  // ── Standard-model phases resume from 1d ─────────────────────────────────────
  // Only the two gates above use the higher tier; all other phases use the default.
  // (Phases 1d, 2, 3, 3b, 4b, 4c, 5 are at the engine's standard model tier.)
  { id: 'postmortem_spec',    label: '1d · postmortem_spec',     subtitle: 'exhaustion analysis', tier: 'sonnet' },
  { id: 'acceptance_test',    label: '2 · acceptance_test',      tier: 'sonnet' },
  { id: 'implement',          label: '3 · implement',            tier: 'sonnet' },
  { id: 'acceptance_run',     label: '3b · acceptance_run',      subtitle: 'engine · no LLM', tier: 'engine' },
  { id: 'review',             label: '4 · review',               subtitle: 'OPUS', tier: 'opus' },
  { id: 'fix',                label: '4b · fix',                 tier: 'sonnet' },
  { id: 'postmortem_review',  label: '4c · postmortem_review',   subtitle: 'exhaustion analysis', tier: 'sonnet' },
  { id: 'test',               label: '5 · test',                 subtitle: 'engine · no LLM', tier: 'engine' },
];

const DEMO_COMPLETED = ['existing_symbols_inventory', 'spec', 'behavioral', 'spec_adversary', 'acceptance_test', 'acceptance_test_adversary'];
const DEMO_ACTIVE = 'implement';

function phaseStatus(phaseId: string, completed: readonly string[], current: string | null, runStatus: string): 'done' | 'active' | 'queued' | 'failed' {
  if (completed.includes(phaseId)) return 'done';
  if (current === phaseId) {
    if (runStatus === 'failed' || runStatus === 'budget_exceeded') return 'failed';
    return 'active';
  }
  return 'queued';
}

// ── Resolve run id ──
// Production (`output: 'export'`): the engine's FastAPI catch-all serves
// `out/runs/_/index.html` for any unknown `/runs/<id>` path. The browser's
// `window.location.pathname` still carries the real id, and `useParams`
// returns the URL segment intact.
//
// Development (`next dev`): the dev server is strict about generateStaticParams
// and would 500 on any id not in the placeholder set. Our `next.config.js`
// rewrite sends `/runs/:id` → `/runs/_?run=:id`, so the real id arrives via
// useSearchParams instead. This helper picks the right one.
function resolveRunId(pathParamId: string | undefined, searchRun: string | null): string {
  if (pathParamId && pathParamId !== '_') return pathParamId;
  if (searchRun) return searchRun;
  if (typeof window !== 'undefined') {
    const m = window.location.pathname.match(/\/runs\/([^/]+)/);
    if (m && m[1] !== '_') return m[1];
  }
  return '';
}

// ── Page ──
function RunCockpitInner() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();

  // Compute runId in a way that produces the SAME value on SSR and the first
  // client render (both: ignore window). After mount, useEffect populates the
  // real id from window.location if needed. Without this two-step approach we
  // hit a React hydration mismatch on `/runs/<id>` paths: server renders the
  // placeholder `_`, client renders the real id, React complains.
  const [runId, setRunId] = useState<string>(() => {
    if (id && id !== '_') return id;
    if (search?.get('run')) return search.get('run')!;
    return '';
  });
  useEffect(() => {
    const resolved = resolveRunId(id, search?.get('run') ?? null);
    if (resolved && resolved !== runId) setRunId(resolved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, search]);

  const [run, setRun] = useState<RunRecord | null>(null);
  const [engineUp, setEngineUp] = useState<boolean | null>(null);
  const [artifacts, setArtifacts] = useState<readonly RunArtifactListEntry[] | null>(null);
  const [phase0, setPhase0] = useState<RunPhase0 | null>(null);
  // Phase rail metadata — hydrated from `GET /api/v1/phases` at mount.
  // Falls back to FALLBACK_PHASES until the live response arrives (or
  // when engine is offline). Replaces the hardcoded PHASES array (#842).
  const [phases, setPhases] = useState<readonly PhaseDef[]>(FALLBACK_PHASES);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    // Fetch all four concurrently — getRun is the only required call;
    // artifacts and phase0 may legitimately 404 (run not started, or
    // skip-spec pipeline with no Phase 0); listPhases failures fall back
    // to FALLBACK_PHASES.
    Promise.allSettled([
      getRun(runId),
      listRunArtifacts(runId),
      getRunPhase0(runId),
      listPhases(),
    ]).then(([runRes, artRes, phase0Res, phasesRes]) => {
      if (cancelled) return;
      if (runRes.status === 'fulfilled') {
        setRun(runRes.value);
        setEngineUp(true);
      } else {
        // engineUp stays false → demo path renders
        setEngineUp(false);
      }
      if (artRes.status === 'fulfilled') setArtifacts(artRes.value.files);
      if (phase0Res.status === 'fulfilled') setPhase0(phase0Res.value);
      if (phasesRes.status === 'fulfilled') {
        setPhases(derivePhaseDefs(phasesRes.value.phases));
      }
    });
    return () => { cancelled = true; };
  }, [runId]);

  const { events } = useRunEvents(runId, engineUp === true);

  // Derive completed phases + current from events when available; demo fallback otherwise
  const completed: readonly string[] = engineUp === true && run
    ? run.completed_phases
    : DEMO_COMPLETED;
  const currentPhase: string | null = engineUp === true && run
    ? run.current_phase
    : DEMO_ACTIVE;
  const status: string = engineUp === true && run
    ? run.status
    : 'running';

  const completedCount = completed.length;
  const progressPct = Math.round((completedCount / phases.length) * 100);

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
      <div className="grid grid-cols-12 gap-4">
        {/* Phase rail */}
        <div className="col-span-4">
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
        <div className="col-span-5 flex flex-col gap-4">
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
              <span className="h-pill h-pill-success">CONSUME · {phase0 ? phase0.verdicts.CONSUME : 3}</span>
              <span className="h-pill h-pill-purple">EXTEND · {phase0 ? phase0.verdicts.EXTEND : 1}</span>
              <span className="h-pill h-pill-warning" style={{ background: '#3B2E1F' }}>DIVERGENT · {phase0 ? phase0.verdicts.DIVERGENT : 0}</span>
              <span className="h-pill" style={{ borderColor: '#5A6371', color: '#8A93A2' }}>NEW-OK · {phase0 ? phase0.verdicts.NEW_OK : 2}</span>
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
              {engineUp === true && phaseCompletedEvents.length > 0 ? (
                phaseCompletedEvents.map((ev, i) => (
                  <div key={i}>
                    <span className="text-harness-dim">[{ev.elapsed_seconds?.toFixed(0) ?? '—'}s]</span>{' '}
                    <span className="text-harness-text">{ev.phase_name ?? ev.phase_id} completed</span>{' '}
                    <span className="text-harness-teal">→ ${(ev.cost_usd ?? 0).toFixed(2)} · {ev.tokens_in ?? 0} in / {ev.tokens_out ?? 0} out</span>
                  </div>
                ))
              ) : (
                <>
                  <div><span className="text-harness-dim">[14m 12s]</span> read_file src/orchestration_engine/verdict_parser.py</div>
                  <div><span className="text-harness-dim">[14m 14s]</span> <span className="text-harness-teal">→ 224 lines · CONSUME verdict</span></div>
                  <div><span className="text-harness-dim">[14m 18s]</span> grep -n &quot;extract_verdict&quot; src/</div>
                  <div><span className="text-harness-dim">[14m 19s]</span> <span className="text-harness-warning">→ 2 hits · review_parser.py:88 (dup risk #687)</span></div>
                  <div><span className="text-harness-dim">[14m 24s]</span> edit_file src/.../review_parser.py · delete extract_verdict</div>
                  <div><span className="text-harness-dim">[14m 25s]</span> <span className="text-harness-teal">→ 36 lines removed</span></div>
                  <div><span className="text-harness-dim">[14m 28s]</span> edit_file src/.../review_parser.py · add re-export</div>
                  <div><span className="text-harness-dim">[14m 30s]</span> <span className="text-harness-teal">→ 3 lines added</span></div>
                  <div><span className="text-harness-dim">[14m 34s]</span> bash python3 -m pytest tests/test_verdict_parser.py -q</div>
                  <div><span className="text-harness-dim">[14m 38s]</span> <span className="text-harness-purple">→ 23 passed · running…</span></div>
                  <div className="mt-2 flex items-center gap-2">
                    <StatusDot tone="info" pulse size={6} />
                    <span className="text-harness-purple">awaiting next tool call</span>
                  </div>
                </>
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
                : <span>41 symbols across 4 sections · drives sub-check 7d at every phase <span className="ml-2 text-harness-warning">(demo — no Phase 0 artifact)</span></span>
            }
            testId="section-phase0"
          >
            <div className="grid grid-cols-2 gap-3 text-[11px]">
              <div>
                <div className="h-section-label">UI PRIMITIVES ({phase0 ? phase0.sections.ui_primitives.count : 18})</div>
                <div className="mt-1 text-harness-text truncate" title={phase0?.sections.ui_primitives.entries.join(' · ')}>
                  {phase0 && phase0.sections.ui_primitives.entries.length > 0
                    ? phase0.sections.ui_primitives.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.ui_primitives.entries.length > 4 ? ' · …' : '')
                    : 'Badge · Button · Spinner · …'}
                </div>
              </div>
              <div>
                <div className="h-section-label">SHARED LIBS ({phase0 ? phase0.sections.shared_libs.count : 4})</div>
                <div className="mt-1 text-harness-text font-mono text-[10px] truncate" title={phase0?.sections.shared_libs.entries.join(' · ')}>
                  {phase0 && phase0.sections.shared_libs.entries.length > 0
                    ? phase0.sections.shared_libs.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.shared_libs.entries.length > 4 ? ' · …' : '')
                    : 'verdict_parser · cost_tracker · file_guard · git_integration'}
                </div>
              </div>
              <div>
                <div className="h-section-label">ADJACENT PATTERNS ({phase0 ? phase0.sections.adjacent_patterns.count : 7})</div>
                <div className="mt-1 text-harness-text truncate" title={phase0?.sections.adjacent_patterns.entries.join(' · ')}>
                  {phase0 && phase0.sections.adjacent_patterns.entries.length > 0
                    ? phase0.sections.adjacent_patterns.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.adjacent_patterns.entries.length > 4 ? ' · …' : '')
                    : 'phase_dispatch · subagent_invocation · sse_emit · …'}
                </div>
              </div>
              <div>
                <div className="h-section-label">WORKSPACE BARRELS ({phase0 ? phase0.sections.workspace_barrels.count : 12})</div>
                <div className="mt-1 text-harness-text font-mono text-[10px] truncate" title={phase0?.sections.workspace_barrels.entries.join(' · ')}>
                  {phase0 && phase0.sections.workspace_barrels.entries.length > 0
                    ? phase0.sections.workspace_barrels.entries.slice(0, 4).map((e) => e.split(' ← ')[0]).join(' · ') + (phase0.sections.workspace_barrels.entries.length > 4 ? ' · …' : '')
                    : 'src/orchestration_engine/__init__.py exports …'}
                </div>
              </div>
            </div>
            <div className="mt-3 text-[10px] text-harness-dim">
              → duplicates-audit referenced: <Link href="/admin#duplicates" className="h-link">DUPLICATES.md group 1 (verdict)</Link>
            </div>
          </SectionCard>
        </div>

        {/* Right: artifacts + cost/conf + drift */}
        <div className="col-span-3 flex flex-col gap-4">
          <SectionCard title="Run artifacts" subtitle={<>.orchemist/runs/{runId.slice(0, 8) || '_'}/</>}>
            <ul className="space-y-1.5 text-[11px]">
              {(artifacts && artifacts.length > 0
                ? artifacts.map((f) => ({ name: f.name, kB: f.size_bytes / 1024, isReal: true }))
                : ['existing_symbols.md', 'spec.md', 'behavioral.md', 'spec_adversary.md', 'acceptance_tests.py', 'implement.md', 'review.md'].map((name, i) => ({
                    name,
                    kB: 2 + i * 0.4,
                    isReal: false,
                  }))
              ).map((f, i) => {
                const isDone = f.isReal || i < completedCount;
                const isActive = !f.isReal && i === completedCount;
                return (
                  <li key={f.name} className="flex items-center justify-between">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className={isDone ? 'text-harness-teal' : isActive ? 'text-harness-purple' : 'text-harness-dim'}>
                        {isDone ? '✓' : isActive ? '●' : '○'}
                      </span>
                      <Link
                        href={`/runs/${runId}/artifacts/${encodeURIComponent(f.name)}`}
                        className={[
                          'truncate',
                          isDone ? 'text-harness-text underline decoration-harness-dim' : isActive ? 'text-harness-text' : 'text-harness-muted',
                        ].join(' ')}
                        title={f.name}
                      >
                        {f.name}
                      </Link>
                    </div>
                    <span className="text-harness-dim text-[10px] ml-2 shrink-0">
                      {f.isReal ? `${f.kB.toFixed(1)} kB` : isDone ? `${f.kB.toFixed(1)} kB` : isActive ? 'writing…' : 'queued'}
                    </span>
                  </li>
                );
              })}
            </ul>
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

      {engineUp === false && (
        <div className="fixed bottom-12 right-6 z-40 h-pill h-pill-warning text-[10px]">demo data · engine offline</div>
      )}
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
