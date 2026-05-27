'use client';

/**
 * Adversary Loop visualizer — screen 3 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/03-adversary-loop.svg
 *
 * Data sources:
 *   - With `?run=<id>` in the URL: fetch real dialogue rounds from
 *     `GET /api/v1/runs/{id}/dialogue` (Track B dialogue phase artifact).
 *     If the run has no dialogue artifact, falls back to demo data with a
 *     "no dialogue artifact" banner so the IA stays reviewable.
 *   - Without `?run=`: renders the canonical demo round-trip from the
 *     SVG mockup so visitors can see what the screen looks like.
 */

import { Suspense } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';
import { getRunDialogue, type RunDialogue, type RunDialogueRound } from '@/lib/api';
import { useApi } from '@/lib/useApi';

// Sentinel returned by the no-op fetcher used when no `?run=<id>` URL param
// is present. The page renders demo data in that case (loadState === 'idle').
const IDLE_SENTINEL = Symbol('adversary-idle');
type DialogueOrIdle = RunDialogue | typeof IDLE_SENTINEL;

function AdversaryLoopInner() {
  const search = useSearchParams();
  const runIdParam = search?.get('run') ?? null;

  // #870 — migrated to useApi. The fetcher resolves to IDLE_SENTINEL when
  // there's no `?run=<id>` URL param, preserving the pre-migration "idle"
  // branch without polluting the page with extra state.
  const { data, error, loading } = useApi<DialogueOrIdle>(
    () => (runIdParam ? getRunDialogue(runIdParam) : Promise.resolve(IDLE_SENTINEL as DialogueOrIdle)),
    [runIdParam],
  );

  const dialogue: RunDialogue | null =
    data === null || data === IDLE_SENTINEL ? null : (data as RunDialogue);

  // Map the useApi state machine onto the page's vocabulary. 404 errors map
  // to 'none' (most common — most runs have no dialogue artifact); other
  // rejections map to 'error'. ApiError preserves a numeric `.status` field
  // which we read via a defensive cast.
  let loadState: 'idle' | 'loading' | 'ok' | 'none' | 'error';
  if (!runIdParam) {
    loadState = 'idle';
  } else if (loading) {
    loadState = 'loading';
  } else if (error) {
    const status = (error as Error & { status?: number }).status;
    loadState = status === 404 ? 'none' : 'error';
  } else {
    loadState = 'ok';
  }

  const usingLive = loadState === 'ok' && dialogue !== null && dialogue.rounds.length > 0;
  const drafterModel = usingLive
    ? dialogue!.rounds.find((r) => r.side === 'drafter')?.model ?? 'unknown drafter model'
    : 'claude-sonnet-4-6';
  const reviewerModel = usingLive
    ? dialogue!.rounds.find((r) => r.side === 'reviewer')?.model ?? 'unknown reviewer model'
    : 'gemini-3-pro';
  const finalVerdict = usingLive
    ? [...dialogue!.rounds].reverse().find((r) => r.side === 'reviewer' && r.verdict)?.verdict ?? null
    : 'approve';

  return (
    <HarnessShell
      title={
        runIdParam
          ? `Cross-model dialogue · run ${runIdParam.slice(0, 8)}${usingLive ? '' : ' · no dialogue artifact'}`
          : 'Cross-model dialogue · drafter ↔ reviewer'
      }
      screenIndex={3}
      breadcrumb={
        runIdParam
          ? [
              { label: 'Fleet', href: '/' },
              { label: `Run ${runIdParam.slice(0, 8)}`, href: `/runs/${encodeURIComponent(runIdParam)}` },
              { label: 'Adversary Loop · 1c spec_adversary' },
            ]
          : [
              { label: 'Fleet', href: '/' },
              { label: 'Adversary Loop (demo)' },
            ]
      }
      actions={
        <>
          {finalVerdict ? (
            <span className={[
              'h-pill text-[11px]',
              finalVerdict === 'approve' ? 'h-pill-success' :
              finalVerdict === 'revise' || finalVerdict === 'request_changes' ? 'h-pill-warning' :
              'h-pill-danger',
            ].join(' ')}>
              {finalVerdict.toUpperCase()} · R{usingLive ? Math.max(...dialogue!.rounds.map((r) => r.index)) : 2}
            </span>
          ) : (
            <span className="h-pill text-harness-muted text-[11px]">no verdict yet</span>
          )}
          <button type="button" className="h-button">Export ↗</button>
          <button type="button" className="h-button h-button-primary">Replay run</button>
        </>
      }
    >
      {runIdParam && loadState === 'loading' && (
        <div className="mb-4 h-pill h-pill-purple text-[11px]">loading dialogue artifact for run {runIdParam.slice(0, 8)}…</div>
      )}
      {runIdParam && loadState === 'none' && (
        <div className="mb-4 h-pill h-pill-warning text-[11px]">
          run {runIdParam.slice(0, 8)} has no dialogue artifact (Track B not enabled for this run) — showing demo
        </div>
      )}
      {runIdParam && loadState === 'error' && (
        <div className="mb-4 h-pill h-pill-danger text-[11px]">
          failed to load dialogue for run {runIdParam.slice(0, 8)} — showing demo
        </div>
      )}
      {!runIdParam && (
        <div className="mb-4 h-pill text-harness-muted text-[11px]">demo data · pass <code className="font-mono">?run=&lt;run-id&gt;</code> to render a real dialogue</div>
      )}

      {/* Model identity header */}
      <SectionCard
        title="Model identities · this round-trip"
        subtitle={<span>cross-model adversary at the phase boundary — the IP wedge per 2026-05-21 pivot</span>}
      >
        <div className="grid grid-cols-12 items-center gap-3">
          <div className="col-span-5 h-card h-card-purple p-4" style={{ background: 'linear-gradient(180deg, #1F1B2E 0%, #181425 100%)' }}>
            <div className="h-section-label" style={{ color: '#8A93A2' }}>DRAFTER</div>
            <div className="mt-1 text-[14px] font-bold text-harness-text">{drafterModel} · spec author</div>
            <div className="mt-1 text-[11px] text-harness-muted">fresh context per round · no reviewer history carry</div>
          </div>
          <div className="col-span-2 text-center text-harness-muted text-lg">⇆</div>
          <div className="col-span-5 h-card h-card-teal p-4" style={{ background: 'linear-gradient(180deg, #1A2A28 0%, #142220 100%)' }}>
            <div className="h-section-label" style={{ color: '#8A93A2' }}>REVIEWER · DIFFERENT FAMILY</div>
            <div className="mt-1 text-[14px] font-bold text-harness-text">{reviewerModel} · deep-think (Track B)</div>
            <div className="mt-1 text-[11px] text-harness-muted">fresh context per round · orchemist-adversary subagent</div>
          </div>
        </div>
      </SectionCard>

      {/* Round columns */}
      <section className="mt-4 grid grid-cols-12 gap-4">
        <div className="col-span-4">
          <div className="h-section-label mb-3">ROUND 1 · 14:02 UTC</div>
          <div className="h-card h-card-purple p-4 mb-3" style={{ background: 'linear-gradient(180deg, #1F1B2E 0%, #181425 100%)' }}>
            <div className="h-section-label" style={{ color: '#7C5CFC' }}>DRAFTER · spec.md</div>
            <div className="mt-1 text-[12px] font-semibold text-harness-text">Proposes: 4 new files, no consume verdict</div>
            <ul className="mt-2 text-[11px] text-harness-muted space-y-1">
              <li>- src/.../verdict_extract.py (new)</li>
              <li>- src/.../verdict_normalize.py (new)</li>
              <li>- 2 dependent test files (new)</li>
            </ul>
            <div className="mt-3 text-[10px] text-harness-dim">cost $0.18 · 1m 02s · 1.3k in / 480 out</div>
          </div>
          <div className="h-card h-card-teal p-4" style={{ background: 'linear-gradient(180deg, #1A2A28 0%, #142220 100%)' }}>
            <div className="h-section-label" style={{ color: '#2DD4BF' }}>REVIEWER · spec_adversary.md</div>
            <div className="mt-1 text-[12px] font-bold text-harness-text">VERDICT · REVISE</div>
            <div className="mt-2 flex flex-wrap gap-2">
              <span className="h-pill h-pill-danger text-[9px]">7d divergence</span>
            </div>
            <p className="mt-2 text-[11px] text-harness-muted">
              &quot;verdict_extract overlaps verdict_parser.py:88; verdict label is CONSUME, not NEW-OK.&quot;
            </p>
            <p className="mt-2 text-[11px] text-harness-muted">
              &quot;verdict_normalize is EXTEND (parameterize existing extract_verdict, do not duplicate).&quot;
            </p>
            <div className="mt-3 text-[10px] text-harness-dim">cost $0.34 · 1m 58s · 2.6k in / 920 out</div>
          </div>
        </div>

        <div className="col-span-4">
          <div className="h-section-label mb-3">ROUND 2 · 14:14 UTC</div>
          <div className="h-card h-card-purple p-4 mb-3" style={{ background: 'linear-gradient(180deg, #1F1B2E 0%, #181425 100%)' }}>
            <div className="h-section-label" style={{ color: '#7C5CFC' }}>DRAFTER · spec.md v2</div>
            <div className="mt-1 text-[12px] font-semibold text-harness-text">Revised: 1 EXTEND, 2 CONSUME, 0 new files</div>
            <ul className="mt-2 text-[11px] text-harness-muted space-y-1">
              <li>- verdict_parser.extract_verdict EXTEND</li>
              <li>&nbsp;&nbsp;(add label=CONSUME|EXTEND param)</li>
              <li>- review_parser CONSUME (re-export)</li>
            </ul>
            <div className="mt-3 text-[10px] text-harness-dim">cost $0.14 · 48s · diff vs R1: -3 files, +1 sig change</div>
          </div>
          <div className="h-card h-card-teal p-4" style={{ background: 'linear-gradient(180deg, #1A2A28 0%, #142220 100%)' }}>
            <div className="h-section-label" style={{ color: '#2DD4BF' }}>REVIEWER · spec_adversary.md v2</div>
            <div className="mt-1 text-[12px] font-bold text-harness-text">VERDICT · APPROVE</div>
            <div className="mt-2 flex flex-wrap gap-2">
              <span className="h-pill h-pill-success text-[9px]">7d resolved</span>
              <span className="h-pill h-pill-purple text-[9px]">EXTEND ok</span>
            </div>
            <p className="mt-2 text-[11px] text-harness-muted">
              &quot;Spec now anchored to verdict_parser:88; EXTEND signature widens correctly without breaking existing callers (3 sites verified).&quot;
            </p>
            <div className="mt-3 text-[10px] text-harness-dim">cost $0.28 · 1m 24s · 2.1k in / 640 out</div>
          </div>
        </div>

        {/* Round 3 / convergence */}
        <div className="col-span-4">
          <div className="h-section-label mb-3">ROUND 3 · convergence</div>
          <div className="h-card h-card-teal p-5" style={{ borderStyle: 'dashed' }}>
            <h3 className="text-[16px] font-bold text-harness-teal">Convergence reached at R2</h3>
            <p className="mt-1 text-[11px] text-harness-muted">no R3 needed · jaccard 0.93 ≥ 0.95 within tolerance · 2 consecutive APPROVE pairs</p>

            <div className="mt-4 h-section-label">CONVERGENCE METRIC</div>
            <div className="mt-2 text-[11px] text-harness-muted">Jaccard(spec.md R1, spec.md R2)</div>
            <div className="text-[32px] font-bold text-harness-teal">0.93</div>
            <div className="text-[11px] text-harness-dim">threshold 0.95</div>

            <div className="mt-4 h-section-label">VERDICT TIMELINE</div>
            <ul className="mt-2 space-y-2 text-[11px]">
              <li className="flex items-center gap-2"><span className="inline-block h-3 w-3 rounded-sm bg-harness-danger" /> <span className="text-harness-text">R1 REVISE</span> <span className="ml-auto text-[10px] text-harness-dim">[divergence][7d]</span></li>
              <li className="flex items-center gap-2"><span className="inline-block h-3 w-3 rounded-sm bg-harness-teal" /> <span className="text-harness-text">R2 APPROVE</span> <span className="ml-auto text-[10px] text-harness-dim">no findings</span></li>
            </ul>

            <div className="mt-4 h-section-label">DOWNSTREAM HANDOFF</div>
            <div className="mt-1 text-[11px] text-harness-muted">writes spec.md (sealed) →</div>
            <Link href="/runs/b90a3719-orchemist-802#acceptance_test" className="h-link text-[11px]">acceptance_test phase ⌘2</Link>
          </div>
        </div>
      </section>

      {/* Turn-by-turn ledger */}
      <SectionCard
        title="Per-turn ledger · why this loop is defensible IP"
        subtitle={<span>cross-model adversary at phase boundary · no competitor in the 2026 SDD market ships this</span>}
        className="mt-4"
      >
        <table className="w-full text-[12px] border-collapse">
          <thead>
            <tr className="text-[10px] text-harness-dim tracking-widest text-left">
              <th className="font-medium pb-2 pr-3">TURN</th>
              <th className="font-medium pb-2 pr-3">SIDE</th>
              <th className="font-medium pb-2 pr-3">MODEL</th>
              <th className="font-medium pb-2 pr-3">TOKENS IN/OUT</th>
              <th className="font-medium pb-2 pr-3">COST $</th>
              <th className="font-medium pb-2 pr-3">VERDICT</th>
              <th className="font-medium pb-2 pr-3">JACCARD vs PREV</th>
              <th className="font-medium pb-2 text-right">FINDINGS</th>
            </tr>
          </thead>
          <tbody className="border-t border-harness-border">
            {usingLive ? (
              dialogue!.rounds.map((r, i) => (
                <tr key={`${r.index}-${r.side}-${i}`} className="border-b border-harness-border last:border-0">
                  <td className="py-2 pr-3 text-harness-text">R{r.index}·{r.side === 'drafter' ? '1' : '2'}</td>
                  <td className={['py-2 pr-3', r.side === 'drafter' ? 'text-harness-purple' : 'text-harness-teal'].join(' ')}>{r.side || '—'}</td>
                  <td className="py-2 pr-3 text-harness-text">{r.model ?? '—'}</td>
                  <td className="py-2 pr-3 text-harness-dim">— / —</td>
                  <td className="py-2 pr-3 text-harness-dim">—</td>
                  <td className={[
                    'py-2 pr-3',
                    r.verdict === 'approve' ? 'text-harness-teal' :
                    r.verdict === 'request_changes' || r.verdict === 'revise' ? 'text-harness-danger' :
                    'text-harness-muted',
                  ].join(' ')}>
                    {r.verdict ? r.verdict.toUpperCase() : (r.side === 'drafter' ? 'draft' : '—')}
                  </td>
                  <td className={[
                    'py-2 pr-3',
                    r.jaccard === null ? 'text-harness-dim' :
                    r.jaccard >= 0.9 ? 'text-harness-teal' :
                    r.jaccard >= 0.7 ? 'text-harness-warning' :
                    'text-harness-danger',
                  ].join(' ')}>
                    {r.jaccard === null ? '—' : r.jaccard.toFixed(2)}
                  </td>
                  <td className="py-2 text-right text-harness-dim">—</td>
                </tr>
              ))
            ) : (
              <>
                <tr className="border-b border-harness-border">
                  <td className="py-2 pr-3 text-harness-text">R1·1</td>
                  <td className="py-2 pr-3 text-harness-purple">drafter</td>
                  <td className="py-2 pr-3 text-harness-text">claude-sonnet-4-6</td>
                  <td className="py-2 pr-3 text-harness-text">1300 / 480</td>
                  <td className="py-2 pr-3 text-harness-text">0.18</td>
                  <td className="py-2 pr-3 text-harness-muted">draft</td>
                  <td className="py-2 pr-3 text-harness-dim">—</td>
                  <td className="py-2 text-right text-harness-dim">—</td>
                </tr>
                <tr className="border-b border-harness-border">
                  <td className="py-2 pr-3 text-harness-text">R1·2</td>
                  <td className="py-2 pr-3 text-harness-teal">reviewer</td>
                  <td className="py-2 pr-3 text-harness-text">gemini-3-pro</td>
                  <td className="py-2 pr-3 text-harness-text">2600 / 920</td>
                  <td className="py-2 pr-3 text-harness-text">0.34</td>
                  <td className="py-2 pr-3 text-harness-danger">REVISE</td>
                  <td className="py-2 pr-3 text-harness-dim">—</td>
                  <td className="py-2 text-right text-harness-danger">2 · 7d / verdict label</td>
                </tr>
                <tr className="border-b border-harness-border">
                  <td className="py-2 pr-3 text-harness-text">R2·1</td>
                  <td className="py-2 pr-3 text-harness-purple">drafter</td>
                  <td className="py-2 pr-3 text-harness-text">claude-sonnet-4-6</td>
                  <td className="py-2 pr-3 text-harness-text">1400 / 520</td>
                  <td className="py-2 pr-3 text-harness-text">0.14</td>
                  <td className="py-2 pr-3 text-harness-muted">revised</td>
                  <td className="py-2 pr-3 text-harness-warning">0.62</td>
                  <td className="py-2 text-right text-harness-dim">—</td>
                </tr>
                <tr>
                  <td className="py-2 pr-3 text-harness-text">R2·2</td>
                  <td className="py-2 pr-3 text-harness-teal">reviewer</td>
                  <td className="py-2 pr-3 text-harness-text">gemini-3-pro</td>
                  <td className="py-2 pr-3 text-harness-text">2100 / 640</td>
                  <td className="py-2 pr-3 text-harness-text">0.28</td>
                  <td className="py-2 pr-3 text-harness-teal">APPROVE</td>
                  <td className="py-2 pr-3 text-harness-teal">0.93</td>
                  <td className="py-2 text-right text-harness-teal">0 · sealed</td>
                </tr>
              </>
            )}
          </tbody>
        </table>
        <div className="mt-4 text-[10px] text-harness-dim">
          {usingLive
            ? <>Live · {dialogue!.rounds.length} turns from {dialogue!.filename}</>
            : <>Total · 7400 tok in · 2560 tok out · $0.94 · 4m 12s · 2 rounds · APPROVE → handoff acceptance_test</>}
        </div>
      </SectionCard>
    </HarnessShell>
  );
}

// `useSearchParams` requires a Suspense boundary in Next.js 14 static export.
export default function AdversaryLoopPage() {
  return (
    <Suspense fallback={null}>
      <AdversaryLoopInner />
    </Suspense>
  );
}
