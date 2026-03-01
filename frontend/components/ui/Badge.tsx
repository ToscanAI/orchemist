/**
 * Badge — small inline status/label pill.
 *
 * Variants map to semantic colours defined in tailwind.config.ts.
 */

import clsx from "clsx";

export type BadgeVariant =
  | "success"
  | "warning"
  | "error"
  | "info"
  | "running"
  | "muted";

interface BadgeProps {
  variant?: BadgeVariant;
  children: React.ReactNode;
  className?: string;
}

const variantClasses: Record<BadgeVariant, string> = {
  success: "badge-success",
  warning: "badge-warning",
  error: "badge-error",
  info: "badge-info",
  running: "badge-running",
  muted: "badge-muted",
};

/**
 * Render a small status badge.
 *
 * @example
 * <Badge variant="success">Completed</Badge>
 * <Badge variant="error">Failed</Badge>
 */
export function Badge({
  variant = "muted",
  children,
  className,
}: BadgeProps) {
  return (
    <span className={clsx("badge", variantClasses[variant], className)}>
      {children}
    </span>
  );
}
