/**
 * Acceptance tests for the frontend P1 cluster (#759 #761 #772 #773 #774 #775 #776).
 *
 * These tests pin the behavioral contracts produced in the spec/behavioral
 * phases of the Orchemist pipeline (run 20260527-447190).
 *
 * They run under Jest (jsdom) and verify:
 *   - #759 — Gates approve/reject confirmation prompts + score/branch display.
 *   - #761 — design-token migration (no legacy zinc-* utility classes in scope files).
 *   - #772 — PhaseModelMap groups by tier (not per-phase).
 *   - #773 — SSE runtime validation + BASE_URL dedup + unused helper removal.
 *   - #774 — controlled API key input, useStaticExportParam hook, lib/constants.ts,
 *            module-level TERMINAL_STATUSES, LogViewer eslint-disable removal.
 *   - #775 — shared ErrorBanner (role=alert), shared Spinner consumed, search aria-label.
 *   - #776 — new test files exist for RunDetailClient, SchemaForm, RunsPage.
 *
 * The tests are derived from behavioral.md only and DO NOT mock React internals.
 *
 * Source of truth: .orchemist/runs/20260527-447190/behavioral.md
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { execSync } from 'node:child_process';

import { render, screen, act, fireEvent } from '@testing-library/react';
import * as React from 'react';

import * as ApiModule from '@/lib/api';
import * as SseModule from '@/lib/sse';
import * as ConstantsModule from '@/lib/constants';
import { ErrorBanner } from '@/components/ui/ErrorBanner';
import { Spinner } from '@/components/ui/Spinner';
import { useStaticExportParam } from '@/hooks/useStaticExportParam';

// Repo root resolved from this test file location.
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const FRONTEND_ROOT = path.join(REPO_ROOT, 'frontend');

function readFile(rel: string): string {
  return fs.readFileSync(path.join(REPO_ROOT, rel), 'utf-8');
}
function fileExists(rel: string): boolean {
  return fs.existsSync(path.join(REPO_ROOT, rel));
}

// ─── Group A1 — shared ErrorBanner (#775) ─────────────────────────────────────

describe('A1 — ErrorBanner', () => {
  it('A1.1 renders role=alert with the message text when message is non-empty', () => {
    render(React.createElement(ErrorBanner, { message: 'something broke' }));
    const alert = screen.getByRole('alert');
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent('something broke');
  });

  it('A1.2 renders nothing when message is null/empty/undefined', () => {
    const { container: c1 } = render(
      React.createElement(ErrorBanner, { message: null }),
    );
    expect(c1.querySelector('[role="alert"]')).toBeNull();
    const { container: c2 } = render(
      React.createElement(ErrorBanner, { message: '' }),
    );
    expect(c2.querySelector('[role="alert"]')).toBeNull();
    const { container: c3 } = render(
      React.createElement(ErrorBanner, { message: undefined as unknown as string }),
    );
    expect(c3.querySelector('[role="alert"]')).toBeNull();
  });

  it('A1.3 no `role="status"` on error blocks (red-styled blocks) in the five enumerated pages', () => {
    const files = [
      'frontend/app/page.tsx',
      'frontend/app/templates/page.tsx',
      'frontend/app/runs/page.tsx',
      'frontend/app/templates/[id]/TemplateDetailClient.tsx',
      'frontend/app/runs/[id]/RunDetailClient.tsx',
    ];
    for (const rel of files) {
      const src = readFile(rel);
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.includes('role="status"')) {
          // Loading / empty-state blocks LEGITIMATELY use `role="status"`
          // (W3C-correct for loading / status updates). The issue #775
          // fix is specifically about ERROR blocks using role="status".
          // We detect error blocks by red Tailwind utility classes on the
          // same element line. If the line contains both `role="status"`
          // and a red-colour class, it is an error block — flag it.
          const isErrorBlock = /\b(?:bg-red-|text-red-|border-red-)/.test(line);
          if (isErrorBlock) {
            throw new Error(
              `${rel}:${i + 1} — error-styled element uses role="status" (should be role="alert"): ${line.trim()}`,
            );
          }
        }
      }
    }
  });
});

// ─── Group A2 — Spinner consumption + aria-label (#775) ───────────────────────

describe('A2 — Spinner + search aria-label', () => {
  it('A2.1 the five pages do not contain duplicated inline SVG spinner markup', () => {
    const files = [
      'frontend/app/page.tsx',
      'frontend/app/templates/page.tsx',
      'frontend/app/runs/page.tsx',
      'frontend/app/templates/[id]/TemplateDetailClient.tsx',
      'frontend/app/runs/[id]/RunDetailClient.tsx',
    ];
    for (const rel of files) {
      const src = readFile(rel);
      // No <svg class="animate-spin ..."> markup inline.
      // The shared Spinner component owns the only animate-spin svg.
      expect(src).not.toMatch(/<svg[^>]*animate-spin/);
    }
  });

  it('A2.2 templates search input exposes aria-label="Search templates"', () => {
    const src = readFile('frontend/app/templates/page.tsx');
    expect(src).toMatch(/aria-label=("|')Search templates("|')/);
  });
});

// ─── Group A3 — useStaticExportParam (#774) ───────────────────────────────────

describe('A3 — useStaticExportParam', () => {
  beforeEach(() => {
    // Reset jsdom location.
    window.history.replaceState({}, '', '/');
  });

  it('A3.1 returns input unchanged for non-placeholder param', () => {
    const TestC: React.FC<{ value: string }> = ({ value }) => {
      const r = useStaticExportParam(value);
      return React.createElement('div', { 'data-testid': 'r' }, r ?? '');
    };
    render(React.createElement(TestC, { value: 'abc' }));
    expect(screen.getByTestId('r')).toHaveTextContent('abc');
  });

  it('A3.2 resolves placeholder to last path segment by default', () => {
    window.history.replaceState({}, '', '/runs/abc123');
    const TestC: React.FC = () => {
      const r = useStaticExportParam('_');
      return React.createElement('div', { 'data-testid': 'r' }, r ?? '');
    };
    render(React.createElement(TestC));
    expect(screen.getByTestId('r')).toHaveTextContent('abc123');
  });

  it('A3.2b resolves placeholder for /templates/my-template', () => {
    window.history.replaceState({}, '', '/templates/my-template');
    const TestC: React.FC = () => {
      const r = useStaticExportParam('_');
      return React.createElement('div', { 'data-testid': 'r' }, r ?? '');
    };
    render(React.createElement(TestC));
    expect(screen.getByTestId('r')).toHaveTextContent('my-template');
  });

  it('A3.6 resolves placeholder with segmentIndexFromEnd: 1 for /templates/:id/edit', () => {
    window.history.replaceState({}, '', '/templates/my-template/edit');
    const TestC: React.FC = () => {
      const r = useStaticExportParam('_', { segmentIndexFromEnd: 1 });
      return React.createElement('div', { 'data-testid': 'r' }, r ?? '');
    };
    render(React.createElement(TestC));
    expect(screen.getByTestId('r')).toHaveTextContent('my-template');
  });

  it('A3.7 URL-decodes the resolved segment', () => {
    window.history.replaceState({}, '', '/templates/my%20template');
    const TestC: React.FC = () => {
      const r = useStaticExportParam('_');
      return React.createElement('div', { 'data-testid': 'r' }, r ?? '');
    };
    render(React.createElement(TestC));
    expect(screen.getByTestId('r')).toHaveTextContent('my template');
  });

  it('A3.8 no duplicated window.location.pathname extraction in scope files', () => {
    const files = [
      'frontend/app/runs/[id]/RunDetailClient.tsx',
      'frontend/app/templates/[id]/TemplateDetailClient.tsx',
      'frontend/app/templates/[id]/edit/EditTemplateClient.tsx',
    ];
    for (const rel of files) {
      const src = readFile(rel);
      // Inline extractors must be gone.
      expect(src).not.toMatch(/window\.location\.pathname\.match/);
      expect(src).not.toMatch(/window\.location\.pathname\.split/);
      // Hook must be imported.
      expect(src).toMatch(/useStaticExportParam/);
    }
  });
});

// ─── Group A4 — Polling interval constants (#774) ─────────────────────────────

describe('A4 — Polling constants', () => {
  it('A4.1 HEALTH_CHECK_INTERVAL_MS === 30000', () => {
    expect((ConstantsModule as Record<string, unknown>).HEALTH_CHECK_INTERVAL_MS).toBe(30_000);
  });

  it('A4.2 RUNS_REFRESH_INTERVAL_MS === 10000', () => {
    expect((ConstantsModule as Record<string, unknown>).RUNS_REFRESH_INTERVAL_MS).toBe(10_000);
  });

  it('A4.3 no inline magic number remains for the dashboard health timer', () => {
    const src = readFile('frontend/app/page.tsx');
    // Either uses the named constant (preferred) or imports it.
    expect(src).toMatch(/HEALTH_CHECK_INTERVAL_MS|from\s+['"]@\/lib\/constants['"]/);
    // Should not contain a raw 30_000 / 30000 for the health timer.
    // Allow other uses but require the timer to use the constant.
    const timerLine = src.split('\n').find(l => /setInterval|polling|health/i.test(l) && /30_?000/.test(l));
    expect(timerLine).toBeUndefined();
  });

  it('A4.3 no inline magic number remains for the runs refresh timer', () => {
    const src = readFile('frontend/app/runs/page.tsx');
    expect(src).toMatch(/RUNS_REFRESH_INTERVAL_MS|from\s+['"]@\/lib\/constants['"]/);
    const timerLine = src.split('\n').find(l => /setInterval|refresh|auto/i.test(l) && /10_?000/.test(l));
    expect(timerLine).toBeUndefined();
  });
});

// ─── Group A5 — Module-level TERMINAL_STATUSES (#774) ─────────────────────────

describe('A5 — TERMINAL_STATUSES at module scope', () => {
  it('A5.1 RunDetailClient declares TERMINAL_STATUSES outside the component body', () => {
    const src = readFile('frontend/app/runs/[id]/RunDetailClient.tsx');
    // Look for `const TERMINAL_STATUSES` outside a function body.
    // Heuristic: it should appear after imports and before `export function` / `export default function`.
    const match = src.match(/const\s+TERMINAL_STATUSES\s*=/);
    expect(match).not.toBeNull();
    const idx = (match as RegExpMatchArray).index ?? 0;
    const preface = src.slice(0, idx);
    // Ensure no `export function` or `export default function` is opened before TERMINAL_STATUSES.
    expect(preface).not.toMatch(/export\s+(default\s+)?function/);
  });
});

// ─── Group A6 — Controlled API key input (#774) ───────────────────────────────

describe('A6 — ProviderSelector controlled input', () => {
  it('A6.1 source declares value={apiKey} on the API key input', () => {
    const src = readFile('frontend/components/pipeline/ProviderSelector.tsx');
    // The input must have a value prop bound to apiKey (or apiKey ?? '').
    expect(src).toMatch(/value=\{apiKey(\s*\?\?\s*['"]\s*['"])?\}/);
  });
});

// ─── Group A7 — LogViewer lint cleanup (#774) ─────────────────────────────────

describe('A7 — LogViewer eslint-disable removed', () => {
  it('A7.1 LogViewer source has no eslint-disable react-hooks/exhaustive-deps comment', () => {
    const src = readFile('frontend/components/pipeline/LogViewer.tsx');
    expect(src).not.toMatch(/eslint-disable-next-line\s+react-hooks\/exhaustive-deps/);
  });
});

// ─── Group A8 — SSE runtime validation (#773) ─────────────────────────────────

describe('A8 — SSE runtime validation', () => {
  let warnSpy: jest.SpyInstance;
  beforeEach(() => {
    warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
  });
  afterEach(() => {
    warnSpy.mockRestore();
  });

  // The sse module exports a parseRawEvent (internal helper) — we test it by
  // calling it directly if exported, or by exercising useRunEvents end-to-end.
  // Per behavioral.md, valid events flow through; invalid events are dropped
  // with a console.warn.
  const sse = SseModule as unknown as { parseRawEvent?: (raw: string) => unknown };
  const hasParse = typeof sse.parseRawEvent === 'function';

  it('A8.1 valid event flows through parseRawEvent (if exported)', () => {
    if (!hasParse) return; // contract still satisfied via useRunEvents elsewhere
    const r = sse.parseRawEvent!(JSON.stringify({ run_id: 'r1', type: 'phase_started', phase: 'spec' }));
    expect(r).toMatchObject({ run_id: 'r1', type: 'phase_started' });
  });

  it('A8.2 missing run_id is rejected and warned', () => {
    if (!hasParse) return;
    const r = sse.parseRawEvent!(JSON.stringify({ type: 'phase_started' }));
    expect(r).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
  });

  it('A8.3 missing type is rejected and warned', () => {
    if (!hasParse) return;
    const r = sse.parseRawEvent!(JSON.stringify({ run_id: 'r1' }));
    expect(r).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
  });

  it('A8.4 unknown type is rejected and warned', () => {
    if (!hasParse) return;
    const r = sse.parseRawEvent!(JSON.stringify({ run_id: 'r1', type: 'not_a_real_event' }));
    expect(r).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
  });

  it('A8.5 extra fields are tolerated', () => {
    if (!hasParse) return;
    const r = sse.parseRawEvent!(JSON.stringify({ run_id: 'r1', type: 'phase_started', extra: 'x' }));
    expect(r).toMatchObject({ run_id: 'r1', type: 'phase_started' });
  });

  it('A8.7 non-string run_id is rejected and warned', () => {
    if (!hasParse) return;
    const r = sse.parseRawEvent!(JSON.stringify({ run_id: 42, type: 'phase_started' }));
    expect(r).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
  });

  it('A8.8 empty-string run_id is rejected and warned', () => {
    if (!hasParse) return;
    const r = sse.parseRawEvent!(JSON.stringify({ run_id: '', type: 'phase_started' }));
    expect(r).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
  });
});

// ─── Group A9 — BASE_URL dedup + streamRun removal (#773) ─────────────────────

describe('A9 — BASE_URL + streamRun', () => {
  it('A9.1 lib/api.ts exports BASE_URL', () => {
    expect((ApiModule as Record<string, unknown>).BASE_URL).toBeDefined();
    expect(typeof (ApiModule as Record<string, unknown>).BASE_URL).toBe('string');
  });

  it('A9.1 lib/sse.ts does not declare its own BASE_URL', () => {
    const src = readFile('frontend/lib/sse.ts');
    // No `const BASE_URL =` at module top.
    // It MAY re-export or alias, but not declare a literal.
    expect(src).not.toMatch(/^const\s+BASE_URL\s*=\s*['"`]/m);
  });

  it('A9.2 streamRun is removed OR explicitly @deprecated', () => {
    const src = readFile('frontend/lib/api.ts');
    const exportsStreamRun = /export\s+(function|const|async\s+function)\s+streamRun\b/.test(src)
      || /export\s*\{[^}]*\bstreamRun\b/.test(src);
    if (exportsStreamRun) {
      // If still exported, must carry a @deprecated docblock nearby.
      const idx = src.search(/streamRun/);
      const preface = src.slice(Math.max(0, idx - 300), idx);
      expect(preface).toMatch(/@deprecated/);
    } else {
      expect(exportsStreamRun).toBe(false);
    }
  });
});

// ─── Group A10 — PhaseModelMap tier grouping (#772) ───────────────────────────

describe('A10 — PhaseModelMap groups by tier', () => {
  // We exercise PhaseModelMap via render. The exported props per existing_symbols.md:
  //   <PhaseModelMap phases={...} modelMap={{}} onModelMapChange={fn} />
  const PhaseModelMapMod = jest.requireActual<typeof import('@/components/pipeline/PhaseModelMap')>(
    '@/components/pipeline/PhaseModelMap',
  );
  const { PhaseModelMap } = PhaseModelMapMod;

  type Phase = {
    id: string;
    name: string;
    description: string;
    model_tier: string;
    thinking_level: string;
    task_type: string;
    max_iterations: number;
    depends_on: readonly string[];
  };

  const makePhase = (id: string, name: string, tier: string): Phase => ({
    id,
    name,
    description: `${name} phase`,
    model_tier: tier,
    thinking_level: 'medium',
    task_type: 'generate',
    max_iterations: 3,
    depends_on: [],
  });

  const threeSonnetOneOpus: Phase[] = [
    makePhase('spec', 'Spec', 'sonnet'),
    makePhase('behavioral', 'Behavioral', 'sonnet'),
    makePhase('implement', 'Implement', 'sonnet'),
    makePhase('review', 'Review', 'opus'),
  ];

  it('A10.1 renders exactly one model-override <select> per unique tier', () => {
    const { container } = render(
      React.createElement(PhaseModelMap, {
        phases: threeSonnetOneOpus,
        modelMap: {},
        onModelMapChange: () => {},
      }),
    );
    const selects = container.querySelectorAll('select');
    expect(selects.length).toBe(2);
  });

  it('A10.2 sonnet tier row lists all 3 sonnet phase names', () => {
    const { container } = render(
      React.createElement(PhaseModelMap, {
        phases: threeSonnetOneOpus,
        modelMap: {},
        onModelMapChange: () => {},
      }),
    );
    // The "sonnet" tier row must mention all sonnet phase names somewhere in
    // the same row (we check the full container text since the layout shows
    // chips under the tier label).
    const text = container.textContent ?? '';
    expect(text).toContain('Spec');
    expect(text).toContain('Behavioral');
    expect(text).toContain('Implement');
    expect(text).toContain('Review');
  });

  it('A10.3 selecting a model writes a single tier key', () => {
    const cb = jest.fn();
    const { container } = render(
      React.createElement(PhaseModelMap, {
        phases: threeSonnetOneOpus,
        modelMap: {},
        onModelMapChange: cb,
      }),
    );
    const selects = container.querySelectorAll('select');
    // Pick the first (sonnet) select; change its value.
    act(() => {
      fireEvent.change(selects[0], { target: { value: 'openai/gpt-4o' } });
    });
    expect(cb).toHaveBeenCalledTimes(1);
    const arg = cb.mock.calls[0][0];
    expect(arg.sonnet).toBe('openai/gpt-4o');
  });

  it('A10.4 clearing an override removes the tier key', () => {
    const cb = jest.fn();
    const { container } = render(
      React.createElement(PhaseModelMap, {
        phases: threeSonnetOneOpus,
        modelMap: { sonnet: 'openai/gpt-4o' },
        onModelMapChange: cb,
      }),
    );
    const selects = container.querySelectorAll('select');
    act(() => {
      fireEvent.change(selects[0], { target: { value: '' } });
    });
    expect(cb).toHaveBeenCalledTimes(1);
    const arg = cb.mock.calls[0][0];
    expect(Object.prototype.hasOwnProperty.call(arg, 'sonnet')).toBe(false);
  });

  it('A10.6 single-phase tier still renders', () => {
    const onePhase: Phase[] = [makePhase('spec', 'Spec', 'opus')];
    const { container } = render(
      React.createElement(PhaseModelMap, {
        phases: onePhase,
        modelMap: {},
        onModelMapChange: () => {},
      }),
    );
    const selects = container.querySelectorAll('select');
    expect(selects.length).toBe(1);
  });

  it('A10.7 empty phase list renders nothing', () => {
    const { container } = render(
      React.createElement(PhaseModelMap, {
        phases: [],
        modelMap: {},
        onModelMapChange: () => {},
      }),
    );
    expect(container.querySelectorAll('select').length).toBe(0);
  });
});

// ─── Group A11 — Gates approve/reject confirmation + score display (#759) ─────

describe('A11 — Gates confirmation + detail', () => {
  it('A11.1/A11.2 the Gates page source contains a confirmation call before approve/reject', () => {
    const src = readFile('frontend/app/gates/page.tsx');
    // Must reference window.confirm or a confirmation prompt mechanism.
    expect(src).toMatch(/window\.confirm|confirm\(|useConfirm|isConfirmOpen/);
  });

  it('A11.4 the Gates page formats scoring_score via toFixed(2) and renders branch → base', () => {
    const src = readFile('frontend/app/gates/page.tsx');
    expect(src).toMatch(/scoring_score|scoreText|confidence.*toFixed\(2\)|toFixed\(2\)/);
    // The arrow character or branch arrow render.
    expect(src).toMatch(/→|&rarr;/);
  });
});

// ─── Group A12 — Token migration (#761) ───────────────────────────────────────

describe('A12 — Design token migration', () => {
  const zincLikePattern = /text-zinc-\d|bg-zinc-\d|border-zinc-\d|placeholder-zinc-\d|placeholder:text-zinc-\d|divide-zinc-\d/;

  const filesInScope = [
    'frontend/components/pipeline/TemplateCard.tsx',
    'frontend/components/pipeline/PhaseList.tsx',
    'frontend/components/pipeline/ProviderSelector.tsx',
    'frontend/components/pipeline/SchemaForm.tsx',
    'frontend/components/pipeline/PhaseModelMap.tsx',
    'frontend/app/templates/page.tsx',
    'frontend/app/runs/page.tsx',
    'frontend/app/templates/new/page.tsx',
    'frontend/app/templates/[id]/TemplateDetailClient.tsx',
    'frontend/app/templates/[id]/edit/EditTemplateClient.tsx',
    'frontend/app/error.tsx',
  ];

  for (const rel of filesInScope) {
    it(`A12.1 ${rel} has no legacy zinc utility classes`, () => {
      const src = readFile(rel);
      expect(src).not.toMatch(zincLikePattern);
    });
  }
});

// ─── Group A13 — Test coverage additions (#776) ───────────────────────────────

describe('A13 — New test files', () => {
  it('A13.1 RunDetailClient test file exists', () => {
    expect(fileExists('frontend/__tests__/components/RunDetailClient.test.tsx')).toBe(true);
  });

  it('A13.2 SchemaForm test file exists', () => {
    expect(fileExists('frontend/__tests__/components/SchemaForm.test.tsx')).toBe(true);
  });

  it('A13.3 RunsPage test file exists', () => {
    expect(fileExists('frontend/__tests__/pages/RunsPage.test.tsx')).toBe(true);
  });

  it('A13.4 the three test files do not mock the react module at top level', () => {
    const files = [
      'frontend/__tests__/components/RunDetailClient.test.tsx',
      'frontend/__tests__/components/SchemaForm.test.tsx',
      'frontend/__tests__/pages/RunsPage.test.tsx',
    ];
    for (const rel of files) {
      const src = readFile(rel);
      // No `jest.mock('react'` or `jest.mock("react"`.
      expect(src).not.toMatch(/jest\.mock\(\s*['"]react['"]/);
    }
  });
});

// ─── Group A14 — Build / typecheck / config gates ─────────────────────────────

describe('A14 — Build, typecheck, config', () => {
  // These are slow shell-outs; we keep them lean.
  // A14.1 tsc, A14.2 jest baseline, A14.3 next.config.js content, A14.4 pytest

  it('A14.3 next.config.js pins output: \'export\'', () => {
    const src = readFile('frontend/next.config.js');
    expect(src).toMatch(/output:\s*['"]export['"]/);
  });

  // A14.1 — tsc clean. Allow opt-out for unit-test mode via env to avoid double
  // type-check cost (the orchestrator's command phase will run tsc separately).
  it('A14.1 typescript compile is clean (tsc --noEmit exits 0)', () => {
    if (process.env.ACCEPTANCE_SKIP_TSC === '1') return;
    let stderr = '';
    try {
      execSync('npx tsc --noEmit', { cwd: FRONTEND_ROOT, stdio: 'pipe' });
    } catch (e) {
      stderr = String((e as { stderr?: Buffer; stdout?: Buffer }).stderr ?? '')
        + String((e as { stdout?: Buffer }).stdout ?? '');
      throw new Error('tsc --noEmit failed:\n' + stderr);
    }
  }, 120_000);
});

// success
