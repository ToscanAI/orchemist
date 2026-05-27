/**
 * Acceptance tests for issue #888 — EngineOfflineGuard.
 *
 * Written from the behavioral contracts in
 * `.orchemist/runs/20260527-8b1881/behavioral.md` (Section A).
 *
 * Tests use Jest + @testing-library/react. The Playwright tests
 * (`tests-e2e/harness-screens.spec.ts`) assert the cross-page wiring via
 * the layout integration. The grep / TS-compile / README / PR contracts
 * are environment-level and verified separately.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';

// Mock the API module so `getHealth` is controllable per-test. Import path
// matches the production source — EngineOfflineGuard imports getHealth from
// '@/lib/api' via the useApi hook.
jest.mock('@/lib/api', () => ({
  __esModule: true,
  getHealth: jest.fn(),
}));

import { getHealth } from '@/lib/api';
import { EngineOfflineGuard } from '@/components/harness/EngineOfflineGuard';

const mockedGetHealth = getHealth as jest.MockedFunction<typeof getHealth>;

describe('EngineOfflineGuard (#888)', () => {
  beforeEach(() => {
    mockedGetHealth.mockReset();
  });

  // ── C1 — Engine reachable on mount → renders children ──
  describe('C1: engine reachable → renders children', () => {
    it('renders children when getHealth resolves with status ok', async () => {
      mockedGetHealth.mockResolvedValue({ status: 'ok', version: '0.5.0' });

      render(
        <EngineOfflineGuard>
          <div data-testid="protected-content">protected content</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('protected-content')).toBeInTheDocument();
      });
      expect(screen.queryByTestId('engine-offline-guard')).not.toBeInTheDocument();
    });
  });

  // ── C2 — Engine offline on mount → renders offline UI ──
  describe('C2: engine offline → renders offline UI', () => {
    it('renders the offline guard region when getHealth rejects', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div data-testid="protected-content">protected content</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-guard')).toBeInTheDocument();
      });
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });

    it('shows the canonical "Engine unreachable at <url>" heading', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div data-testid="protected-content">protected</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        const region = screen.getByTestId('engine-offline-guard');
        expect(region.textContent ?? '').toMatch(/Engine unreachable at .+/);
      });
    });

    it('exposes a Retry button with data-testid="engine-offline-retry"', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div>nope</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-retry')).toBeInTheDocument();
      });
      const button = screen.getByTestId('engine-offline-retry');
      expect(button.tagName).toBe('BUTTON');
      const accessibleName = button.getAttribute('aria-label') ?? button.textContent ?? '';
      expect(accessibleName).toMatch(/Retry/);
    });

    it('exposes a docs link pointing to the orchemist quickstart', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div>nope</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-docs-link')).toBeInTheDocument();
      });

      const link = screen.getByTestId('engine-offline-docs-link') as HTMLAnchorElement;
      expect(link.tagName).toBe('A');
      expect(link.getAttribute('href') ?? '').toMatch(/https:\/\/github\.com\/ToscanAI\/orchemist/);
      expect(link.getAttribute('href') ?? '').toMatch(/quickstart/);
    });
  });

  // ── C3 — Loading state (probe in flight) ──
  describe('C3: loading state', () => {
    it('does not show offline UI before the probe settles', () => {
      // Never-resolving promise keeps the loading state observable.
      let _resolve: (v: { status: string; version: string }) => void = () => {};
      mockedGetHealth.mockImplementation(
        () =>
          new Promise((resolve) => {
            _resolve = resolve;
          }),
      );

      render(
        <EngineOfflineGuard>
          <div data-testid="protected-content">protected</div>
        </EngineOfflineGuard>,
      );

      expect(screen.queryByTestId('engine-offline-guard')).not.toBeInTheDocument();

      act(() => _resolve({ status: 'ok', version: '0.0.0' }));
    });
  });

  // ── C4 — Clicking Retry re-probes ──
  describe('C4: retry button triggers new probe', () => {
    it('calls getHealth a second time when Retry is clicked', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div>nope</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-retry')).toBeInTheDocument();
      });
      const initialCallCount = mockedGetHealth.mock.calls.length;
      expect(initialCallCount).toBeGreaterThanOrEqual(1);

      fireEvent.click(screen.getByTestId('engine-offline-retry'));

      await waitFor(() => {
        expect(mockedGetHealth.mock.calls.length).toBeGreaterThan(initialCallCount);
      });
    });
  });

  // ── C5 — Successful retry replaces error with children ──
  describe('C5: successful retry swaps error UI for children', () => {
    it('renders children after a successful retry', async () => {
      mockedGetHealth
        .mockRejectedValueOnce(new Error('engine offline'))
        .mockResolvedValueOnce({ status: 'ok', version: '0.5.0' });

      render(
        <EngineOfflineGuard>
          <div data-testid="protected-content">protected</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-retry')).toBeInTheDocument();
      });

      fireEvent.click(screen.getByTestId('engine-offline-retry'));

      await waitFor(() => {
        expect(screen.getByTestId('protected-content')).toBeInTheDocument();
      });
      expect(screen.queryByTestId('engine-offline-guard')).not.toBeInTheDocument();
    });
  });

  // ── C6 — Failed retry keeps the error UI ──
  describe('C6: failed retry keeps error UI', () => {
    it('keeps offline UI after a failed retry', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div data-testid="protected-content">protected</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-retry')).toBeInTheDocument();
      });

      fireEvent.click(screen.getByTestId('engine-offline-retry'));

      await waitFor(() => {
        expect(mockedGetHealth.mock.calls.length).toBeGreaterThanOrEqual(2);
      });

      expect(screen.getByTestId('engine-offline-guard')).toBeInTheDocument();
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });
  });

  // ── C7 — Retry button is keyboard focusable ──
  describe('C7: retry button is keyboard accessible', () => {
    it('the retry button is a focusable <button> element (not a div)', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div>nope</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-retry')).toBeInTheDocument();
      });
      const button = screen.getByTestId('engine-offline-retry');
      expect(button.tagName).toBe('BUTTON');

      button.focus();
      expect(document.activeElement).toBe(button);
    });
  });

  // ── C8 — Offline region announces to screen readers ──
  describe('C8: offline region announces to screen readers', () => {
    it('has role="alert" on the offline region (or a child with role=alert)', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div>nope</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-guard')).toBeInTheDocument();
      });

      const region = screen.getByTestId('engine-offline-guard');
      const rootHasAlertRole = region.getAttribute('role') === 'alert';
      const childAlert = region.querySelector('[role="alert"]');
      expect(rootHasAlertRole || childAlert !== null).toBe(true);
    });
  });

  // ── C9 — Docs link opens externally (safe rel) ──
  describe('C9: docs link opens externally with safe rel', () => {
    it('docs link has target=_blank and rel containing noopener+noreferrer', async () => {
      mockedGetHealth.mockRejectedValue(new Error('engine offline'));

      render(
        <EngineOfflineGuard>
          <div>nope</div>
        </EngineOfflineGuard>,
      );

      await waitFor(() => {
        expect(screen.getByTestId('engine-offline-docs-link')).toBeInTheDocument();
      });

      const link = screen.getByTestId('engine-offline-docs-link');
      expect(link.getAttribute('target')).toBe('_blank');
      const rel = link.getAttribute('rel') ?? '';
      expect(rel).toMatch(/noopener/);
      expect(rel).toMatch(/noreferrer/);
    });
  });
});
