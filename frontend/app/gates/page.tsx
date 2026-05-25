'use client';

/**
 * Trust & Gates — screen 4 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/04-trust-gates.svg
 *
 * Wires the existing `/api/v1/gates` endpoints (#743). When the engine is
 * reachable AND the queue is non-empty, real GateRecord objects render with
 * working Approve / Reject calls. Otherwise demo data falls back so the page
 * never blanks during development or in CI.
 *
 * Operator affordances: per-row Approve / Reject (POST /api/v1/gates/.../approve
 * and .../reject) and a top-bar Bulk Approve that batches all visible rows
 * through `approveGate`.
 */

import { useEffect, useMemo, useState } from 'react';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';
import {
  listGates,
  approveGate,
  rejectGate,
  listTrustProfiles,
  listDecisions,
  ApiError,
} from '@/lib/api';
import type { GateRecord, GateStatus, TrustProfileRecord, DecisionRecord } from '@/lib/api';
import {
  DEMO_GATES,
  DEMO_TRUST_PROFILES,
  DEMO_DECISIONS,
} from '@/lib/demo-data';

/**
 * Operator-facing filter vocabulary. Maps to canonical engine `GateStatus`
 * values (see `lib/api.ts`) so the API actually filters server-side
 * instead of every pill returning the same rows.
 *
 *   'pending'     → awaiting_approval   (the queue we ask operators to triage)
 *   'auto-merged' → merged              (audit trail of auto-completed gates)
 *   'held'        → rejected            (operator-rejected, kept for audit)
 *   'all'         → no status filter
 */
type Filter = 'all' | 'pending' | 'auto-merged' | 'held';

const FILTER_TO_STATUS: Record<Exclude<Filter, 'all'>, GateStatus> = {
  pending: 'awaiting_approval',
  'auto-merged': 'merged',
  held: 'rejected',
};

/** Shape consumed by the rendering layer (normalized across real + demo). */
interface GateRow {
  readonly key: string;
  readonly headline: string;
  readonly subline: string;
  readonly template: string;
  readonly confidence: number | null;
  readonly threshold: number;
  readonly waitingLabel: string;
  readonly waitingTone: 'warning' | 'danger' | 'neutral';
  readonly approveId?: string;   // present iff this is a real record
}

function confidenceColor(c: number | null, threshold: number): string {
  if (c === null) return 'text-harness-muted';
  if (c >= threshold) return 'text-harness-teal';
  if (c >= threshold - 0.1) return 'text-harness-warning';
  return 'text-harness-danger';
}

function elapsedHoursMin(iso: string): { label: string; tone: 'warning' | 'danger' | 'neutral' } {
  const now = Date.now();
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return { label: '—', tone: 'neutral' };
  const diffMin = Math.floor((now - t) / 60_000);
  if (diffMin < 0) return { label: 'queued', tone: 'neutral' };
  if (diffMin < 60) return { label: `${diffMin}m`, tone: diffMin > 30 ? 'warning' : 'neutral' };
  const h = Math.floor(diffMin / 60);
  const m = diffMin % 60;
  return { label: `${h}h ${m}m`, tone: h >= 2 ? 'danger' : 'warning' };
}

/** Convert a real GateRecord into the normalized row shape. */
function fromGateRecord(g: GateRecord): GateRow {
  const wait = elapsedHoursMin(g.created_at);
  return {
    key: g.run_id,
    headline: g.pipeline_id || g.run_id.slice(0, 12),
    subline: g.message ?? `${g.branch} → ${g.base_branch}`,
    template: g.pipeline_id,
    confidence: g.scoring_score,
    threshold: 0.90,
    waitingLabel: wait.label,
    waitingTone: wait.tone,
    approveId: g.run_id,
  };
}

