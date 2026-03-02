import React from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Semantic status variant for the Badge. */
export type BadgeVariant = 'success' | 'warning' | 'error' | 'info' | 'neutral';

export interface BadgeProps {
  /** Status variant controls colour. Defaults to `neutral`. */
  variant?: BadgeVariant;
  /** Badge label text or content. */
  children: React.ReactNode;
  /** Additional Tailwind class overrides. */
  className?: string;
}

// ---------------------------------------------------------------------------
// CSS class map — delegates to @layer components in globals.css
// ---------------------------------------------------------------------------

const variantClass: Record<BadgeVariant, string> = {
  success: 'badge-success',
  warning: 'badge-warning',
  error: 'badge-error',
  info: 'badge-info',
  neutral: 'badge-neutral',
};

// ---------------------------------------------------------------------------
// Badge component
// ---------------------------------------------------------------------------

/**
 * Status Badge primitive.
 *
 * Renders a pill-shaped badge using the pre-defined `.badge-*` component
 * classes from `globals.css` (@layer components). This ensures the badge
 * styling is consistent with any server-rendered or non-React usage of
 * those classes.
 *
 * @example
 * <Badge variant="success">Running</Badge>
 * <Badge variant="error">Failed</Badge>
 * <Badge variant="neutral">Pending</Badge>
 */
export const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  ({ variant = 'neutral', className = '', children, ...rest }, ref) => {
    return (
      <span
        ref={ref}
        className={[variantClass[variant], className].filter(Boolean).join(' ')}
        {...rest}
      >
        {children}
      </span>
    );
  },
);

Badge.displayName = 'Badge';
