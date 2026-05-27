/**
 * RunsPage tests (issue #776).
 *
 * Covers pagination arithmetic, status filtering with offset reset, and
 * auto-refresh timer behavior. Mocks at API boundary; no React internals.
 */

import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import * as React from 'react';

import { RUNS_REFRESH_INTERVAL_MS } from '@/lib/constants';

// ── Mock next/navigation ─────────────────────────────────────────────────────
const mockPushFn = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPushFn, back: jest.fn() }),
  useParams: () => ({}),
  useSearchParams: () => ({ get: () => null }),
  usePathname: () => '/runs',
}));

// ── Mock API client ──────────────────────────────────────────────────────────
const mockListRuns = jest.fn();
jest.mock('@/lib/api', () => {
  const actual = jest.requireActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    listRuns: (...args: unknown[]) => mockListRuns(...args),
  };
});

import RunsPage from '@/app/runs/page';

function makeRun(idx: number) {
  return {
    run_id: `run-${idx.toString().padStart(4, '0')}`,
    template_id: 'coding-pipeline-standard',
    mode: 'standalone' as const,
    status: 'success' as const,
    current_phase: null,
    completed_phases: [],
    created_at: '2026-05-27T14:00:00Z',
    started_at: '2026-05-27T14:00:00Z',
    completed_at: '2026-05-27T14:30:00Z',
  };
}

beforeEach(() => {
  mockListRuns.mockReset();
  mockPushFn.mockReset();
});

describe('RunsPage — pagination', () => {
  it('with 25 total runs and page size 20, renders pagination controls', async () => {
    // Page size on this page is 20 (per the source). 25 total = 2 pages.
    mockListRuns.mockResolvedValue({
      items: Array.from({ length: 20 }, (_, i) => makeRun(i)),
      total: 25,
      limit: 20,
      offset: 0,
    });
    render(<RunsPage />);
    await waitFor(() => expect(mockListRuns).toHaveBeenCalled());
    // Wait for items to render.
    await screen.findByText('Page 1 of 2');
    // Previous + Next buttons exist.
    expect(screen.getByRole('button', { name: /Previous/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Next/i })).toBeInTheDocument();
  });

  it('Next button advances offset to next page', async () => {
    mockListRuns.mockResolvedValue({
      items: Array.from({ length: 20 }, (_, i) => makeRun(i)),
      total: 50,
      limit: 20,
      offset: 0,
    });
    render(<RunsPage />);
    await screen.findByRole('button', { name: /Next/i });
    fireEvent.click(screen.getByRole('button', { name: /Next/i }));
    await waitFor(() => {
      // listRuns called with offset=20 after Next.
      const lastCall = mockListRuns.mock.calls[mockListRuns.mock.calls.length - 1][0];
      expect(lastCall.offset).toBe(20);
    });
  });
});

describe('RunsPage — status filter', () => {
  it('resets offset to 0 when status filter changes', async () => {
    mockListRuns.mockResolvedValue({
      items: Array.from({ length: 20 }, (_, i) => makeRun(i)),
      total: 100,
      limit: 20,
      offset: 0,
    });
    render(<RunsPage />);
    await screen.findByRole('button', { name: /Next/i });

    // Advance offset by clicking Next.
    fireEvent.click(screen.getByRole('button', { name: /Next/i }));
    await waitFor(() => {
      const c = mockListRuns.mock.calls[mockListRuns.mock.calls.length - 1][0];
      expect(c.offset).toBe(20);
    });

    // Change status filter.
    const filterSelect = screen.getByLabelText('Filter by status') as HTMLSelectElement;
    fireEvent.change(filterSelect, { target: { value: 'success' } });

    await waitFor(() => {
      // After the filter-change useEffect resets offset to 0 AND fetchRuns
      // re-fetches, the LAST call must have offset=0 and status='success'.
      const c = mockListRuns.mock.calls[mockListRuns.mock.calls.length - 1][0];
      expect(c.offset).toBe(0);
      expect(c.status).toBe('success');
    });
  });
});

describe('RunsPage — auto-refresh', () => {
  it('fires a refetch after RUNS_REFRESH_INTERVAL_MS', async () => {
    jest.useFakeTimers();
    mockListRuns.mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });
    render(<RunsPage />);
    await waitFor(() => expect(mockListRuns).toHaveBeenCalledTimes(1));

    const before = mockListRuns.mock.calls.length;
    act(() => {
      jest.advanceTimersByTime(RUNS_REFRESH_INTERVAL_MS);
    });

    await waitFor(() => {
      expect(mockListRuns.mock.calls.length).toBeGreaterThan(before);
    });
    jest.useRealTimers();
  });
});
