/**
 * Integration tests for `frontend/app/templates/[id]/page.tsx`.
 *
 * Mocks:
 *  - `@/lib/api` — `getTemplate`, `startRun`, and `ApiError`
 *  - `next/navigation` — `useParams`, `useRouter`
 *
 * Tests cover: loading state, fetch error state, template metadata rendering,
 * PhaseList integration, launch form mode selector, JSON textarea validation,
 * and form submission (success + failure paths).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ---------------------------------------------------------------------------
// Mocks — must be declared before imports that reference them
// ---------------------------------------------------------------------------

// Mock next/navigation
const mockPush = jest.fn();
const mockUseParams = jest.fn();

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
  useParams: () => mockUseParams(),
}));

// Mock next/link to a simple anchor for easier testing
jest.mock('next/link', () => {
  const MockLink = ({
    href,
    children,
    className,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
  }) => (
    <a href={href} className={className}>
      {children}
    </a>
  );
  MockLink.displayName = 'MockLink';
  return MockLink;
});

// Mock @/lib/api
const mockGetTemplate = jest.fn();
const mockStartRun = jest.fn();

jest.mock('@/lib/api', () => {
  class MockApiError extends Error {
    readonly status: number;
    readonly detail: unknown;
    constructor(status: number, detail: unknown, message?: string) {
      super(message ?? `API error ${status}`);
      this.name = 'ApiError';
      this.status = status;
      this.detail = detail;
    }
  }
  return {
    getTemplate: (...args: unknown[]) => mockGetTemplate(...args),
    startRun: (...args: unknown[]) => mockStartRun(...args),
    ApiError: MockApiError,
  };
});

// Import the page AFTER mocks are declared
import TemplateDetailPage from '@/app/templates/[id]/page';
import type { TemplateDetail, RunRecord } from '@/lib/types';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const mockTemplate: TemplateDetail = {
  id: 'tpl-abc123',
  name: 'Content Pipeline v2',
  version: '2.7.0',
  description: 'End-to-end content production pipeline.',
  author: 'René Maier',
  phases_count: 2,
  category: 'content',
  tags: ['content', 'research'],
  phases: [
    {
      id: 'phase-1',
      name: 'Research',
      description: 'Gather sources and data.',
      model_tier: 'sonnet',
      thinking_level: 'low',
      depends_on: [],
      task_type: 'research',
    },
    {
      id: 'phase-2',
      name: 'Write',
      description: 'Draft the article.',
      model_tier: 'opus',
      thinking_level: 'high',
      depends_on: ['phase-1'],
      task_type: 'generation',
    },
  ],
  example_input: { topic: 'AI agents', length: 1000 },
  config_schema: {},
};

const mockRun: RunRecord = {
  run_id: 'run-xyz789',
  template_id: 'tpl-abc123',
  template_path: '/templates/content.yaml',
  mode: 'dry-run',
  status: 'pending',
  current_phase: null,
  completed_phases: [],
  pid: null,
  output_dir: '/tmp/runs/run-xyz789',
  error_message: null,
  gateway_url: null,
  skip_scoring: false,
  scoring_status: null,
  scoring_score: null,
  started_at: null,
  completed_at: null,
  created_at: '2024-01-01T00:00:00Z',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Render the page with a given useParams return value. */
function renderPage(id = 'tpl-abc123') {
  mockUseParams.mockReturnValue({ id });
  return render(<TemplateDetailPage />);
}

/** Returns a never-resolving promise to keep the component in loading state. */
function makeNeverResolve<T>(): Promise<T> {
  return new Promise(() => {/* intentionally never resolves */});
}

