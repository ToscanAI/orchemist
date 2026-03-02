import React from 'react';
import { render, screen } from '@testing-library/react';
import { Badge } from '@/components/ui/Badge';

describe('Badge', () => {
  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  describe('rendering', () => {
    it('renders children', () => {
      render(<Badge>Running</Badge>);
      expect(screen.getByText('Running')).toBeInTheDocument();
    });

    it('renders as a <span>', () => {
      render(<Badge>Span test</Badge>);
      expect(screen.getByText('Span test').tagName).toBe('SPAN');
    });

    it('forwards ref to the underlying <span>', () => {
      const ref = React.createRef<HTMLSpanElement>();
      render(<Badge ref={ref}>Ref</Badge>);
      expect(ref.current).toBeInstanceOf(HTMLSpanElement);
    });

    it('uses neutral variant by default', () => {
      render(<Badge>Default</Badge>);
      expect(screen.getByText('Default').className).toContain('badge-neutral');
    });
  });

  // -------------------------------------------------------------------------
  // Variants
  // -------------------------------------------------------------------------

  describe('variants', () => {
    const cases: Array<[string, string]> = [
      ['success', 'badge-success'],
      ['warning', 'badge-warning'],
      ['error', 'badge-error'],
      ['info', 'badge-info'],
      ['neutral', 'badge-neutral'],
    ];

    it.each(cases)('renders %s variant with class %s', (variant, expectedClass) => {
      render(
        <Badge variant={variant as React.ComponentProps<typeof Badge>['variant']}>
          {variant}
        </Badge>,
      );
      expect(screen.getByText(variant).className).toContain(expectedClass);
    });
  });

  // -------------------------------------------------------------------------
  // Custom class / attrs
  // -------------------------------------------------------------------------

  describe('customisation', () => {
    it('merges additional className', () => {
      render(<Badge className="ml-2">Merged</Badge>);
      expect(screen.getByText('Merged').className).toContain('ml-2');
    });

    it('retains variant class when custom className is provided', () => {
      render(
        <Badge variant="success" className="ml-2">
          Both
        </Badge>,
      );
      const el = screen.getByText('Both');
      expect(el.className).toContain('badge-success');
      expect(el.className).toContain('ml-2');
    });

    it('passes extra HTML attributes to the <span>', () => {
      render(<Badge data-testid="status-badge">Attrs</Badge>);
      expect(screen.getByTestId('status-badge')).toBeInTheDocument();
    });
  });
});
