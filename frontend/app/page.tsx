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
 * Data path:
 *   - Engine reachable → `listRuns({status: 'running'})` + `listRuns({limit: 1})` for totals
 *   - Engine offline → DEMO_ACTIVE_RUNS + DEMO_REGRESSIONS + DEMO_STALE
 *
 * Cross-links to /admin (autonomy ramp), /gates (gate-needs-review KPI), /runs (in-flight rows).
 */

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { listRuns } from '@/lib/api';
import type { RunRecord } from '@/lib/types';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { KPICard } from '@/components/harness/KPICard';
import { SectionCard } from '@/components/harness/SectionCard';
import { AutonomyRamp } from '@/components/harness/AutonomyRamp';
import { RunRow } from '@/components/harness/RunRow';
import { StatusDot } from '@/components/harness/StatusDot';
import {
  DEMO_ACTIVE_RUNS,
  DEMO_REGRESSIONS,
  DEMO_STALE,
} from '@/lib/demo-data';

export default function FleetDashboardPage() {
  const [liveRuns, setLiveRuns] = useState<readonly RunRecord[]>([]);
  const [engineUp, setEngineUp] = useState<boolean | null>(null);
  const [totalRuns, setTotalRuns] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    listRuns({ status: 'running', limit: 20 })
      .then((r) => {
        if (cancelled) return;
        setLiveRuns(r.items);
        setEngineUp(true);
        // separate query for totals
        listRuns({ limit: 1 }).then((all) => { if (!cancelled) setTotalRuns(all.total); }).catch(() => {});
      })
      .catch(() => {
        if (!cancelled) {
          setEngineUp(false);
        }
      });
    return () => { cancelled = true; };
  }, []);

  const showDemo = engineUp === false;
  const rows = showDemo
    ? DEMO_ACTIVE_RUNS
    : liveRuns.map((r) => ({ run: r, totalPhases: 10, confidence: undefined, modelTier: undefined, costUsd: undefined, etaLabel: undefined }));

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
          value={7}
          tone="warning"
          sublabel={
            <span>
              oldest 2h 14m → <Link href="/gates" className="h-link">Trust &amp; Gates ⌘4</Link>
            </span>
          }
          testId="kpi-gates"
        />
        <KPICard
          label="Regressions detected"
          value={DEMO_REGRESSIONS.length}
          tone="danger"
          sublabel={<span>auto-fix pipelines spawned · 1 PR open</span>}
          testId="kpi-regressions"
        />
        <KPICard
          label="Shipped last 24h"
          value={totalRuns ?? 14}
          tone="success"
          sublabel={<span>11 auto-merged · 3 human-reviewed</span>}
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
              {showDemo && (
                <span className="ml-3 h-pill h-pill-warning text-[9px]">demo data · engine offline</span>
              )}
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
            subtitle={<span className="text-harness-danger">{DEMO_REGRESSIONS.length} detected · auto-fix pipelines spawned</span>}
            testId="section-regressions"
          >
            <ul className="flex flex-col gap-3">
              {DEMO_REGRESSIONS.map((r, i) => (
                <li key={i} className="rounded-md border border-harness-danger bg-harness-surface2 p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-semibold text-[13px] text-harness-text">
                      {r.repo} · {r.branch}
                    </div>
                    {r.prUrl && (
                      <a
                        href={r.prUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="h-link text-[11px]"
                      >
                        PR ↗
                      </a>
                    )}
                    {r.retryStatus && (
                      <span className="text-harness-warning text-[11px]">{r.retryStatus}</span>
                    )}
                  </div>
                  <div className="mt-1 text-[11px] text-harness-muted">{r.summary}{r.sinceCommit && ` (${r.hoursAgo}h ago)`}</div>
                </li>
              ))}
            </ul>
            <div className="mt-4 text-[10px] text-harness-dim">
              closes ROADMAP §3.3 (regression.py) · UI surface for §3.4
            </div>
          </SectionCard>
        </div>
        <div className="col-span-6">
          <SectionCard
            title="Stale detection · proactive maintenance"
            subtitle={<span>ROADMAP §3.5 · scan cadence 24h · next 04:00 UTC</span>}
            testId="section-stale"
          >
            <div className="h-section-label mb-2">FINDINGS ({DEMO_STALE.length})</div>
            <ul className="flex flex-col gap-3">
              {DEMO_STALE.map((s, i) => (
                <li key={i} className="flex gap-3">
                  <StatusDot tone={s.severity === 'warn' ? 'warning' : 'neutral'} />
                  <div className="flex-1">
                    <div className="text-[13px] text-harness-text">{s.summary}</div>
                    <div className="mt-1 text-[11px] text-harness-muted">{s.hint}</div>
                  </div>
                </li>
              ))}
            </ul>
            <div className="mt-4 text-[10px] text-harness-dim">
              Adversary review on every fix · cross-model via <Link href="/adversary" className="h-link">⌘3 Adversary Loop</Link>
            </div>
          </SectionCard>
        </div>
      </section>
    </HarnessShell>
  );
}
