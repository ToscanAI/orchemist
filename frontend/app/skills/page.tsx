'use client';

/**
 * Skills Pack Mode — screen 6 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/06-skills-pack-mode.svg
 *
 * Track A surface: lets users see the locally-installed Claude Code skills
 * pack and replay a local run through the remote engine. The page hydrates
 * phase metadata from `GET /api/v1/phases` (#842) so the phase grid stays
 * in lockstep with the engine's canonical YAML.
 */

import Link from 'next/link';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';
import { listPhases, type PhasesResponse } from '@/lib/api';
import { useApi } from '@/lib/useApi';
import { derivePhaseDefs, type PhaseDef } from '@/lib/phaseLabels';

interface PhaseCard {
  readonly tag: string;
  readonly title: string;
  readonly subtitle: string;
  readonly highlight?: 'phase0' | 'opus' | 'engine';
}

/**
 * Render-time mapping from PhaseDef (canonical, backend-driven) to the
 * Skills page's card shape. Keeps the existing visual styling (PHASE
 * tag prefix + subtitle slot) while sourcing the data from /api/v1/phases.
 *
 * Tag derivation uses the label's prefix segment (everything before the
 * first '·'); for phases whose label is the synthesised default
 * (`${order} · ${id}`), this resolves to the order number. For phases
 * with custom overrides in `STANDARD_PIPELINE_OVERRIDES` it picks up the
 * sub-letter ordering (`1a`, `1b`, `3b`, etc.).
 */
function asCard(p: PhaseDef, index: number): PhaseCard {
  const highlight: PhaseCard['highlight'] =
    p.id === 'existing_symbols_inventory' ? 'phase0' :
    p.tier === 'opus' ? 'opus' :
    p.tier === 'engine' ? 'engine' :
    undefined;
  const title = p.id === 'existing_symbols_inventory' ? 'existing_symbols' : p.id;
  // Pull the prefix segment from the label. If no '·' is present (e.g.
  // a non-canonical pipeline whose phases lack an override), fall back
  // to the index — never the full phase id, which would produce ugly
  // tags like "PHASE existing_symbols_inventory".
  const labelParts = p.label.split('·');
  const tagPrefix = labelParts.length >= 2
    ? (labelParts[0]?.trim() ?? String(index))
    : String(index);
  const isOpus = p.tier === 'opus';
  const isPhase0 = p.id === 'existing_symbols_inventory';
  const tag = isPhase0
    ? `PHASE ${tagPrefix} · v4.2`
    : isOpus
    ? `PHASE ${tagPrefix} · OPUS`
    : `PHASE ${tagPrefix}`;
  const subtitle = p.subtitle
    ?? (p.tier === 'engine' ? 'engine · no LLM'
      : isOpus ? 'orchemist-adversary'
      : 'general-purpose');
  return { tag, title, subtitle, highlight };
}

