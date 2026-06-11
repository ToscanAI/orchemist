'use client';

/**
 * Admin / Activation Console — screen 5 of the Orchemist Harness.
 *
 * Canonical mockup: docs/harness-redesign-2026-05-24/screens/05-admin-activation.svg
 *
 * Six sections:
 *   1. Autonomy ramp (L3 → L4 → L4.3 → L5) — visual + unlock checklist
 *   2. Modes & providers — toggles for openrouter / standalone / openclaw / dry-run
 *   3. Branch protection — read-only audit of all four repos
 *   4. Kill switches — instant stop, no redeploy
 *   5. Webhook triggers — CRUD-shaped list (read-only here; full CRUD lives in /admin/triggers)
 *   6. Feature flags — v4.2 phase0_hard_gate, EXTEND verdict, dialogue phase, cross-repo
 *
 * Per the AUTONOMY.md decision recorded 2026-05-24: branch protection is
 * intentionally NOT enabled on any repo. The status panel reflects that and
 * exposes the proposed `gh api` script as copy-friendly text rather than an
 * "Enable" action.
 */

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { HarnessShell } from '@/components/harness/HarnessShell';
import { SectionCard } from '@/components/harness/SectionCard';
import { AutonomyRamp } from '@/components/harness/AutonomyRamp';
import { getAdminState, updateAdminFeatureFlags } from '@/lib/api';
import type { AdminState } from '@/lib/api';

interface ToggleProps {
  readonly label: string;
  readonly sublabel?: string;
  readonly value: boolean;
  readonly tone?: 'on' | 'off' | 'dev';
  readonly onToggle?: (next: boolean) => void;
  readonly testId?: string;
}

function Toggle({ label, sublabel, value, tone = value ? 'on' : 'off', onToggle, testId }: ToggleProps) {
  const trackBg = tone === 'on' ? 'bg-harness-teal' : tone === 'dev' ? 'bg-[#1F1F2E] border border-harness-purple' : 'bg-[#3B2E1F] border border-harness-warning';
  const knobLeft = tone === 'on' ? 'right-1' : 'left-1';
  const labelColor = tone === 'on' ? 'text-[#0B0D10]' : tone === 'dev' ? 'text-harness-purple' : 'text-harness-warning';
  return (
    <div className="flex items-center gap-3">
      <div className="flex-1">
        <div className="text-[12px] font-semibold text-harness-text">{label}</div>
        {sublabel && <div className="text-[10px] text-harness-dim">{sublabel}</div>}
      </div>
      <button
        type="button"
        className={['relative h-5 w-12 rounded-full transition-colors', trackBg].join(' ')}
        onClick={() => onToggle?.(!value)}
        data-testid={testId}
        aria-pressed={value}
      >
        <span
          className={['absolute top-0.5 h-4 w-4 rounded-full bg-[#0B0D10]', knobLeft].join(' ')}
        />
        <span className={['absolute top-1/2 -translate-y-1/2 text-[9px] font-bold', labelColor, tone === 'on' ? 'left-2' : 'right-2'].join(' ')}>
          {tone === 'on' ? 'ON' : tone === 'dev' ? 'DEV' : 'OFF'}
        </span>
      </button>
    </div>
  );
}

