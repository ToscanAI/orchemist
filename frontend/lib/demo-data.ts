/**
 * Demo / fallback data used when the engine API is unreachable.
 *
 * The harness must NEVER render a blank or broken screen — when the engine
 * is offline we show plausibly-shaped sample data with a clear "engine
 * offline" affordance in the BottomStrip. This module is the single source
 * of those samples so every screen tells the same story.
 *
 * All values are deliberately tied to the 2026-05-24 investigation pack
 * (issue numbers, repos, phase names) so they read as continuous with the
 * SVG mockups.
 */

import type { RunRecord, RunStatus } from './types';

export interface DemoActiveRun {
  readonly run: RunRecord;
  readonly totalPhases: number;
  readonly confidence: number;
  readonly modelTier: string;
  readonly costUsd: number;
  readonly etaLabel: string;
}

function mkRun(overrides: Partial<RunRecord> & Pick<RunRecord, 'run_id' | 'template_id' | 'current_phase'>): RunRecord {
  return {
    run_id: overrides.run_id,
    template_id: overrides.template_id,
    template_path: `~/.orchemist/templates/${overrides.template_id}.yaml`,
    mode: overrides.mode ?? 'openrouter',
    status: (overrides.status ?? 'running') as RunStatus,
    current_phase: overrides.current_phase,
    completed_phases: overrides.completed_phases ?? [],
    pid: 12345,
    output_dir: `.orchemist/runs/${overrides.run_id}`,
    error_message: null,
    gateway_url: null,
    skip_scoring: false,
    scoring_status: null,
    scoring_score: null,
    started_at: '2026-05-24T18:00:00Z',
    completed_at: null,
    created_at: '2026-05-24T18:00:00Z',
  };
}

export const DEMO_ACTIVE_RUNS: readonly DemoActiveRun[] = [
  {
    run: mkRun({
      run_id: 'b90a3719-orchemist-802',
      template_id: 'coding-pipeline-standard',
      current_phase: 'acceptance_test',
      completed_phases: ['existing_symbols_inventory', 'spec', 'behavioral', 'spec_adversary', 'acceptance_test_adversary'],
    }),
    totalPhases: 10,
    confidence: 0.91,
    modelTier: 'opus',
    costUsd: 1.82,
    etaLabel: '~6 min',
  },
  {
    run: mkRun({
      run_id: 'a4f2c0c1-orchemist-799',
      template_id: 'coding-pipeline-standard',
      current_phase: 'spec_adversary',
      completed_phases: ['existing_symbols_inventory', 'spec', 'behavioral'],
    }),
    totalPhases: 10,
    confidence: 0.62,
    modelTier: 'opus',
    costUsd: 3.40,
    etaLabel: '↑ R3?',
  },
  {
    run: mkRun({
      run_id: '5e1b39d3-orchemist-776',
      template_id: 'coding-pipeline-standard',
      current_phase: 'implement',
      completed_phases: ['existing_symbols_inventory', 'spec', 'behavioral', 'spec_adversary', 'acceptance_test', 'acceptance_test_adversary'],
    }),
    totalPhases: 10,
    confidence: 0.88,
    modelTier: 'sonnet',
    costUsd: 0.64,
    etaLabel: '~2 min',
  },
  {
    run: mkRun({
      run_id: 'c3d7b2e0-skills-local',
      template_id: 'coding-pipeline-standard',
      current_phase: 'existing_symbols_inventory',
      completed_phases: [],
    }),
    totalPhases: 10,
    confidence: 0.5,
    modelTier: 'sonnet',
    costUsd: 0.04,
    etaLabel: '~12 min',
  },
];

export interface DemoRegression {
  readonly repo: string;
  readonly branch: string;
  readonly summary: string;
  readonly sinceCommit: string;
  readonly hoursAgo: number;
  readonly prUrl?: string;
  readonly retryStatus?: string;
}

export const DEMO_REGRESSIONS: readonly DemoRegression[] = [
  {
    repo: 'orchemist',
    branch: 'CI · main',
    summary: 'test_postmortem_spec_routing failed since 7253d9a',
    sinceCommit: '7253d9a',
    hoursAgo: 4,
    prUrl: 'https://github.com/ToscanAI/orchemist/pull/810',
  },
  {
    repo: 'orchemist-website',
    branch: 'build',
    summary: 'image regen step 502 from Vercel · pipeline retrying',
    sinceCommit: '',
    hoursAgo: 0.5,
    retryStatus: 'retry 2/3',
  },
];

export interface DemoStaleFinding {
  readonly severity: 'warn' | 'info';
  readonly summary: string;
  readonly hint: string;
}