export default function SkillsPackModePage() {
  // #888 — when engine is reachable, hydrate from `/api/v1/phases`; when
  // unreachable, the page never renders (EngineOfflineGuard short-circuits
  // at the layout level). If the engine is up but the phases endpoint
  // 404s, the cards list is empty — the SectionCard's empty-state copy
  // surfaces that as "phases endpoint unavailable" so the operator can
  // distinguish "engine wedged" from "feature not yet implemented".
  const { data } = useApi<PhasesResponse>(() => listPhases(), []);
  const cards: readonly PhaseCard[] = data
    ? derivePhaseDefs(data.phases).map((p, i) => asCard(p, i))
    : [];
  return (
    <HarnessShell
      title="Track A · run the pipeline locally inside Claude Code"
      screenIndex={6}
      breadcrumb={[{ label: 'Fleet', href: '/' }, { label: 'Skills Pack Mode' }]}
      actions={
        <>
          <button type="button" className="h-button h-button-primary">/orchemist:run</button>
          <button type="button" className="h-button">Update pack</button>
        </>
      }
    >
      {/* Install state banner */}
      <SectionCard
        title="Skills pack installed · v4.2 pipeline · 2026-05-24"
        subtitle={
          <span>
            ~/.claude/skills/orchemist-* · 3 subagents · 2 pipelines · last updated locally 19:12 UTC
            <br />
            <a href="https://github.com/ToscanAI/orchemist-skills" className="h-link">github.com/ToscanAI/orchemist-skills</a> · public · MIT · 0 ★ (alpha) · language-agnostic (python, typescript, go, ...)
          </span>
        }
        tone="success"
        testId="section-install-state"
        action={
          <div className="flex flex-col gap-2">
            <a className="h-button" href="https://github.com/ToscanAI/orchemist-skills" target="_blank" rel="noreferrer">Open in github.com ↗</a>
            <button type="button" className="h-button h-button-primary">Reinstall · ./install.sh</button>
          </div>
        }
      >
        <div className="flex items-center gap-4">
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-[#1B2A1F] border border-harness-teal text-3xl font-extrabold text-harness-teal">
            ✓
          </div>
          <div className="flex-1">
            <div className="text-[12px] text-harness-muted">
              The skills pack ships every phase as pure markdown — runs inside Claude Code with no daemon, no API key, no Python runtime.
              Default execution language is detected from the template&apos;s <code className="text-harness-text font-mono">language</code> config field;
              the engine supports Python (pytest), TypeScript/JS (jest), and Go (<code className="text-harness-text font-mono">go test</code>) today,
              with unknown languages falling back to Python (see <a href="https://github.com/ToscanAI/orchemist-skills/blob/main/README.md" className="h-link">README §Status</a>).
            </div>
          </div>
        </div>
      </SectionCard>

      <section className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <SectionCard
            title="Phase skills (10) · invoked via /orchemist:&lt;phase&gt;"
            subtitle={<span>each skill delegates to a fresh subagent per <em>feedback_fresh_subagent_per_phase</em></span>}
          >
            {cards.length === 0 ? (
              <div className="text-[12px] text-harness-muted py-4">
                phases endpoint unavailable · /api/v1/phases returned no data
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
                {cards.map((p) => (
                  <div
                    key={p.title}
                    className={[
                      'rounded-md border p-3',
                      p.highlight === 'phase0' ? 'border-harness-teal' :
                      p.highlight === 'opus' ? 'border-harness-purple' :
                      'border-harness-border',
                      p.highlight === 'phase0' ? 'bg-[#1B2A1F]' :
                      p.highlight === 'opus' ? 'bg-[#1F1F2E]' :
                      p.highlight === 'engine' ? 'bg-[#161A21]' :
                      'bg-harness-surface2',
                    ].join(' ')}
                    data-testid={`phase-${p.title}`}
                  >
                    <div className={[
                      'text-[10px] tracking-widest font-semibold',
                      p.highlight === 'phase0' ? 'text-harness-teal' :
                      p.highlight === 'opus' ? 'text-harness-purple' :
                      'text-harness-muted',
                    ].join(' ')}>{p.tag}</div>
                    <div className="mt-1 text-[12px] font-bold text-harness-text">{p.title}</div>
                    <div className="mt-0.5 text-[10px] text-harness-muted">{p.subtitle}</div>
                  </div>
                ))}
              </div>
            )}

            <div className="mt-4 rounded-md border border-dashed border-harness-purple p-3">
              <div className="h-section-label text-harness-purple">/orchemist:run</div>
              <div className="mt-1 text-[12px] text-harness-text">
                orchestrator · drives the YAML state machine end-to-end · persists state to .orchemist/runs/&lt;id&gt;/state.json · resumable
              </div>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-3 text-[11px]">
              <div className="rounded-md border border-harness-border bg-harness-surface2 p-3">
                <div className="h-section-label">PIPELINE YAML · v4.2</div>
                <div className="mt-1 text-[12px] font-bold text-harness-text">coding-pipeline-standard.yaml (1317 lines)</div>
              </div>
              <div className="rounded-md border border-harness-border bg-harness-surface2 p-3">
                <div className="h-section-label">PIPELINE YAML · v4.2</div>
                <div className="mt-1 text-[12px] font-bold text-harness-text">coding-pipeline-skip-spec.yaml (Phase-0 absent)</div>
              </div>
            </div>
          </SectionCard>
        </div>

        <div className="lg:col-span-4">
          <SectionCard
            title="Local run history"
            subtitle={<span>.orchemist/runs/ · cwd: ~/ToscanWorkspace/orchemist</span>}
          >
            <ul className="space-y-3 text-[11px]">
              {[
                ['b90a3719', 'parseDuration · 12/12 ✓', 'today', 'success'],
                ['a4f2c0c1', 'CompanyPostEditor · 32/32 ✓', '4d ago', 'success'],
                ['5e1b39d3', 'N5-1 fixture · 18/18 ✓', '11d ago', 'success'],
                ['3b50b5bf', 'spec exhausted at R3', '43d ago', 'warn'],
              ].map(([id, summary, when, tone]) => (
                <li key={String(id)} className="flex items-start gap-3">
                  <div className={[
                    'font-bold',
                    tone === 'warn' ? 'text-harness-warning' : 'text-harness-text',
                  ].join(' ')}>{id}</div>
                  <div className="flex-1">
                    <div className="text-[11px] text-harness-text">{summary}</div>
                  </div>
                  <span className={[
                    'text-[10px]',
                    tone === 'warn' ? 'text-harness-muted' : when === 'today' ? 'text-harness-teal' : 'text-harness-muted',
                  ].join(' ')}>{when}</span>
                </li>
              ))}
            </ul>

            <div className="mt-4 h-section-label">PROMOTE TO REMOTE</div>
            <button type="button" className="mt-2 w-full rounded-md border border-dashed border-harness-purple py-2 text-[11px] font-semibold text-harness-purple hover:bg-[#22321F]">
              Replay this run via remote engine
            </button>
            <div className="mt-2 text-[10px] text-harness-dim">→ writes PR + lands trust calibration update</div>
          </SectionCard>
        </div>
      </section>

      <SectionCard
        title="What you see when you run /orchemist:run examples/example-issue.md"
        className="mt-4"
      >
        <pre className="font-mono text-[11px] leading-relaxed text-harness-muted bg-[#0A0C0F] rounded-md border border-harness-border p-4 overflow-x-auto">
{`$ claude
> /orchemist:run examples/example-issue.md

orchemist · v4.2 · standard pipeline
→ creating run b90a3719 at .orchemist/runs/2026-05-24-b90a3719/

[0]   existing_symbols_inventory          ✓   41 symbols · 18s · $0.04
[1a]  spec                                ✓   1 EXTEND · 2 NEW-OK · 1m 02s · $0.18
[1b]  behavioral                          ✓   7 contracts · 48s · $0.12
[1c]  spec_adversary R2 (opus)            ✓ APPROVE   jaccard 0.93 · $0.62
[3]   implement                           running…    23/100 tool calls · $0.26`}
        </pre>
      </SectionCard>
    </HarnessShell>
  );
}