export default function TrustAndGatesPage() {
  const [filter, setFilter] = useState<Filter>('all');
  const [liveGates, setLiveGates] = useState<readonly GateRecord[] | null>(null);
  const [trustProfiles, setTrustProfiles] = useState<readonly TrustProfileRecord[] | null>(null);
  const [decisions, setDecisions] = useState<readonly DecisionRecord[] | null>(null);
  const [engineUp, setEngineUp] = useState<boolean | null>(null);
  const [busyRunId, setBusyRunId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function refresh(currentFilter: Filter = filter) {
    try {
      const statusParam = currentFilter === 'all' ? undefined : FILTER_TO_STATUS[currentFilter];
      const r = await listGates({ limit: 50, status: statusParam });
      setLiveGates(r.items);
      setEngineUp(true);
    } catch (e) {
      setEngineUp(false);
    }
  }

  // Re-fetch whenever the filter changes so the queue actually narrows
  // (audit 2026-05-25: filter pills previously had no effect on results).
  useEffect(() => { void refresh(filter); }, [filter]);

  // Trust profiles + decisions are filter-independent — fetch once on mount.
  // Both fail soft: 404 / engine-offline → null, demo data renders.
  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([listTrustProfiles(), listDecisions({ limit: 20 })])
      .then(([profRes, decRes]) => {
        if (cancelled) return;
        if (profRes.status === 'fulfilled') setTrustProfiles(profRes.value.items);
        if (decRes.status === 'fulfilled') setDecisions(decRes.value.items);
      });
    return () => { cancelled = true; };
  }, []);

  const usingLive = engineUp === true && liveGates !== null && liveGates.length > 0;

  const rows: readonly GateRow[] = usingLive
    ? liveGates!.map(fromGateRecord)
    : DEMO_GATES.map((g, i) => ({
        key: `${g.repo}-${g.issueNumber}-${i}`,
        headline: `${g.repo} · ${g.issueNumber}`,
        subline: g.issueTitle,
        template: g.template,
        confidence: g.confidence,
        threshold: g.threshold,
        waitingLabel: g.waitingLabel,
        waitingTone: g.waitingTone,
      }));

  async function handleApprove(row: GateRow) {
    if (!row.approveId) { setActionError('Cannot approve demo row — engine offline'); return; }
    setBusyRunId(row.approveId);
    setActionError(null);
    try {
      await approveGate(row.approveId, { message: 'approved via harness' });
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status}: ${e.message}` : e instanceof Error ? e.message : 'approve failed';
      setActionError(msg);
    } finally {
      setBusyRunId(null);
    }
  }

  async function handleReject(row: GateRow) {
    if (!row.approveId) { setActionError('Cannot reject demo row — engine offline'); return; }
    setBusyRunId(row.approveId);
    setActionError(null);
    try {
      await rejectGate(row.approveId, { reason: 'rejected via harness' });
      await refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status}: ${e.message}` : e instanceof Error ? e.message : 'reject failed';
      setActionError(msg);
    } finally {
      setBusyRunId(null);
    }
  }

  async function handleBulkApprove() {
    if (!usingLive) { setActionError('Bulk approve disabled in demo mode'); return; }
    setActionError(null);
    for (const row of rows) {
      if (row.approveId) {
        setBusyRunId(row.approveId);
        try {
          await approveGate(row.approveId, { message: 'bulk approved via harness' });
        } catch (e) {
          /* continue best-effort */
        }
      }
    }
    setBusyRunId(null);
    await refresh();
  }

  return (
    <HarnessShell
      title={
        usingLive
          ? `${rows.length} gate${rows.length === 1 ? '' : 's'} need decision · live`
          : '7 gates need decision · trust calibration per (repo, template, task)'
      }
      screenIndex={4}
      breadcrumb={[
        { label: 'Fleet', href: '/' },
        { label: 'Trust & Gates' },
      ]}
      actions={
        <>
          <button type="button" className="h-button">Export audit</button>
          <button
            type="button"
            className="h-button h-button-success"
            onClick={handleBulkApprove}
            disabled={!usingLive || busyRunId !== null}
            title={usingLive ? '' : 'Bulk approve disabled — no live gates'}
          >
            Bulk approve
          </button>
        </>
      }
    >
      {/* Filters + status banner */}
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
            data-testid={`gates-filter-${f}`}
            title={
              f === 'all'
                ? 'No status filter — show all gates'
                : `Status filter: ${FILTER_TO_STATUS[f]}`
            }
          >
            {f}
          </button>
        ))}
        {!usingLive && (
          <span className="h-pill h-pill-warning text-[9px] ml-auto" data-testid="gates-demo-banner">
            {engineUp === false ? 'demo data · engine offline' : 'demo data · no pending gates in engine'}
          </span>
        )}
        {usingLive && (
          <span className="h-pill h-pill-success text-[9px] ml-auto" data-testid="gates-live-banner">
            live · {rows.length} from /api/v1/gates
          </span>
        )}
      </div>

      {actionError && (
        <div className="mb-3 rounded-md border border-harness-danger bg-[#2A1F1F] p-3 text-[12px] text-harness-danger">
          {actionError}
        </div>
      )}

      <section className="grid grid-cols-12 gap-4">
        {/* Approval queue */}
        <div className="col-span-8">
          <SectionCard
            title="Approval queue"
            subtitle={<span>below threshold or human-review-only repos · oldest first</span>}
            testId="section-gates"
          >
            <div className="grid grid-cols-12 gap-3 px-3 pb-2 text-[10px] tracking-widest text-harness-dim border-b border-harness-border">
              <div className="col-span-3">REPO · ISSUE / RUN</div>
              <div className="col-span-3">TEMPLATE</div>
              <div className="col-span-1">CONF</div>
              <div className="col-span-1">THR</div>
              <div className="col-span-1">WAIT</div>
              <div className="col-span-3 text-right">ACTION</div>
            </div>
            <ul>
              {rows.length === 0 ? (
                <li className="py-8 text-center text-[12px] text-harness-muted">
                  No gates in the queue. The engine has no pipelines waiting on a human decision right now.
                </li>
              ) : rows.map((row, i) => (
                <li
                  key={row.key}
                  className="grid grid-cols-12 gap-3 items-center px-3 py-3 border-b border-harness-border text-[12px]"
                  data-testid={`gate-row-${i}`}
                >
                  <div className="col-span-3">
                    <div className="font-semibold text-harness-text">{row.headline}</div>
                    <div className="text-[10px] text-harness-dim truncate" title={row.subline}>{row.subline}</div>
                  </div>
                  <div className="col-span-3 text-harness-muted truncate" title={row.template}>{row.template}</div>
                  <div className={['col-span-1 font-medium', confidenceColor(row.confidence, row.threshold)].join(' ')}>
                    {row.confidence === null ? '—' : row.confidence.toFixed(2)}
                  </div>
                  <div className="col-span-1 text-harness-muted">{row.threshold.toFixed(2)}</div>
                  <div className={[
                    'col-span-1',
                    row.waitingTone === 'danger' ? 'text-harness-danger' :
                    row.waitingTone === 'warning' ? 'text-harness-warning' :
                    'text-harness-muted',
                  ].join(' ')}>{row.waitingLabel}</div>
                  <div className="col-span-3 flex justify-end gap-2">
                    <button
                      type="button"
                      className="h-button h-button-success"
                      onClick={() => handleApprove(row)}
                      disabled={busyRunId !== null || !row.approveId}
                      title={row.approveId ? '' : 'Demo row — engine offline'}
                    >
                      {busyRunId === row.approveId ? '...' : 'Approve'}
                    </button>
                    <button
                      type="button"
                      className="h-button h-button-danger"
                      onClick={() => handleReject(row)}
                      disabled={busyRunId !== null || !row.approveId}
                      title={row.approveId ? '' : 'Demo row — engine offline'}
                    >
                      {busyRunId === row.approveId ? '...' : 'Reject'}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
            {usingLive && rows.length > 0 && (
              <div className="mt-3 text-[10px] text-harness-dim">
                {rows.length} live gate{rows.length === 1 ? '' : 's'} · approve / reject calls hit /api/v1/gates/{`{run_id}`}/(approve|reject)
              </div>
            )}
            {!usingLive && rows.length > 0 && (
              <div className="mt-3 text-[10px] text-harness-dim">+ 2 more · cursor end of queue · demo data</div>
            )}
          </SectionCard>
        </div>

        {/* Trust profiles */}
        <div className="col-span-4">
          <SectionCard
            title="Trust profiles"
            subtitle={
              trustProfiles && trustProfiles.length > 0
                ? <span>per (repo, template, task) · {trustProfiles.length} live from /api/v1/trust-profiles</span>
                : <span>per (repo, template, task) · <span className="text-harness-warning">demo · no trust profiles in engine yet</span></span>
            }
            testId="section-trust"
          >
            <ul className="space-y-4 text-[11px]">
              {trustProfiles && trustProfiles.length > 0 ? trustProfiles.slice(0, 8).map((p) => {
                const pct = Math.min(100, p.trust_score * 100);
                // Verdict logic: trust_score >= auto_merge_threshold → auto-merge eligible.
                // Below human_review_threshold → review-all stance.
                // Between → hold (waiting on threshold drift or operator decision).
                const verdict: 'auto' | 'hold' | 'review-all' =
                  p.trust_score >= p.auto_merge_threshold ? 'auto' :
                  p.trust_score < p.human_review_threshold ? 'review-all' : 'hold';
                const tone = verdict === 'auto' ? 'bg-harness-teal' : verdict === 'hold' ? 'bg-harness-warning' : 'bg-harness-danger';
                const key = `${p.repo} · ${p.template_id} · ${p.task_type}`;
                return (
                  <li key={p.id}>
                    <div className="font-semibold text-harness-text">{key}</div>
                    <div className="mt-1 h-1.5 w-full rounded bg-harness-border">
                      <div className={['h-1.5 rounded', tone].join(' ')} style={{ width: `${pct}%` }} />
                    </div>
                    <div className="mt-1 text-[10px] text-harness-dim text-right">
                      {p.trust_score.toFixed(2)} {verdict === 'auto' ? '≥' : verdict === 'hold' ? '·' : '<'} {p.auto_merge_threshold.toFixed(2)} ({verdict})
                    </div>
                  </li>
                );
              }) : DEMO_TRUST_PROFILES.map((p) => {
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
            subtitle={
              decisions && decisions.length > 0
                ? <span>{decisions.length} live from /api/v1/decisions · closes ROADMAP §4.5</span>
                : <span>closes ROADMAP §4.5 audit trail &amp; compliance export (issue #565) · <span className="text-harness-warning">demo · no decisions logged yet</span></span>
            }
            testId="section-decisions"
          >
            <ul className="space-y-2 text-[11px]">
              {decisions && decisions.length > 0 ? decisions.map((d) => {
                // Map APPROVE / REQUEST_CHANGES (engine canonical) onto the
                // approve / reject / auto vocabulary the demo data uses.
                const v = (d.verdict ?? '').toLowerCase();
                const kind: 'approve' | 'reject' | 'auto' =
                  v === 'approve' ? 'approve' :
                  v === 'request_changes' || v === 'reject' ? 'reject' :
                  'auto';
                const ago = (() => {
                  const t = new Date(d.created_at).getTime();
                  if (!Number.isFinite(t)) return '—';
                  const min = Math.floor((Date.now() - t) / 60000);
                  if (min < 60) return `${min} min ago`;
                  const h = Math.floor(min / 60);
                  if (h < 24) return `${h}h ago`;
                  const days = Math.floor(h / 24);
                  return `${days}d ago`;
                })();
                return (
                  <li key={d.id} className="flex items-center gap-3">
                    <span className={[
                      'w-16',
                      kind === 'approve' ? 'text-harness-teal' :
                      kind === 'reject' ? 'text-harness-danger' :
                      'text-harness-purple',
                    ].join(' ')}>
                      {kind === 'approve' ? '✓ approve' : kind === 'reject' ? '✗ reject' : '⏏ auto'}
                    </span>
                    <span className="flex-1 text-harness-text truncate" title={d.run_id}>
                      run {d.run_id.slice(0, 8)} · {d.reviewer_model ?? 'reviewer'} {d.confidence !== null ? `· ${d.confidence.toFixed(2)}` : ''}
                    </span>
                    <span className="text-harness-dim text-[10px]">{ago}</span>
                  </li>
                );
              }) : DEMO_DECISIONS.map((d, i) => (
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
