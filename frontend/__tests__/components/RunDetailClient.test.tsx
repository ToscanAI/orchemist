/**
 * RunDetailClient tests (issue #776).
 *
 * Tests the page-level run-detail client component by mocking the API client
 * and SSE hook at module boundary. Does not mock React internals.
 *
 * Covers:
 *   - Initial REST hydration (getRun, listRunArtifacts, getRunPhase0, listPhases)
 *   - SSE hook engagement when engine is up
 *   - SSE hook stays closed for terminal-status runs
 *   - Cancel button calls cancelRun
 *   - Demo fallback when engine is unreachable
 */

import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import * as React from 'react';

// ── Mock next/navigation ─────────────────────────────────────────────────────
const mockPushFn = jest.fn();
jest.mock('next/navigation', () => ({
  useParams: () => ({ id: 'run-abc12345' }),
  useSearchParams: () => ({
    get: () => null,
  }),
  useRouter: () => ({ push: mockPushFn, back: jest.fn() }),
  usePathname: () => '/runs/run-abc12345',
}));

// ── Mock API client ──────────────────────────────────────────────────────────
const mockGetRun = jest.fn();
const mockListRunArtifacts = jest.fn();
const mockGetRunPhase0 = jest.fn();
const mockListPhases = jest.fn();
const mockResumeRun = jest.fn();
const mockCancelRun = jest.fn();

jest.mock('@/lib/api', () => {
  const actual = jest.requireActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    getRun: (...args: unknown[]) => mockGetRun(...args),
    listRunArtifacts: (...args: unknown[]) => mockListRunArtifacts(...args),
    getRunPhase0: (...args: unknown[]) => mockGetRunPhase0(...args),
    listPhases: (...args: unknown[]) => mockListPhases(...args),
    resumeRun: (...args: unknown[]) => mockResumeRun(...args),
    cancelRun: (...args: unknown[]) => mockCancelRun(...args),
  };
});

// ── Mock SSE hook ────────────────────────────────────────────────────────────
const mockUseRunEvents = jest.fn();
jest.mock('@/lib/sse', () => ({
  useRunEvents: (...args: unknown[]) => mockUseRunEvents(...args),
}));

// Import AFTER mocks are set up.
import RunDetailClient from '@/app/runs/[id]/RunDetailClient';

const SAMPLE_RUN = {
  run_id: 'run-abc12345',
  template_id: 'coding-pipeline-standard',
  mode: 'standalone' as const,
  status: 'running' as const,
  current_phase: 'implement',
  completed_phases: ['existing_symbols_inventory', 'spec', 'behavioral'],
  created_at: '2026-05-27T14:00:00Z',
  started_at: '2026-05-27T14:00:00Z',
  completed_at: null,
  // Other optional fields the renderer may consume.
} as unknown;

const SAMPLE_PHASES_RESPONSE = {
  phases: [
    { id: 'existing_symbols_inventory', name: 'Inventory', model_tier: 'sonnet', task_type: 'inventory' },
    { id: 'spec', name: 'Spec', model_tier: 'sonnet', task_type: 'generate' },
    { id: 'behavioral', name: 'Behavioral', model_tier: 'sonnet', task_type: 'generate' },
    { id: 'implement', name: 'Implement', model_tier: 'sonnet', task_type: 'generate' },
  ],
};

beforeEach(() => {
  mockGetRun.mockReset();
  mockListRunArtifacts.mockReset();
  mockGetRunPhase0.mockReset();
  mockListPhases.mockReset();
  mockResumeRun.mockReset();
  mockCancelRun.mockReset();
  mockUseRunEvents.mockReset();
  mockUseRunEvents.mockReturnValue({ events: [], status: 'connecting', connected: false });
  mockPushFn.mockReset();
});