export default function AdminActivationPage() {
  // Kill switches + autonomy ramp UI live in local state — these don't have
  // backend write endpoints today (they're not yet honoured by the runtime;
  // separate epic). Modes are also local: provider availability is governed
  // by env vars at server boot, not by runtime toggles.
  const [autoMerge, setAutoMerge] = useState(true);
  const [issueSpawn, setIssueSpawn] = useState(true);
  const [regressionAutoFix, setRegressionAutoFix] = useState(true);
  const [skillsMode, setSkillsMode] = useState(true);

  // Feature flags + modes hydrate from /api/v1/admin/state on mount.
  // `engineUp === null` is loading; `false` means we fell back to local-only
  // state (offline / endpoint missing).
  const [adminState, setAdminState] = useState<AdminState | null>(null);
  const [engineUp, setEngineUp] = useState<boolean | null>(null);
  const [flagBusy, setFlagBusy] = useState<string | null>(null);
  const [flagError, setFlagError] = useState<string | null>(null);

  // Modes — initial defaults; replaced from adminState once it loads.
  const [openrouter, setOpenrouter] = useState(true);
  const [standalone, setStandalone] = useState(true);
  const [openclaw, setOpenclaw] = useState(false);
  const [dryRun, setDryRun] = useState(true);

  const [phase0Hard, setPhase0Hard] = useState(false);
  const [extendVerdict, setExtendVerdict] = useState(true);
  const [crossRepo, setCrossRepo] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getAdminState()
      .then((s) => {
        if (cancelled) return;
        setAdminState(s);
        setEngineUp(true);
        setOpenrouter(s.modes.openrouter);
        setStandalone(s.modes.standalone);
        setOpenclaw(s.modes.openclaw);
        setDryRun(s.modes.dry_run);
        setPhase0Hard(s.feature_flags.phase0_hard_gate);
        setExtendVerdict(s.feature_flags.extend_verdict);
        setCrossRepo(s.feature_flags.cross_repo);
      })
      .catch(() => { if (!cancelled) setEngineUp(false); });
    return () => { cancelled = true; };
  }, []);

  // Wrap each feature-flag toggle so the PUT goes to the engine and the
  // optimistic update reverts on failure.
  async function persistFlag(
    key: 'phase0_hard_gate' | 'extend_verdict' | 'cross_repo',
    next: boolean,
    localSetter: (v: boolean) => void,
  ) {
    localSetter(next);
    if (engineUp !== true) return; // local-only mode
    setFlagBusy(key);
    setFlagError(null);
    try {
      await updateAdminFeatureFlags({ [key]: next });
    } catch (e) {
      // Revert on failure
      localSetter(!next);
      setFlagError(e instanceof Error ? e.message : `Failed to persist ${key}`);
    } finally {
      setFlagBusy(null);
    }
  }

  return (
    <HarnessShell
      title="Activation console · what's on, what's gated, what's off"
      screenIndex={5}
      breadcrumb={[{ label: 'Fleet', href: '/' }, { label: 'Admin / Activation' }]}
      actions={
        <>
          <span className="h-pill h-pill-danger text-[11px]">
            <span className="inline-block h-2 w-2 rounded-full bg-harness-danger" />
            ADMIN · destructive ops
          </span>
          <button type="button" className="h-button">Show audit log</button>
        </>
      }
    >
      {/* Section 1: Autonomy ramp */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <SectionCard
            title={`1 · Autonomy ramp · current Level ${adminState?.autonomy_level ?? '4.3'}`}
            subtitle={<span>L3 = review-all · L4 = auto-merge ≥ threshold · L5 = dark factory (full Tuesday-morning scenario)</span>}
          >
            <AutonomyRamp />
            <div className="mt-4 grid grid-cols-2 gap-3 text-[11px] xl:grid-cols-4">
              <span className="text-harness-muted">To promote to Level 5 ·</span>
              <span className="text-harness-teal">✓ fleet UI shipped</span>
              <span className="text-harness-warning">○ stale detection (3.5)</span>
              <span className="text-harness-warning">○ multi-repo (4.6 / Sprint 12)</span>
            </div>
          </SectionCard>
        </div>

        {/* Section 2: Modes & providers */}
        <div className="lg:col-span-4">
          <SectionCard title="2 · Modes &amp; providers" subtitle={<span>model-agnostic by design</span>}>
            <div className="flex flex-col gap-3">
              <Toggle label="openrouter" value={openrouter} onToggle={setOpenrouter} testId="toggle-openrouter" />
              <Toggle label="standalone (Anthropic)" value={standalone} onToggle={setStandalone} testId="toggle-standalone" />
              <Toggle label="openclaw gateway" value={openclaw} tone="off" onToggle={setOpenclaw} testId="toggle-openclaw" />
              <Toggle label="dry-run" value={dryRun} onToggle={setDryRun} testId="toggle-dryrun" />
            </div>
            <div className="mt-3 text-[10px] text-harness-dim">
              Every model provider that exposes a tool-calling chat-completions API is supported. Default model tier per phase is configured in the template YAML, not here — this surface controls availability, not selection.
            </div>
          </SectionCard>
        </div>
      </section>

      {/* Section 3: Branch protection (READ-ONLY status) */}
      <section className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <SectionCard
            title="3 · Branch protection · intentionally off (per René 2026-05-24)"
            subtitle={<span className="text-harness-warning">All 4 repos currently unprotected on main. See <Link href="https://github.com/ToscanAI/orchemist/blob/main/docs/harness-redesign-2026-05-24/AUTONOMY.md" className="h-link">AUTONOMY.md</Link> · issue <a href="https://github.com/ToscanAI/orchemist/issues/812" className="h-link">#812</a> closed.</span>}
            tone="warning"
          >
            <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-[12px] border-collapse">
              <thead>
                <tr className="text-[10px] tracking-widest text-harness-dim">
                  <th className="text-left pb-2">REPO</th>
                  <th className="text-left pb-2">REQUIRED REVIEWS</th>
                  <th className="text-left pb-2">REQUIRED STATUS</th>
                  <th className="text-left pb-2">FORCE-PUSH BLOCK</th>
                </tr>
              </thead>
              <tbody className="border-t border-harness-border">
                {[
                  ['orchemist', '— off', '— off', '— off'],
                  ['orchemist-skills', '— off', '— off', '— off'],
                  ['orchemist-website', '— off', '— off', '— off'],
                  ['orchemist-ide', 'deprecated', '—', '—'],
                ].map(([repo, reviews, status, fp]) => (
                  <tr key={repo} className="border-b border-harness-border">
                    <td className="py-2 text-harness-text">{repo}</td>
                    <td className={['py-2', reviews === 'deprecated' ? 'text-harness-dim line-through' : 'text-harness-danger'].join(' ')}>{reviews}</td>
                    <td className={['py-2', status === '—' ? 'text-harness-dim' : 'text-harness-danger'].join(' ')}>{status}</td>
                    <td className={['py-2', fp === '—' ? 'text-harness-dim' : 'text-harness-danger'].join(' ')}>{fp}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            <div className="mt-3 text-[10px] text-harness-dim">
              Engine git_integration.py already refuses force-push internally · branch protection (defence-in-depth at GitHub layer) is intentionally not enabled.
            </div>
          </SectionCard>
        </div>

        {/* Section 4: Kill switches */}
        <div className="lg:col-span-4">
          <SectionCard title="4 · Kill switches" subtitle={<span>instant stop · do not require redeploy</span>}>
            <div className="flex flex-col gap-4">
              <Toggle label="Auto-merge globally" value={autoMerge} onToggle={setAutoMerge} testId="kill-automerge" />
              <Toggle label="Issue → pipeline auto-spawn" value={issueSpawn} onToggle={setIssueSpawn} testId="kill-spawn" />
              <Toggle label="Regression auto-fix" value={regressionAutoFix} onToggle={setRegressionAutoFix} testId="kill-regression" />
              <Toggle label="Skills pack mode" value={skillsMode} onToggle={setSkillsMode} testId="kill-skills" />
            </div>
            <button
              type="button"
              className="mt-4 w-full h-button h-button-danger font-bold"
              data-testid="panic-button"
            >
              PANIC · stop all pipelines
            </button>
          </SectionCard>
        </div>
      </section>

      {/* Section 5: Webhook triggers */}
      <section className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <SectionCard
            title="5 · Webhook triggers · 4 registered"
            subtitle={<span>events that auto-spawn pipelines · per-trigger rate limits enforced (webhooks.py)</span>}
          >
            <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-[12px] border-collapse">
              <thead>
                <tr className="text-[10px] tracking-widest text-harness-dim">
                  <th className="text-left pb-2">TRIGGER</th>
                  <th className="text-left pb-2">EVENT</th>
                  <th className="text-left pb-2">TEMPLATE</th>
                  <th className="text-left pb-2">RATE</th>
                  <th className="text-right pb-2">ENABLED</th>
                </tr>
              </thead>
              <tbody className="border-t border-harness-border">
                {[
                  ['issue-bug-fix', 'issues.opened · label:bug', 'coding-pipeline-standard', '10/h', true],
                  ['issue-feature', 'issues.opened · label:enhancement', 'coding-pipeline-standard', '6/h', true],
                  ['ci-regression', 'workflow_run.completed · status:failure', 'regression-fix-pipeline', '12/h', true],
                  ['docs-stale', 'schedule.daily 04:00 UTC', 'docs-pipeline-v1', '1/d', false],
                ].map(([name, event, tpl, rate, on]) => (
                  <tr key={String(name)} className="border-b border-harness-border">
                    <td className="py-2 text-harness-text font-semibold">{name}</td>
                    <td className="py-2 text-harness-muted">{event}</td>
                    <td className="py-2 text-harness-muted">{tpl}</td>
                    <td className="py-2 text-harness-muted">{rate}</td>
                    <td className="py-2 text-right">
                      <span className={[
                        'inline-block h-5 w-10 rounded-full',
                        on ? 'bg-harness-teal' : 'bg-[#3B2E1F] border border-harness-warning',
                      ].join(' ')} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            <button
              type="button"
              className="mt-4 w-full rounded-md border border-dashed border-harness-purple py-2 text-[11px] text-harness-purple hover:bg-[#22321F]"
            >
              + Add trigger · GitHub event · scheduled · custom HTTP
            </button>
          </SectionCard>
        </div>

        {/* Section 6: Feature flags */}
        <div className="lg:col-span-4">
          <SectionCard
            title="6 · Feature flags · v4.2"
            subtitle={
              adminState
                ? <span>persisted to {adminState.source === 'file' ? adminState.path : 'in-memory defaults'} · live</span>
                : engineUp === false
                  ? <span className="text-harness-warning">engine offline · toggles local-only</span>
                  : <span>pipeline structural toggles · loading…</span>
            }
          >
            {flagError && (
              <div className="mb-3 text-[11px] text-harness-danger">{flagError}</div>
            )}
            <div className="flex flex-col gap-4">
              <Toggle
                label={`phase0_hard_gate${flagBusy === 'phase0_hard_gate' ? ' ·' : ''}`}
                sublabel="HALT on missing inventory"
                value={phase0Hard}
                tone={phase0Hard ? 'on' : 'off'}
                onToggle={(next) => persistFlag('phase0_hard_gate', next, setPhase0Hard)}
                testId="flag-phase0"
              />
              <Toggle
                label={`EXTEND verdict${flagBusy === 'extend_verdict' ? ' ·' : ''}`}
                sublabel="v4.2 four-verdict schema"
                value={extendVerdict}
                onToggle={(next) => persistFlag('extend_verdict', next, setExtendVerdict)}
                testId="flag-extend"
              />
              <Toggle
                label="dialogue phase (#808)"
                sublabel="Gemini reviewer prototype · not yet persisted"
                value={true}
                tone="dev"
                testId="flag-dialogue"
              />
              <Toggle
                label={`cross-repo orchestration${flagBusy === 'cross_repo' ? ' ·' : ''}`}
                sublabel="Sprint 12 · #563-#567"
                value={crossRepo}
                tone={crossRepo ? 'on' : 'off'}
                onToggle={(next) => persistFlag('cross_repo', next, setCrossRepo)}
                testId="flag-crossrepo"
              />
            </div>
          </SectionCard>
        </div>
      </section>
    </HarnessShell>
  );
}
