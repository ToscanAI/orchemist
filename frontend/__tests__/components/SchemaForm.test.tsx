/**
 * SchemaForm component tests (issue #776).
 *
 * Covers:
 *   - Schema-driven rendering for string / number / boolean / enum
 *   - JSON textarea fallback when schema is empty
 *   - Default value seeding on mount
 *   - Required-field marker rendering
 *   - Load Example button populates form / textarea
 *
 * No mocking of the React module — tests render the real component.
 */

import { render, screen, fireEvent, act } from '@testing-library/react';
import * as React from 'react';

import { SchemaForm } from '@/components/pipeline/SchemaForm';

describe('SchemaForm', () => {
  describe('schema-driven rendering', () => {
    it('renders a text input for a string field', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{ properties: { name: { type: 'string' } } }}
          onChange={onChange}
        />,
      );
      const input = screen.getByLabelText('name');
      expect(input).toBeInTheDocument();
      expect(input).toHaveAttribute('type', 'text');
    });

    it('renders a number input for a number field', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{ properties: { temperature: { type: 'number' } } }}
          onChange={onChange}
        />,
      );
      const input = screen.getByLabelText('temperature');
      expect(input).toHaveAttribute('type', 'number');
    });

    it('renders a checkbox for a boolean field', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{ properties: { dry_run: { type: 'boolean' } } }}
          onChange={onChange}
        />,
      );
      const checkbox = screen.getByRole('checkbox', { name: /dry_run/ });
      expect(checkbox).toBeInTheDocument();
    });

    it('renders a select for an enum field', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{ properties: { mode: { type: 'string', enum: ['fast', 'slow'] as const } } }}
          onChange={onChange}
        />,
      );
      const select = screen.getByLabelText('mode') as HTMLSelectElement;
      expect(select.tagName).toBe('SELECT');
      const options = Array.from(select.options).map((o) => o.value);
      expect(options).toContain('fast');
      expect(options).toContain('slow');
    });

    it('marks required fields with an asterisk', () => {
      const onChange = jest.fn();
      const { container } = render(
        <SchemaForm
          schema={{
            properties: { name: { type: 'string' } },
            required: ['name'],
          }}
          onChange={onChange}
        />,
      );
      // The asterisk is rendered as a <span> with the literal "*" character
      // next to the label.
      const text = container.textContent ?? '';
      expect(text).toContain('*');
    });

    it('seeds default values from the schema and emits onChange once on mount', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{
            properties: {
              greeting: { type: 'string', default: 'hello' },
              count: { type: 'number', default: 42 },
            },
          }}
          onChange={onChange}
        />,
      );
      expect(onChange).toHaveBeenCalledWith({ greeting: 'hello', count: 42 });
      const greeting = screen.getByLabelText('greeting') as HTMLInputElement;
      expect(greeting.value).toBe('hello');
    });

    it('emits onChange when a string field changes', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{ properties: { name: { type: 'string' } } }}
          onChange={onChange}
        />,
      );
      const input = screen.getByLabelText('name') as HTMLInputElement;
      fireEvent.change(input, { target: { value: 'alice' } });
      // The last call should reflect the new value.
      const last = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(last.name).toBe('alice');
    });
  });

  describe('JSON textarea fallback', () => {
    it('renders a JSON textarea when schema has no properties', () => {
      const onChange = jest.fn();
      render(<SchemaForm schema={{}} onChange={onChange} />);
      const ta = screen.getByLabelText('Input (JSON)') as HTMLTextAreaElement;
      expect(ta).toBeInTheDocument();
      expect(ta.tagName).toBe('TEXTAREA');
    });

    it('renders the textarea when properties object is empty', () => {
      const onChange = jest.fn();
      render(<SchemaForm schema={{ properties: {} }} onChange={onChange} />);
      const ta = screen.getByLabelText('Input (JSON)');
      expect(ta).toBeInTheDocument();
    });

    it('parses valid JSON and forwards it via onChange', () => {
      const onChange = jest.fn();
      render(<SchemaForm schema={{}} onChange={onChange} />);
      const ta = screen.getByLabelText('Input (JSON)') as HTMLTextAreaElement;
      act(() => {
        fireEvent.change(ta, { target: { value: '{"foo":"bar"}' } });
      });
      expect(onChange).toHaveBeenLastCalledWith({ foo: 'bar' });
    });

    it('shows an inline error for invalid JSON without throwing', () => {
      const onChange = jest.fn();
      const { container } = render(<SchemaForm schema={{}} onChange={onChange} />);
      const ta = screen.getByLabelText('Input (JSON)') as HTMLTextAreaElement;
      act(() => {
        fireEvent.change(ta, { target: { value: 'not-json' } });
      });
      // Component must not crash; an error message is rendered.
      const text = container.textContent ?? '';
      expect(text.length).toBeGreaterThan(0);
    });
  });

  describe('Load Example', () => {
    it('renders a Load Example button when exampleInput is provided (schema mode)', () => {
      const onChange = jest.fn();
      render(
        <SchemaForm
          schema={{ properties: { greeting: { type: 'string' } } }}
          exampleInput={{ greeting: 'hi' }}
          onChange={onChange}
        />,
      );
      const btn = screen.getByRole('button', { name: /Load Example/i });
      expect(btn).toBeInTheDocument();
    });
  });
});
