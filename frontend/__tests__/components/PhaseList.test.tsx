/**
 * Unit tests for `frontend/components/pipeline/PhaseList.tsx`.
 *
 * Covers:
 *  - Empty state rendering
 *  - Phase rows: name, 1-based index, model_tier badge, description, task_type,
 *    depends_on
 *  - Badge variant mapping for model_tier
 *  - Accessibility attributes (aria-label on <ol> and <li>)
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { PhaseList } from '@/components/pipeline/PhaseList';
import type { PhaseDetail } from '@/lib/types';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** Minimal set of phases used across most tests. */
const mockPhases: PhaseDetail[] = [
  {
    id: 'phase-1',
    name: 'Research',
    description: 'Gather information',
    model_tier: 'sonnet',
    thinking_level: 'low',
    depends_on: [],
    task_type: 'research',
  },
  {
    id: 'phase-2',
    name: 'Write',
    description: 'Draft the output',
    model_tier: 'opus',
    thinking_level: 'high',
    depends_on: ['phase-1'],
    task_type: 'generation',
  },
];

/** Single phase with haiku tier, no description, no dependencies. */
const haikusPhase: PhaseDetail = {
  id: 'haiku-phase',
  name: 'Summarise',
  description: '',
  model_tier: 'haiku',
  thinking_level: 'off',
  depends_on: [],
  task_type: 'summarisation',
};

/** Phase with unknown/custom model tier. */
const unknownTierPhase: PhaseDetail = {
  id: 'custom-phase',
  name: 'Custom',
  description: 'Custom phase',
  model_tier: 'gpt-4o',
  thinking_level: 'off',
  depends_on: [],
  task_type: 'custom',
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PhaseList', () => {
  // ── Empty state ──────────────────────────────────────────────────────────

  describe('empty state', () => {
    it('renders a "no phases" message when phases array is empty', () => {
      render(<PhaseList phases={[]} />);
      expect(
        screen.getByText(/no phases defined for this template/i),
      ).toBeInTheDocument();
    });

    it('does not render an <ol> when phases is empty', () => {
      const { container } = render(<PhaseList phases={[]} />);
      expect(container.querySelector('ol')).toBeNull();
    });
  });

  // ── Rendering ─────────────────────────────────────────────────────────────

  describe('rendering', () => {
    it('renders one list item per phase', () => {
      render(<PhaseList phases={mockPhases} />);
      const items = screen.getAllByRole('listitem');
      expect(items).toHaveLength(mockPhases.length);
    });

    it('renders phase name for each phase', () => {
      render(<PhaseList phases={mockPhases} />);
      expect(screen.getByText('Research')).toBeInTheDocument();
      expect(screen.getByText('Write')).toBeInTheDocument();
    });

    it('renders 1-based index numbers (1, 2, 3…)', () => {
      render(<PhaseList phases={mockPhases} />);
      // Index numbers are in aria-hidden spans — query by text content
      expect(screen.getByText('1')).toBeInTheDocument();
      expect(screen.getByText('2')).toBeInTheDocument();
    });

    it('renders model_tier inside a Badge', () => {
      render(<PhaseList phases={mockPhases} />);
      // model_tier values appear as badge children
      expect(screen.getByText('sonnet')).toBeInTheDocument();
      expect(screen.getByText('opus')).toBeInTheDocument();
    });

    it('renders phase description when present', () => {
      render(<PhaseList phases={mockPhases} />);
      expect(screen.getByText('Gather information')).toBeInTheDocument();
      expect(screen.getByText('Draft the output')).toBeInTheDocument();
    });

    it('renders task_type in the metadata row', () => {
      render(<PhaseList phases={mockPhases} />);
      expect(screen.getByText('research')).toBeInTheDocument();
      expect(screen.getByText('generation')).toBeInTheDocument();
    });

    it('renders depends_on IDs when depends_on is non-empty', () => {
      render(<PhaseList phases={mockPhases} />);
      // Phase 2 depends on phase-1
      expect(screen.getByText('phase-1')).toBeInTheDocument();
    });

    it('does not render depends_on section when depends_on is empty', () => {
      render(<PhaseList phases={[mockPhases[0]!]} />);
      expect(screen.queryByText(/depends on/i)).toBeNull();
    });

    it('does not render description element when description is empty string', () => {
      render(<PhaseList phases={[haikusPhase]} />);
      // The haiku phase has an empty description — no <p> for description
      // The phase name and task_type should still appear
      expect(screen.getByText('Summarise')).toBeInTheDocument();
      expect(screen.getByText('summarisation')).toBeInTheDocument();
    });
  });

  // ── Badge variant mapping ─────────────────────────────────────────────────

  describe('Badge variant mapping', () => {
    it('uses "error" variant for model_tier="opus"', () => {
      render(<PhaseList phases={[mockPhases[1]!]} />);
      // Badge renders with badge-error class
      const badge = screen.getByText('opus');
      expect(badge.className).toContain('badge-error');
    });

    it('uses "warning" variant for model_tier="sonnet"', () => {
      render(<PhaseList phases={[mockPhases[0]!]} />);
      const badge = screen.getByText('sonnet');
      expect(badge.className).toContain('badge-warning');
    });

    it('uses "info" variant for model_tier="haiku"', () => {
      render(<PhaseList phases={[haikusPhase]} />);
      const badge = screen.getByText('haiku');
      expect(badge.className).toContain('badge-info');
    });

    it('uses "neutral" variant for unknown model_tier', () => {
      render(<PhaseList phases={[unknownTierPhase]} />);
      const badge = screen.getByText('gpt-4o');
      expect(badge.className).toContain('badge-neutral');
    });
  });

  // ── Accessibility ─────────────────────────────────────────────────────────

  describe('accessibility', () => {
    it('renders an <ol> with aria-label="Phase execution plan"', () => {
      render(<PhaseList phases={mockPhases} />);
      const list = screen.getByRole('list', { name: 'Phase execution plan' });
      expect(list.tagName).toBe('OL');
    });

    it('each <li> has an aria-label containing the phase name', () => {
      render(<PhaseList phases={mockPhases} />);
      expect(
        screen.getByRole('listitem', { name: /Phase 1: Research/i }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole('listitem', { name: /Phase 2: Write/i }),
      ).toBeInTheDocument();
    });

    it('index number spans are aria-hidden', () => {
      const { container } = render(<PhaseList phases={mockPhases} />);
      const hiddenSpans = container.querySelectorAll('[aria-hidden="true"]');
      // Should have aria-hidden spans for the index numbers
      expect(hiddenSpans.length).toBeGreaterThanOrEqual(2);
    });
  });

  // ── Multiple depends_on ───────────────────────────────────────────────────

  describe('multiple depends_on', () => {
    it('joins multiple dependencies with ", "', () => {
      const phase: PhaseDetail = {
        id: 'phase-3',
        name: 'Review',
        description: 'Review output',
        model_tier: 'haiku',
        thinking_level: 'low',
        depends_on: ['phase-1', 'phase-2'],
        task_type: 'review',
      };
      render(<PhaseList phases={[phase]} />);
      expect(screen.getByText('phase-1, phase-2')).toBeInTheDocument();
    });
  });
});