export const DEMO_STALE: readonly DemoStaleFinding[] = [
  {
    severity: 'warn',
    summary: 'orchemist · docs/openrouter-setup.md references stale env var',
    hint: 'draft fix PR ready · /orchemist:run --dry-run',
  },
  {
    severity: 'warn',
    summary: 'orchemist · 5 dependencies have CVE patches available',
    hint: 'cycle through deps-pipeline-v1 · ETA 12 min',
  },
  {
    severity: 'info',
    summary: 'orchemist-skills · v4.2 changelog references future v5 · clarify',
    hint: 'human review · ambiguous priority',
  },
];

export interface DemoGate {
  readonly repo: string;
  readonly issueNumber: string;
  readonly issueTitle: string;
  readonly template: string;
  readonly confidence: number;
  readonly threshold: number;
  readonly waitingLabel: string;
  readonly waitingTone: 'warning' | 'danger' | 'neutral';
}

export const DEMO_GATES: readonly DemoGate[] = [
  {
    repo: 'orchemist',
    issueNumber: '#799',
    issueTitle: 'verdict extraction format',
    template: 'coding-pipeline-standard',
    confidence: 0.74,
    threshold: 0.90,
    waitingLabel: '2h 14m',
    waitingTone: 'danger',
  },
  {
    repo: 'project-dashboard',
    issueNumber: '#12',
    issueTitle: 'CSV export truncates rows > 10k',
    template: 'coding-pipeline-standard',
    confidence: 0.86,
    threshold: 0.90,
    waitingLabel: '48m',
    waitingTone: 'warning',
  },
  {
    repo: 'orchemist',
    issueNumber: '#735',
    issueTitle: 'content pipeline timeout race',
    template: 'coding-pipeline-skip-spec',
    confidence: 0.81,
    threshold: 0.90,
    waitingLabel: '34m',
    waitingTone: 'warning',
  },
  {
    repo: 'orchemist-website',
    issueNumber: 'build',
    issueTitle: 'image regen step retried twice',
    template: 'deps-pipeline-v1',
    confidence: 0.69,
    threshold: 0.90,
    waitingLabel: '22m',
    waitingTone: 'warning',
  },
  {
    repo: 'orchemist',
    issueNumber: '#806',
    issueTitle: 'skills pack pivot follow-ups',
    template: 'coding-pipeline-standard',
    confidence: 0.92,
    threshold: 0.90,
    waitingLabel: '11m',
    waitingTone: 'neutral',
  },
];

export interface DemoTrustProfile {
  readonly key: string;
  readonly confidence: number;
  readonly threshold: number;
  readonly verdict: 'auto' | 'hold' | 'review-all';
}

export const DEMO_TRUST_PROFILES: readonly DemoTrustProfile[] = [
  { key: 'orchemist · coding · feature', confidence: 0.90, threshold: 0.90, verdict: 'auto' },
  { key: 'orchemist · coding · bug', confidence: 0.93, threshold: 0.90, verdict: 'auto' },
  { key: 'orchemist-website · content', confidence: 0.68, threshold: 0.85, verdict: 'hold' },
  { key: 'project-dashboard · coding', confidence: 0.81, threshold: 0.90, verdict: 'hold' },
  { key: 'orchemist · docs', confidence: 0.96, threshold: 0.80, verdict: 'auto' },
  { key: 'orchemist · refactor', confidence: 0.15, threshold: 0.90, verdict: 'review-all' },
];

export interface DemoDecision {
  readonly verdict: 'approve' | 'reject' | 'auto';
  readonly summary: string;
  readonly when: string;
}

export const DEMO_DECISIONS: readonly DemoDecision[] = [
  { verdict: 'approve', summary: 'orchemist#797 · openrouter MAX_TOOL_ITERATIONS bump', when: '18 min ago' },
  { verdict: 'approve', summary: 'orchemist#796 · sandbox_roots at 5 sites', when: '1h 4m ago' },
  { verdict: 'reject',  summary: 'orchemist#624 · docs-pipeline title bug · spec too vague', when: '3h 12m ago' },
  { verdict: 'auto',    summary: 'orchemist#791 · GROUND TRUTH skip-spec · 0.94', when: 'yesterday 14:33' },
  { verdict: 'auto',    summary: 'orchemist#790 · GROUND TRUTH at/imp/rev/fix · 0.92', when: 'yesterday 13:11' },
  { verdict: 'auto',    summary: 'orchemist#789 · GROUND TRUTH spec_adversary · 0.91', when: 'yesterday 12:48' },
];
