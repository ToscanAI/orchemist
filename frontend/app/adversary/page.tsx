'use client';

/**
 * Adversary Loop visualizer — screen 3 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/03-adversary-loop.svg
 *
 * Shows the cross-model drafter↔reviewer dialogue at the spec_adversary
 * boundary. This is the marquee screen — the trust-engine wedge made visible.
 *
 * Data sources:
 *   - Track A (skills pack) — reads spec_adversary.md from the run's output_dir
 *     and reconstructs verdict turns. Single-model context per turn.
 *   - Track B (dialogue phase PR #808) — reads dialogue_phase.md with full
 *     turn-by-turn drafter/reviewer message pairs and Jaccard scores.
 *
 * Today this page renders the canonical demo data (one full round-trip with
 * convergence) so the IA is reviewable before #808 lands. Real-data wiring is
 * orchemist#810 sub-issue "Adversary loop visualizer".
 */

import Link from 'next/link';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';

export default function AdversaryLoopPage() {
  return (
    <HarnessShell
      title="Cross-model dialogue · drafter ↔ reviewer"
      screenIndex={3}
      breadcrumb={[
        { label: 'Fleet', href: '/' },
        { label: 'Run b90a3719', href: '/runs/b90a3719-orchemist-802' },
        { label: 'Adversary Loop · 1c spec_adversary' },
      ]}
      actions={
        <>
          <span className="h-pill h-pill-success">APPROVED · R2</span>
          <button type="button" className="h-button">Export ↗</button>
          <button type="button" className="h-button h-button-primary">Replay run</button>
        </>
      }
    >
      {/* Model identity header */}
      <SectionCard
        title="Model identities · this round-trip"
        subtitle={<span>cross-model adversary at the phase boundary — the IP wedge per 2026-05-21 pivot</span>}
      >
        <div className="grid grid-cols-12 items-center gap-3">
          <div className="col-span-5 h-card h-card-purple p-4" style={{ background: 'linear-gradient(180deg, #1F1B2E 0%, #181425 100%)' }}>
            <div className="h-section-label" style={{ color: '#8A93A2' }}>DRAFTER</div>
            <div className="mt-1 text-[14px] font-bold text-harness-text">claude-sonnet-4-6 · spec author</div>
            <div className="mt-1 text-[11px] text-harness-muted">fresh context per round · no reviewer history carry</div>
          </div>
          <div className="col-span-2 text-center text-harness-muted text-lg">⇆</div>
          <div className="col-span-5 h-card h-card-teal p-4" style={{ background: 'linear-gradient(180deg, #1A2A28 0%, #142220 100%)' }}>
            <div className="h-section-label" style={{ color: '#8A93A2' }}>REVIEWER · DIFFERENT FAMILY</div>
            <div className="mt-1 text-[14px] font-bold text-harness-text">gemini-3-pro · deep-think (Track B)</div>
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
          </tbody>
        </table>
        <div className="mt-4 text-[10px] text-harness-dim">
          Total · 7400 tok in · 2560 tok out · $0.94 · 4m 12s · 2 rounds · APPROVE → handoff acceptance_test
        </div>
      </SectionCard>
    </HarnessShell>
  );
}
