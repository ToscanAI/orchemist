'use client';

/**
 * Fleet Dashboard — screen 1 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/01-fleet-dashboard.svg
 *
 * Layout (left → right, top → bottom):
 *   1. 4 KPI cards (active runs · gates · regressions · shipped 24h)
 *   2. In-flight runs table (8 cols × 12 grid) + Autonomy ramp (4 cols)
 *   3. Regression queue (6 cols) + Stale detection (6 cols)
 *
 * Data path (post-#888):
 *   - The page only renders when the engine is reachable —
 *     `EngineOfflineGuard` at the layout level short-circuits to an error
 *     UI otherwise. There is no demo-data fallback any more (issue #888).
 *   - On mount we fire six concurrent reads; any endpoint that 404s leaves
 *     its slot empty / 0 rather than rendering placeholder content.
 *
 * Cross-links to /admin (autonomy ramp), /gates (gate-needs-review KPI), /runs (in-flight rows).
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { listRuns, listGates, listRegressions, listStaleFindings } from '@/lib/api';
import type { RegressionRecord, StaleFindingsResponse } from '@/lib/api';
import type { RunRecord } from '@/lib/types';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { KPICard } from '@/components/harness/KPICard';
import { SectionCard } from '@/components/harness/SectionCard';
import { AutonomyRamp } from '@/components/harness/AutonomyRamp';
import { RunRow } from '@/components/harness/RunRow';
import { StatusDot } from '@/components/harness/StatusDot';

interface RunRowEntry {
  readonly run: RunRecord;
  readonly totalPhases: number;
  readonly confidence: number | undefined;
  readonly modelTier: string | undefined;
  readonly costUsd: number | undefined;
  readonly etaLabel: string | undefined;
}

export default function FleetDashboardPage() {
  const [liveRuns, setLiveRuns] = useState<readonly RunRecord[]>([]);
  const [totalRuns, setTotalRuns] = useState<number | null>(null);
  const [gateCount, setGateCount] = useState<number | null>(null);
  const [regressions, setRegressions] = useState<readonly RegressionRecord[] | null>(null);
  const [regressionsTotal, setRegressionsTotal] = useState<number | null>(null);
  // Round-2 audit fix: the previous code used `regressions.filter(r =>
  // r.status === 'fixing').length` on a list that was pre-filtered to
  // `status=detected`, which always returned 0. Track the fixing count via
  // a separate API call so the KPI sublabel reflects reality.
  const [fixingTotal, setFixingTotal] = useState<number | null>(null);
  const [staleFindings, setStaleFindings] = useState<StaleFindingsResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      listRuns({ status: 'running', limit: 20 }),
      listRuns({ limit: 1 }),
      listGates({ limit: 1 }),
      // Detected (queue) — KPI uses `total`, card uses items.
      listRegressions({ status: 'detected', limit: 10 }),
      // Fixing — separate call so the sublabel can honestly say "N auto-fix
      // in flight" instead of always-zero.
      listRegressions({ status: 'fixing', limit: 1 }),
      listStaleFindings(),
    ]).then(([runningRes, totalsRes, gatesRes, regRes, fixRes, staleRes]) => {
      if (cancelled) return;
      if (runningRes.status === 'fulfilled') setLiveRuns(runningRes.value.items);
      if (totalsRes.status === 'fulfilled') setTotalRuns(totalsRes.value.total);
      if (gatesRes.status === 'fulfilled') setGateCount(gatesRes.value.total);
      if (regRes.status === 'fulfilled') {
        setRegressions(regRes.value.items);
        setRegressionsTotal(regRes.value.total);
      }
      if (fixRes.status === 'fulfilled') setFixingTotal(fixRes.value.total);
      if (staleRes.status === 'fulfilled') setStaleFindings(staleRes.value);
    });
    return () => { cancelled = true; };
  }, []);

  const rows: readonly RunRowEntry[] = liveRuns.map((r) => ({
    run: r,
    totalPhases: 10,
    confidence: undefined,
    modelTier: undefined,
    costUsd: undefined,
    etaLabel: undefined,
  }));

  return (
    <HarnessShell
      title={`Hello René — fleet status as of ${new Date().toUTCString().slice(17, 22)} UTC`}
      screenIndex={1}
      breadcrumb={[{ label: 'Home', href: '/' }, { label: 'Fleet Dashboard' }]}
    >
      {/* Row 1: 4 KPI cards */}
      <section
        className="grid grid-cols-4 gap-4"
        aria-label="Fleet key indicators"
      >
        <KPICard
          label="Active runs"
          value={rows.length}
          tone="neutral"
          sublabel={
            <span className="text-harness-teal">
              {rows.filter((r) => (r.confidence ?? 1) >= 0.7).length} nominal · {rows.filter((r) => (r.confidence ?? 1) < 0.7).length} escalating
            </span>
          }
          testId="kpi-active-runs"
        />
        <KPICard
          label="Gates needing review"
          value={gateCount ?? 0}
          tone={(gateCount ?? 0) > 0 ? 'warning' : 'success'}
          sublabel={
            <span>
              {gateCount === null
                ? <>loading… → <Link href="/gates" className="h-link">Trust &amp; Gates ⌘4</Link></>
                : gateCount === 0
                ? <>all clear → <Link href="/gates" className="h-link">Trust &amp; Gates ⌘4</Link></>
                : <>{`${gateCount} pending`} → <Link href="/gates" className="h-link">Trust &amp; Gates ⌘4</Link></>}
            </span>
          }
          testId="kpi-gates"
        />
        <KPICard
          label="Regressions detected"
          value={regressionsTotal ?? 0}
          tone={(regressionsTotal ?? 0) > 0 ? 'danger' : 'success'}
          sublabel={
            regressionsTotal !== null
              ? regressionsTotal === 0
                ? <span>queue empty · no detected regressions</span>
                : <span>{fixingTotal ?? 0} auto-fix in flight · {regressionsTotal} detected</span>
              : <span>loading regression queue…</span>
          }
          testId="kpi-regressions"
        />
        <KPICard
          label="Shipped last 24h"
          value={totalRuns ?? 0}
          tone="success"
          sublabel={<span>auto-merged + human-reviewed totals</span>}
          testId="kpi-shipped"
        />
      </section>

      {/* Row 2: In-flight runs (col-span-8) + Autonomy ramp (col-span-4) */}
      <section className="mt-4 grid grid-cols-12 gap-4">
        <div className="col-span-8">
          <SectionCard
            title="In-flight pipeline runs"
            subtitle={
              <span>
                click any row → <Link href="/runs" className="h-link">Run Cockpit ⌘2</Link>
              </span>
            }
            action={
              <div className="flex items-center gap-2 text-[10px] tracking-widest text-harness-muted">
                <StatusDot tone="success" pulse />
                LIVE · SSE
              </div>
            }
            testId="section-inflight"
          >
            {rows.length === 0 ? (
              <div className="text-[12px] text-harness-muted py-6 text-center">
                No pipelines in flight. Launch one from the <Link href="/runs" className="h-link">Run Cockpit</Link>.
              </div>
            ) : (
              <div>
                <div className="grid grid-cols-12 gap-3 px-4 pb-2 border-b border-harness-border text-[10px] tracking-widest text-harness-dim">
                  <div className="col-span-3">REPO / TEMPLATE</div>
                  <div className="col-span-1">RUN</div>
                  <div className="col-span-3">PHASE</div>
                  <div className="col-span-1">MODEL</div>
                  <div className="col-span-1">COST</div>
                  <div className="col-span-1">ETA</div>
                  <div className="col-span-2 text-right">CONFIDENCE</div>
                </div>
                {rows.map((entry) => (
                  <RunRow
                    key={entry.run.run_id}
                    run={entry.run}
                    totalPhases={entry.totalPhases}
                    confidence={entry.confidence}
                    modelTier={entry.modelTier}
                    costUsd={entry.costUsd}
                    etaLabel={entry.etaLabel}
                  />
                ))}
              </div>
            )}
            <div className="mt-3 px-4 text-[10px] text-harness-dim">
              Showing {rows.length} of {rows.length} active runs · last 50 history → <Link href="/runs" className="h-link">/runs</Link>
            </div>
          </SectionCard>
        </div>
        <div className="col-span-4">
          <SectionCard
            title="Autonomy ramp"
            subtitle={
              <span>
                global · this org · adjust on <Link href="/admin" className="h-link">Admin ⌘5</Link>
              </span>
            }
            testId="section-autonomy"
          >
            <AutonomyRamp />
          </SectionCard>
        </div>
      </section>

      {/* Row 3: Regression queue + Stale detection */}
      <section className="mt-4 grid grid-cols-12 gap-4">
        <div className="col-span-6">
          <SectionCard
            title="Regression queue"
            subtitle={
              regressions !== null
                ? regressions.length > 0
                  ? <span className="text-harness-danger">{regressions.length} detected · live from /api/v1/regressions</span>
                  : <span className="text-harness-teal">all clear · 0 active regressions</span>
                : <span>loading regression queue…</span>
            }
            testId="section-regressions"
          >
            {regressions !== null ? (
              regressions.length === 0 ? (
                <div className="text-[12px] text-harness-muted py-3">No regressions detected in last 24h. <Link href="/runs" className="h-link">View all runs</Link></div>
              ) : (
                <ul className="flex flex-col gap-3">
                  {regressions.slice(0, 4).map((r) => (
                    <li key={r.id} className="rounded-md border border-harness-danger bg-harness-surface2 p-3">
                      <div className="flex items-center justify-between gap-3">
                        <div className="font-semibold text-[13px] text-harness-text">
                          {r.failure_type}
                        </div>
                        {r.fix_run_id ? (
                          <Link href={`/runs/${r.fix_run_id}`} className="h-link text-[11px]">
                            fix run ↗
                          </Link>
                        ) : (
                          <span className="text-harness-warning text-[11px]">no fix yet</span>
                        )}
                      </div>
                      <div className="mt-1 text-[11px] text-harness-muted">
                        {r.commit_sha.slice(0, 8)} · {
                          // Defensive: server-side `_normalize_row` coerces
                          // a malformed JSON list back to [], but the type
                          // signature still allows `readonly string[]` and a
                          // future change could let a non-array leak through.
                          Array.isArray(r.affected_files)
                            ? r.affected_files.slice(0, 2).join(', ') +
                              (r.affected_files.length > 2 ? ` +${r.affected_files.length - 2} more` : '')
                            : '—'
                        }
                        {' '}· status {r.status}
                      </div>
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <div className="text-[12px] text-harness-muted py-3">Loading regressions…</div>
            )}
            <div className="mt-4 text-[10px] text-harness-dim">
              closes ROADMAP §3.3 (regression.py) · UI surface for §3.4
            </div>
          </SectionCard>
        </div>
        <div className="col-span-6">
          <SectionCard
            title="Stale detection · proactive maintenance"
            subtitle={
              staleFindings !== null
                ? staleFindings.scan_status === 'no_scanner_yet'
                  ? <span className="text-harness-warning">ROADMAP §3.5 · scanner not yet implemented (live API placeholder)</span>
                  : <span>ROADMAP §3.5 · {staleFindings.items.length} findings · scan {staleFindings.scan_status}</span>
                : <span>ROADMAP §3.5 · loading…</span>
            }
            testId="section-stale"
          >
            {staleFindings !== null && staleFindings.scan_status === 'no_scanner_yet' ? (
              <div className="text-[12px] text-harness-muted py-3">
                Scanner not yet shipped. Endpoint returns an empty list — the harness card will populate automatically once the scanner lands. Tracked in <Link href="https://github.com/ToscanAI/orchemist/issues/817" className="h-link">#817</Link>.
              </div>
            ) : staleFindings !== null ? (
              <>
                <div className="h-section-label mb-2">FINDINGS ({staleFindings.items.length})</div>
                {staleFindings.items.length === 0 ? (
                  <div className="text-[12px] text-harness-muted py-3">No stale findings.</div>
                ) : (
                  <ul className="flex flex-col gap-3">
                    {staleFindings.items.map((s, i) => (
                      <li key={i} className="flex gap-3">
                        <StatusDot tone={s.severity === 'warn' ? 'warning' : 'neutral'} />
                        <div className="flex-1">
                          <div className="text-[13px] text-harness-text">{s.summary}</div>
                          <div className="mt-1 text-[11px] text-harness-muted">{s.hint}</div>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <div className="text-[12px] text-harness-muted py-3">Loading findings…</div>
            )}
            <div className="mt-4 text-[10px] text-harness-dim">
              Adversary review on every fix · cross-model via <Link href="/adversary" className="h-link">⌘3 Adversary Loop</Link>
            </div>
          </SectionCard>
        </div>
      </section>
    </HarnessShell>
  );
}
