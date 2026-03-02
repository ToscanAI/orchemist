import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Button } from '@/components/ui/Button';

describe('Button', () => {
  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  describe('rendering', () => {
    it('renders children', () => {
      render(<Button>Click me</Button>);
      expect(screen.getByRole('button', { name: 'Click me' })).toBeInTheDocument();
    });

    it('forwards ref to the underlying <button>', () => {
      const ref = React.createRef<HTMLButtonElement>();
      render(<Button ref={ref}>Ref test</Button>);
      expect(ref.current).toBeInstanceOf(HTMLButtonElement);
    });

    it('uses primary variant by default', () => {
      render(<Button>Default</Button>);
      const btn = screen.getByRole('button');
      expect(btn.className).toContain('bg-brand-500');
    });

    it('uses md size by default', () => {
      render(<Button>Default size</Button>);
      const btn = screen.getByRole('button');
      expect(btn.className).toContain('px-4');
    });
  });

  // -------------------------------------------------------------------------
  // Variants
  // -------------------------------------------------------------------------

  describe('variants', () => {
    it('renders primary variant', () => {
      render(<Button variant="primary">Primary</Button>);
      expect(screen.getByRole('button').className).toContain('bg-brand-500');
    });

    it('renders secondary variant', () => {
      render(<Button variant="secondary">Secondary</Button>);
      expect(screen.getByRole('button').className).toContain('bg-surface-2');
    });

    it('renders ghost variant', () => {
      render(<Button variant="ghost">Ghost</Button>);
      expect(screen.getByRole('button').className).toContain('bg-transparent');
    });

    it('renders danger variant', () => {
      render(<Button variant="danger">Danger</Button>);
      expect(screen.getByRole('button').className).toContain('bg-error');
    });
  });

  // -------------------------------------------------------------------------
  // Sizes
  // -------------------------------------------------------------------------

  describe('sizes', () => {
    it('renders sm size', () => {
      render(<Button size="sm">Small</Button>);
      expect(screen.getByRole('button').className).toContain('px-3');
    });

    it('renders md size', () => {
      render(<Button size="md">Medium</Button>);
      expect(screen.getByRole('button').className).toContain('px-4');
    });

    it('renders lg size', () => {
      render(<Button size="lg">Large</Button>);
      expect(screen.getByRole('button').className).toContain('px-5');
    });
  });

  // -------------------------------------------------------------------------
  // States
  // -------------------------------------------------------------------------

  describe('disabled state', () => {
    it('is disabled when disabled prop is true', () => {
      render(<Button disabled>Disabled</Button>);
      expect(screen.getByRole('button')).toBeDisabled();
    });

    it('sets aria-disabled when disabled', () => {
      render(<Button disabled>Disabled</Button>);
      expect(screen.getByRole('button')).toHaveAttribute('aria-disabled', 'true');
    });

    it('does not fire onClick when disabled', () => {
      const handleClick = jest.fn();
      render(<Button disabled onClick={handleClick}>Disabled</Button>);
      fireEvent.click(screen.getByRole('button'));
      expect(handleClick).not.toHaveBeenCalled();
    });
  });

  describe('loading state', () => {
    it('is disabled when loading is true', () => {
      render(<Button loading>Loading</Button>);
      expect(screen.getByRole('button')).toBeDisabled();
    });

    it('sets aria-busy when loading', () => {
      render(<Button loading>Loading</Button>);
      expect(screen.getByRole('button')).toHaveAttribute('aria-busy', 'true');
    });

    it('renders a spinner svg when loading', () => {
      render(<Button loading>Loading</Button>);
      const svg = screen.getByRole('button').querySelector('svg');
      expect(svg).toBeInTheDocument();
    });

    it('still renders children alongside the spinner', () => {
      render(<Button loading>Saving…</Button>);
      expect(screen.getByText('Saving…')).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Interaction
  // -------------------------------------------------------------------------

  describe('interaction', () => {
    it('calls onClick when clicked', async () => {
      const user = userEvent.setup();
      const handleClick = jest.fn();
      render(<Button onClick={handleClick}>Click</Button>);
      await user.click(screen.getByRole('button'));
      expect(handleClick).toHaveBeenCalledTimes(1);
    });

    it('passes extra HTML attributes to the <button>', () => {
      render(<Button data-testid="my-button">Attrs</Button>);
      expect(screen.getByTestId('my-button')).toBeInTheDocument();
    });

    it('merges custom className', () => {
      render(<Button className="my-custom-class">Custom</Button>);
      expect(screen.getByRole('button').className).toContain('my-custom-class');
    });
  });
});
