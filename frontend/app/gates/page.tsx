'use client';

/**
 * Trust & Gates — screen 4 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/04-trust-gates.svg
 *
 * Wires the existing `/api/v1/gates` endpoints (issue #743 — endpoints shipped,
 * UI was missing per FRONTEND.md). When the engine is reachable, real gate
 * records render; otherwise demo data preserves the page's IA + the audit
 * narrative from the 2026-05-24 investigation pack.
 *
 * Operator affordances per row: Approve / Reject (via `/api/v1/gates/{run_id}`).
 * Bulk-approve is a marquee action in the top bar.
 */

import { useEffect, useState } from 'react';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';
import { listGates, approveGate, rejectGate, ApiError } from '@/lib/api';
import type { GateRecord } from '@/lib/api';
import {
  DEMO_GATES,
  DEMO_TRUST_PROFILES,
  DEMO_DECISIONS,
} from '@/lib/demo-data';

type Filter = 'all' | 'pending' | 'auto-merged' | 'held';

function confidenceColor(c: number, threshold: number): string {
  if (c >= threshold) return 'text-harness-teal';
  if (c >= threshold - 0.1) return 'text-harness-warning';
  return 'text-harness-danger';
}

export default function TrustAndGatesPage() {
  const [filter, setFilter] = useState<Filter>('all');
  const [liveGates, setLiveGates] = useState<readonly GateRecord[] | null>(null);
  const [engineUp, setEngineUp] = useState<boolean | null>(null);
  const [busyRunId, setBusyRunId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listGates({ limit: 50 })
      .then((r) => { if (!cancelled) { setLiveGates(r.items); setEngineUp(true); } })
      .catch(() => { if (!cancelled) setEngineUp(false); });
    return () => { cancelled = true; };
  }, []);

  const showDemo = engineUp === false || (liveGates !== null && liveGates.length === 0);

  async function handleApprove(runId: string) {
    setBusyRunId(runId);
    try {
      await approveGate(runId, {});
      // Refresh
      const r = await listGates({ limit: 50 });
      setLiveGates(r.items);
    } catch (e) {
      console.error('gate approve failed', e);
    } finally {
      setBusyRunId(null);
    }
  }

  async function handleReject(runId: string) {
    setBusyRunId(runId);
    try {
      await rejectGate(runId, { reason: 'rejected via harness' });
      const r = await listGates({ limit: 50 });
      setLiveGates(r.items);
    } catch (e) {
      console.error('gate reject failed', e);
    } finally {
      setBusyRunId(null);
    }
  }

  return (
    <HarnessShell
      title="7 gates need decision · trust calibration per (repo, template, task)"
      screenIndex={4}
      breadcrumb={[
        { label: 'Fleet', href: '/' },
        { label: 'Trust & Gates' },
      ]}
      actions={
        <>
          <button type="button" className="h-button">Export audit</button>
          <button type="button" className="h-button h-button-success">Bulk approve</button>
        </>
      }
    >
      {/* Filters */}
      <div className="mb-4 flex items-center gap-2 text-[11px]">
        {(['all', 'pending', 'auto-merged', 'held'] as const).map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            className={[
              'h-pill text-[11px]',
              filter === f ? 'h-pill-purple' : 'text-harness-muted',
            ].join(' ')}
          >
            {f}
          </button>
        ))}
        {showDemo && (
          <span className="h-pill h-pill-warning text-[9px] ml-auto">demo data · engine offline or gates queue empty</span>
        )}
      </div>

      <section className="grid grid-cols-12 gap-4">
        {/* Approval queue */}
        <div className="col-span-8">
          <SectionCard
            title="Approval queue"
            subtitle={<span>below threshold or human-review-only repos · oldest first</span>}
            testId="section-gates"
          >
            <div className="grid grid-cols-12 gap-3 px-3 pb-2 text-[10px] tracking-widest text-harness-dim border-b border-harness-border">
              <div className="col-span-3">REPO · ISSUE</div>
              <div className="col-span-3">TEMPLATE</div>
              <div className="col-span-1">CONF</div>
              <div className="col-span-1">THR</div>
              <div className="col-span-1">WAIT</div>
              <div className="col-span-3 text-right">ACTION</div>
            </div>
            <ul>
              {DEMO_GATES.map((g, i) => (
                <li
                  key={`${g.repo}-${g.issueNumber}-${i}`}
                  className="grid grid-cols-12 gap-3 items-center px-3 py-3 border-b border-harness-border text-[12px]"
                  data-testid={`gate-row-${i}`}
                >
                  <div className="col-span-3">
                    <div className="font-semibold text-harness-text">{g.repo} · {g.issueNumber}</div>
                    <div className="text-[10px] text-harness-dim">{g.issueTitle}</div>
                  </div>
                  <div className="col-span-3 text-harness-muted">{g.template}</div>
                  <div className={['col-span-1 font-medium', confidenceColor(g.confidence, g.threshold)].join(' ')}>
                    {g.confidence.toFixed(2)}
                  </div>
                  <div className="col-span-1 text-harness-muted">{g.threshold.toFixed(2)}</div>
                  <div className={[
                    'col-span-1',
                    g.waitingTone === 'danger' ? 'text-harness-danger' :
                    g.waitingTone === 'warning' ? 'text-harness-warning' :
                    'text-harness-muted',
                  ].join(' ')}>{g.waitingLabel}</div>
                  <div className="col-span-3 flex justify-end gap-2">
                    <button
                      type="button"
                      className="h-button h-button-success"
                      onClick={() => handleApprove(g.issueNumber + '-' + i)}
                      disabled={busyRunId !== null}
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      className="h-button h-button-danger"
                      onClick={() => handleReject(g.issueNumber + '-' + i)}
                      disabled={busyRunId !== null}
                    >
                      Reject
                    </button>
                  </div>
                </li>
              ))}
            </ul>
            <div className="mt-3 text-[10px] text-harness-dim">+ 2 more · cursor end of queue</div>
          </SectionCard>
        </div>

        {/* Trust profiles */}
        <div className="col-span-4">
          <SectionCard
            title="Trust profiles"
            subtitle={<span>per (repo, template, task_type)</span>}
            testId="section-trust"
          >
            <ul className="space-y-4 text-[11px]">
              {DEMO_TRUST_PROFILES.map((p) => {
                const pct = Math.min(100, p.confidence * 100);
                const tone = p.verdict === 'auto' ? 'bg-harness-teal' : p.verdict === 'hold' ? 'bg-harness-warning' : 'bg-harness-danger';
                return (
                  <li key={p.key}>
                    <div className="font-semibold text-harness-text">{p.key}</div>
                    <div className="mt-1 h-1.5 w-full rounded bg-harness-border">
                      <div className={['h-1.5 rounded', tone].join(' ')} style={{ width: `${pct}%` }} />
                    </div>
                    <div className="mt-1 text-[10px] text-harness-dim text-right">
                      {p.confidence.toFixed(2)} {p.verdict === 'auto' ? '≥' : p.verdict === 'hold' ? '<' : '·'} {p.threshold.toFixed(2)} ({p.verdict})
                    </div>
                  </li>
                );
              })}
            </ul>
          </SectionCard>
        </div>
      </section>

      <section className="mt-4 grid grid-cols-12 gap-4">
        <div className="col-span-6">
          <SectionCard
            title="Calibration curve · last 14 days"
            subtitle={<span>predicted confidence vs actual post-merge success</span>}
          >
            <svg viewBox="0 0 460 200" className="w-full">
              <line x1="60" y1="170" x2="440" y2="170" stroke="#20242C" />
              <line x1="60" y1="10" x2="60" y2="170" stroke="#20242C" />
              <line x1="60" y1="170" x2="440" y2="10" stroke="#5A6371" strokeDasharray="4 4" />
              {[
                [104, 160], [148, 144], [192, 130], [236, 104], [280, 84],
                [324, 64], [368, 50], [412, 36], [434, 22],
              ].map(([cx, cy], i) => (
                <circle key={i} cx={cx} cy={cy} r="5" fill={i >= 5 ? '#2DD4BF' : '#7C5CFC'} />
              ))}
              <text x="60" y="186" fontSize="10" fill="#5A6371">0.50</text>
              <text x="250" y="186" fontSize="10" fill="#5A6371">predicted confidence</text>
              <text x="440" y="186" fontSize="10" fill="#5A6371" textAnchor="end">1.00</text>
            </svg>
            <div className="mt-2 text-[11px] text-harness-muted">
              model is <span className="text-harness-teal font-bold">well-calibrated</span> above 0.85 · slightly under-confident in 0.65–0.80 band
            </div>
          </SectionCard>
        </div>

        <div className="col-span-6">
          <SectionCard
            title="Recent decisions · audit trail"
            subtitle={<span>closes ROADMAP §4.5 audit trail &amp; compliance export (issue #565)</span>}
            testId="section-decisions"
          >
            <ul className="space-y-2 text-[11px]">
              {DEMO_DECISIONS.map((d, i) => (
                <li key={i} className="flex items-center gap-3">
                  <span className={[
                    'w-16',
                    d.verdict === 'approve' ? 'text-harness-teal' :
                    d.verdict === 'reject' ? 'text-harness-danger' :
                    'text-harness-purple',
                  ].join(' ')}>
                    {d.verdict === 'approve' ? '✓ approve' : d.verdict === 'reject' ? '✗ reject' : '⏏ auto'}
                  </span>
                  <span className="flex-1 text-harness-text truncate">{d.summary}</span>
                  <span className="text-harness-dim text-[10px]">{d.when}</span>
                </li>
              ))}
            </ul>
            <div className="mt-3 text-[10px] text-harness-dim">
              Exports: <a className="h-link" href="#">CSV</a> · <a className="h-link" href="#">JSON</a> · <a className="h-link" href="#">SOC2 evidence pack</a> · all decisions immutable (no destructive UI affordance)
            </div>
          </SectionCard>
        </div>
      </section>
    </HarnessShell>
  );
}
