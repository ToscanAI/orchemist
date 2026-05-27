/**
 * Shared error banner — replaces ad-hoc `role="status"` error divs across
 * dashboard, templates list, runs list, template detail, and run detail pages.
 *
 * Uses `role="alert"` (the W3C-correct role for error announcements) and a
 * consistent harness-token visual treatment. Renders nothing when the message
 * is null/empty/undefined so callers can pass `error` state directly without
 * conditional wrappers.
 *
 * Issue: #775
 */

export interface ErrorBannerProps {
  /** Error message text. When null/empty/undefined the banner renders nothing. */
  message: string | null | undefined;
  /** Optional extra classes for layout adjustments. */
  className?: string;
}

export function ErrorBanner({ message, className = '' }: ErrorBannerProps) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className={[
        'rounded-md border border-red-500/40 bg-red-900/20 px-4 py-3 text-sm text-red-300',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {message}
    </div>
  );
}
