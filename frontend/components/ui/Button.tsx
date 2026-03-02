import React from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Visual style of the button. */
export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';

/** Relative size of the button. */
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual variant. Defaults to `primary`. */
  variant?: ButtonVariant;
  /** Size preset. Defaults to `md`. */
  size?: ButtonSize;
  /** When true, renders a spinner and disables the button. */
  loading?: boolean;
  /** Content inside the button. */
  children: React.ReactNode;
}

// ---------------------------------------------------------------------------
// Style maps — Tailwind classes only, no inline styles
// ---------------------------------------------------------------------------

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    'bg-brand-500 text-white hover:bg-brand-400 active:bg-brand-600 ' +
    'focus-visible:ring-brand-500',
  secondary:
    'bg-surface-2 text-content-primary border border-surface-3 ' +
    'hover:bg-surface-3 active:bg-surface-4 focus-visible:ring-brand-500',
  ghost:
    'bg-transparent text-content-secondary ' +
    'hover:bg-surface-2 hover:text-content-primary active:bg-surface-3 ' +
    'focus-visible:ring-brand-500',
  danger:
    'bg-error text-white hover:bg-red-400 active:bg-red-700 ' +
    'focus-visible:ring-red-500',
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-xs rounded-md gap-1.5',
  md: 'px-4 py-2 text-sm rounded-lg gap-2',
  lg: 'px-5 py-2.5 text-base rounded-xl gap-2.5',
};

// ---------------------------------------------------------------------------
// Spinner sub-component
// ---------------------------------------------------------------------------

function Spinner({ size }: { size: ButtonSize }) {
  const spinnerSize =
    size === 'sm' ? 'h-3 w-3' : size === 'lg' ? 'h-5 w-5' : 'h-4 w-4';

  return (
    <svg
      className={`${spinnerSize} animate-spin`}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Button component
// ---------------------------------------------------------------------------

/**
 * Reusable Button primitive.
 *
 * Supports four visual variants (primary, secondary, ghost, danger),
 * three sizes (sm, md, lg), a loading state, and full disabled support.
 * Forwards refs to the underlying <button> element.
 *
 * @example
 * <Button variant="primary" size="md" onClick={handleClick}>
 *   Save changes
 * </Button>
 *
 * @example
 * <Button variant="danger" loading>Deleting…</Button>
 */
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = 'primary',
      size = 'md',
      loading = false,
      disabled,
      className = '',
      children,
      ...rest
    },
    ref,
  ) => {
    const isDisabled = disabled || loading;

    const baseClasses =
      'inline-flex items-center justify-center font-medium transition-colors ' +
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 ' +
      'focus-visible:ring-offset-surface-1 ' +
      'disabled:pointer-events-none disabled:opacity-50';

    return (
      <button
        ref={ref}
        disabled={isDisabled}
        aria-disabled={isDisabled}
        aria-busy={loading}
        className={[baseClasses, variantClasses[variant], sizeClasses[size], className]
          .filter(Boolean)
          .join(' ')}
        {...rest}
      >
        {loading && <Spinner size={size} />}
        {children}
      </button>
    );
  },
);

Button.displayName = 'Button';
