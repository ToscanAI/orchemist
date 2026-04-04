/**
 * Shared loading spinner component.
 *
 * Replaces the duplicated SVG spinner across dashboard, templates, and detail pages.
 */

interface SpinnerProps {
  /** Optional message below the spinner. */
  message?: string;
  /** Size class for the spinner SVG. Default: 'h-8 w-8'. */
  size?: string;
}

export function Spinner({ message, size = 'h-8 w-8' }: SpinnerProps) {
  return (
    <div
      className="flex flex-col items-center justify-center gap-4 py-16 text-zinc-400"
      role="status"
      aria-live="polite"
    >
      <svg
        className={`${size} animate-spin text-sky-500`}
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
          d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
        />
      </svg>
      {message && <span className="text-sm">{message}</span>}
    </div>
  );
}