// ---------------------------------------------------------------------------
// Test setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  jest.clearAllMocks();
  mockUseParams.mockReturnValue({ id: 'tpl-abc123' });
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('TemplateDetailPage', () => {
  // ── Loading state ──────────────────────────────────────────────────────────

  describe('loading state', () => {
    it('shows loading spinner while fetch is in-flight', () => {
      mockGetTemplate.mockReturnValue(makeNeverResolve());
      renderPage();
      expect(screen.getByRole('status')).toBeInTheDocument();
      expect(screen.getByText(/loading template/i)).toBeInTheDocument();
    });

    it('does not render template content while loading', () => {
      mockGetTemplate.mockReturnValue(makeNeverResolve());
      renderPage();
      expect(screen.queryByRole('heading', { name: /content pipeline/i })).toBeNull();
    });
  });

  // ── Error state ───────────────────────────────────────────────────────────

  describe('error state', () => {
    it('shows error message when getTemplate rejects with ApiError', async () => {
      const { ApiError } = jest.requireMock('@/lib/api') as {
        ApiError: new (status: number, detail: unknown, message?: string) => Error;
      };
      mockGetTemplate.mockRejectedValue(
        new ApiError(404, { detail: 'Not found' }, 'API error 404: Not Found'),
      );
      renderPage();
      await waitFor(() => {
        expect(screen.getByText(/api error 404/i)).toBeInTheDocument();
      });
    });

    it('shows error message for generic Error', async () => {
      mockGetTemplate.mockRejectedValue(new Error('Network failure'));
      renderPage();
      await waitFor(() => {
        expect(screen.getByText(/network failure/i)).toBeInTheDocument();
      });
    });

    it('shows back link even in error state', async () => {
      mockGetTemplate.mockRejectedValue(new Error('Oops'));
      renderPage();
      await waitFor(() => {
        const backLink = screen.getByRole('link', { name: /back to dashboard/i });
        expect(backLink).toHaveAttribute('href', '/');
      });
    });

    it('renders error as alert role', async () => {
      mockGetTemplate.mockRejectedValue(new Error('Oops'));
      renderPage();
      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      });
    });
  });

  // ── Template metadata ──────────────────────────────────────────────────────

  describe('template metadata', () => {
    beforeEach(() => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
    });

    it('renders template name as h1', async () => {
      renderPage();
      await waitFor(() => {
        expect(
          screen.getByRole('heading', { level: 1, name: /content pipeline v2/i }),
        ).toBeInTheDocument();
      });
    });

    it('renders version badge with "v" prefix', async () => {
      renderPage();
      await waitFor(() => {
        expect(screen.getByText('v2.7.0')).toBeInTheDocument();
      });
    });

    it('renders template description', async () => {
      renderPage();
      await waitFor(() => {
        expect(
          screen.getByText('End-to-end content production pipeline.'),
        ).toBeInTheDocument();
      });
    });

    it('renders author', async () => {
      renderPage();
      await waitFor(() => {
        expect(screen.getByText(/René Maier/)).toBeInTheDocument();
      });
    });

    it('renders each tag as a Badge', async () => {
      renderPage();
      await waitFor(() => {
        // 'content' only appears as a tag badge here
        expect(screen.getByText('content')).toBeInTheDocument();
        // 'research' appears both as a tag badge and as a task_type in PhaseList
        const researchElements = screen.getAllByText('research');
        expect(researchElements.length).toBeGreaterThanOrEqual(1);
        // Confirm at least one has the badge-info class (tag badge)
        const tagBadge = researchElements.find((el) =>
          el.className.includes('badge-info'),
        );
        expect(tagBadge).toBeTruthy();
      });
    });

    it('renders back link to "/"', async () => {
      renderPage();
      await waitFor(() => {
        const backLink = screen.getByRole('link', { name: /back to dashboard/i });
        expect(backLink).toHaveAttribute('href', '/');
      });
    });
  });

  // ── PhaseList integration ─────────────────────────────────────────────────

  describe('PhaseList integration', () => {
    it('renders a PhaseList with template.phases', async () => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
      renderPage();
      await waitFor(() => {
        // PhaseList renders an <ol> with aria-label
        expect(
          screen.getByRole('list', { name: 'Phase execution plan' }),
        ).toBeInTheDocument();
      });
    });

    it('renders correct number of phase items', async () => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
      renderPage();
      await waitFor(() => {
        const items = screen.getAllByRole('listitem');
        expect(items).toHaveLength(mockTemplate.phases.length);
      });
    });
  });

  // ── Launch form — mode selector ────────────────────────────────────────────

  describe('launch form — mode selector', () => {
    beforeEach(() => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
    });

    it('defaults to dry-run mode selected', async () => {
      renderPage();
      await waitFor(() => {
        const dryRunBtn = screen.getByRole('button', { name: 'dry-run' });
        expect(dryRunBtn).toHaveAttribute('aria-pressed', 'true');
      });
    });

    it('inactive mode buttons have aria-pressed=false', async () => {
      renderPage();
      await waitFor(() => {
        const standaloneBtn = screen.getByRole('button', { name: 'standalone' });
        const openclawBtn = screen.getByRole('button', { name: 'openclaw' });
        expect(standaloneBtn).toHaveAttribute('aria-pressed', 'false');
        expect(openclawBtn).toHaveAttribute('aria-pressed', 'false');
      });
    });

    it('switches mode when a mode button is clicked', async () => {
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: 'standalone' }));
      await user.click(screen.getByRole('button', { name: 'standalone' }));
      expect(screen.getByRole('button', { name: 'standalone' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(screen.getByRole('button', { name: 'dry-run' })).toHaveAttribute(
        'aria-pressed',
        'false',
      );
    });

    it('active mode button has primary variant (bg-brand-500 class)', async () => {
      renderPage();
      await waitFor(() => {
        const dryRunBtn = screen.getByRole('button', { name: 'dry-run' });
        expect(dryRunBtn.className).toContain('bg-brand-500');
      });
    });

    it('inactive mode buttons have secondary variant (bg-surface-2 class)', async () => {
      renderPage();
      await waitFor(() => {
        const standaloneBtn = screen.getByRole('button', { name: 'standalone' });
        expect(standaloneBtn.className).toContain('bg-surface-2');
      });
    });
  });

  // ── Launch form — JSON input ───────────────────────────────────────────────

  describe('launch form — JSON input', () => {
    beforeEach(() => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
    });

    it('renders a JSON textarea fallback when config_schema has no properties', async () => {
      renderPage();
      await waitFor(() => {
        const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
        expect(textarea).toBeInTheDocument();
        expect(textarea.value).toBe('{}');
      });
    });

    it('shows Load Example button when example_input is available', async () => {
      renderPage();
      await waitFor(() => {
        expect(screen.getByText(/load example/i)).toBeInTheDocument();
      });
    });

    it('hides Load Example button when example_input is null', async () => {
      mockGetTemplate.mockResolvedValue({ ...mockTemplate, example_input: null });
      renderPage();
      await waitFor(() => screen.getByRole('textbox'));
      expect(screen.queryByText(/load example/i)).toBeNull();
    });

    it('populates textarea on Load Example click', async () => {
      renderPage();
      await waitFor(() => screen.getByText(/load example/i));
      fireEvent.click(screen.getByText(/load example/i));
      await waitFor(() => {
        const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
        const parsed = JSON.parse(textarea.value);
        expect(parsed).toEqual(mockTemplate.example_input);
      });
    });

    it('submit button is always enabled with empty JSON (SchemaForm manages validation)', async () => {
      renderPage();
      await waitFor(() => {
        const submitBtn = screen.getByRole('button', { name: /launch run/i });
        expect(submitBtn).not.toBeDisabled();
      });
    });
  });

  // ── Launch form — submission ───────────────────────────────────────────────

  describe('launch form — submission', () => {
    beforeEach(() => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
    });

    it('calls startRun with correct template, mode, and input on submit', async () => {
      mockStartRun.mockResolvedValue(mockRun);
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      // Use fireEvent.change to set JSON (avoids userEvent special-char parsing of { })
      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{"key":"value"}' } });

      await user.click(screen.getByRole('button', { name: /launch run/i }));

      await waitFor(() => {
        expect(mockStartRun).toHaveBeenCalledWith({
          template: mockTemplate.id,
          mode: 'dry-run',
          input: { key: 'value' },
        });
      });
    });

    it('navigates to /runs/{run_id} on successful startRun', async () => {
      mockStartRun.mockResolvedValue(mockRun);
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{}' } });

      await user.click(screen.getByRole('button', { name: /launch run/i }));

      await waitFor(() => {
        expect(mockPush).toHaveBeenCalledWith(`/runs/${mockRun.run_id}`);
      });
    });

    it('shows API error message on startRun rejection', async () => {
      const { ApiError } = jest.requireMock('@/lib/api') as {
        ApiError: new (status: number, detail: unknown, message?: string) => Error;
      };
      mockStartRun.mockRejectedValue(
        new ApiError(500, {}, 'API error 500: Internal Server Error'),
      );
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{}' } });

      await user.click(screen.getByRole('button', { name: /launch run/i }));

      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
        expect(screen.getByText(/api error 500/i)).toBeInTheDocument();
      });
    });

    it('re-enables submit button after API error', async () => {
      const { ApiError } = jest.requireMock('@/lib/api') as {
        ApiError: new (status: number, detail: unknown, message?: string) => Error;
      };
      mockStartRun.mockRejectedValue(
        new ApiError(500, {}, 'API error 500: Internal Server Error'),
      );
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{}' } });

      await user.click(screen.getByRole('button', { name: /launch run/i }));

      await waitFor(() => {
        const submitBtn = screen.getByRole('button', { name: /launch run/i });
        expect(submitBtn).not.toBeDisabled();
      });
    });

    it('does not navigate on API error', async () => {
      const { ApiError } = jest.requireMock('@/lib/api') as {
        ApiError: new (status: number, detail: unknown, message?: string) => Error;
      };
      mockStartRun.mockRejectedValue(new ApiError(500, {}, 'Server error'));
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{}' } });

      await user.click(screen.getByRole('button', { name: /launch run/i }));

      await waitFor(() => screen.getByRole('alert'));
      expect(mockPush).not.toHaveBeenCalled();
    });

    it('clears apiError when user changes mode', async () => {
      const { ApiError } = jest.requireMock('@/lib/api') as {
        ApiError: new (status: number, detail: unknown, message?: string) => Error;
      };
      mockStartRun.mockRejectedValue(new ApiError(500, {}, 'Server error'));
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{}' } });
      await user.click(screen.getByRole('button', { name: /launch run/i }));
      await waitFor(() => screen.getByRole('alert'));

      // Changing mode should clear the error
      await user.click(screen.getByRole('button', { name: 'standalone' }));
      await waitFor(() => {
        expect(screen.queryByRole('alert')).toBeNull();
      });
    });

    it('clears apiError when user edits JSON textarea', async () => {
      const { ApiError } = jest.requireMock('@/lib/api') as {
        ApiError: new (status: number, detail: unknown, message?: string) => Error;
      };
      mockStartRun.mockRejectedValue(new ApiError(500, {}, 'Server error'));
      const user = userEvent.setup();
      renderPage();
      await waitFor(() => screen.getByRole('button', { name: /launch run/i }));

      const textarea = screen.getByRole('textbox');
      fireEvent.change(textarea, { target: { value: '{}' } });
      await user.click(screen.getByRole('button', { name: /launch run/i }));
      await waitFor(() => screen.getByRole('alert'));

      // Changing textarea value should clear the error
      fireEvent.change(textarea, { target: { value: '{ }' } });
      await waitFor(() => {
        expect(screen.queryByRole('alert')).toBeNull();
      });
    });
  });

  // ── Navigation ─────────────────────────────────────────────────────────────

  describe('navigation', () => {
    it('renders a back link to "/" when template is loaded', async () => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
      renderPage();
      await waitFor(() => {
        const backLink = screen.getByRole('link', { name: /back to dashboard/i });
        expect(backLink).toHaveAttribute('href', '/');
      });
    });
  });

  // ── Params decoding ────────────────────────────────────────────────────────

  describe('params decoding', () => {
    it('decodes URI-encoded id before passing to getTemplate', async () => {
      mockGetTemplate.mockResolvedValue(mockTemplate);
      mockUseParams.mockReturnValue({ id: encodeURIComponent('my template') });
      render(<TemplateDetailPage />);
      await waitFor(() => {
        expect(mockGetTemplate).toHaveBeenCalledWith('my template');
      });
    });
  });
});