describe('RunDetailClient — REST hydration', () => {
  it('fetches getRun, listRunArtifacts, getRunPhase0, listPhases on mount', async () => {
    mockGetRun.mockResolvedValue(SAMPLE_RUN);
    mockListRunArtifacts.mockResolvedValue({ run_id: 'run-abc12345', files: [] });
    mockGetRunPhase0.mockRejectedValue(new Error('no phase0'));
    mockListPhases.mockResolvedValue(SAMPLE_PHASES_RESPONSE);

    render(<RunDetailClient />);

    await waitFor(() => {
      expect(mockGetRun).toHaveBeenCalledWith('run-abc12345');
    });
    expect(mockListRunArtifacts).toHaveBeenCalled();
    expect(mockGetRunPhase0).toHaveBeenCalled();
    expect(mockListPhases).toHaveBeenCalled();
  });
});

describe('RunDetailClient — SSE engagement', () => {
  it('opens SSE (enabled=true) when engine is up and status is not terminal', async () => {
    mockGetRun.mockResolvedValue(SAMPLE_RUN);
    mockListRunArtifacts.mockResolvedValue({ run_id: 'run-abc12345', files: [] });
    mockGetRunPhase0.mockRejectedValue(new Error('no phase0'));
    mockListPhases.mockResolvedValue(SAMPLE_PHASES_RESPONSE);

    render(<RunDetailClient />);

    await waitFor(() => {
      // After the initial GET resolves, the hook should be called with enabled=true.
      const lastCall = mockUseRunEvents.mock.calls[mockUseRunEvents.mock.calls.length - 1];
      expect(lastCall).toEqual(['run-abc12345', true]);
    });
  });

  it('keeps SSE closed (enabled=false) when run status is terminal (e.g. success)', async () => {
    mockGetRun.mockResolvedValue({ ...SAMPLE_RUN as object, status: 'success', current_phase: null } as unknown);
    mockListRunArtifacts.mockResolvedValue({ run_id: 'run-abc12345', files: [] });
    mockGetRunPhase0.mockRejectedValue(new Error('no phase0'));
    mockListPhases.mockResolvedValue(SAMPLE_PHASES_RESPONSE);

    render(<RunDetailClient />);

    await waitFor(() => {
      const lastCall = mockUseRunEvents.mock.calls[mockUseRunEvents.mock.calls.length - 1];
      expect(lastCall && lastCall[1]).toBe(false);
    });
  });
});

describe('RunDetailClient — actions', () => {
  it('Cancel button calls cancelRun(runId)', async () => {
    mockGetRun.mockResolvedValue(SAMPLE_RUN);
    mockListRunArtifacts.mockResolvedValue({ run_id: 'run-abc12345', files: [] });
    mockGetRunPhase0.mockRejectedValue(new Error('no phase0'));
    mockListPhases.mockResolvedValue(SAMPLE_PHASES_RESPONSE);
    mockCancelRun.mockResolvedValue({ ok: true });

    render(<RunDetailClient />);

    // Cancel button is rendered in the HarnessShell actions slot.
    const cancelBtn = await screen.findByRole('button', { name: /Cancel/i });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(mockCancelRun).toHaveBeenCalledWith('run-abc12345');
    });
  });
});

describe('RunDetailClient — demo fallback', () => {
  it('renders demo data when getRun rejects (engine offline)', async () => {
    mockGetRun.mockRejectedValue(new Error('ECONNREFUSED'));
    mockListRunArtifacts.mockRejectedValue(new Error('ECONNREFUSED'));
    mockGetRunPhase0.mockRejectedValue(new Error('ECONNREFUSED'));
    mockListPhases.mockRejectedValue(new Error('ECONNREFUSED'));

    const { container } = render(<RunDetailClient />);

    await waitFor(() => {
      expect(mockGetRun).toHaveBeenCalled();
    });
    // The demo-fallback path renders the harness shell (with phases) — assert
    // at least one phase chip is present in the DOM.
    expect(container.textContent ?? '').toMatch(/spec|implement|review|behavioral/i);
  });
});
