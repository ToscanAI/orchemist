'use client';

/**
 * Global error boundary for the Orchemist Web UI.
 *
 * Catches unhandled rendering errors in any page component and displays
 * a recovery UI instead of a white screen.
 *
 * @see https://nextjs.org/docs/app/building-your-application/routing/error-handling
 * @module
 */

interface ErrorBoundaryProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ErrorBoundary({ error, reset }: ErrorBoundaryProps) {
  // Log for debugging
  // eslint-disable-next-line no-console
  console.error('[Orchemist] Uncaught rendering error:', error);

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-6 px-4 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-red-500/10">
        <svg
          className="h-8 w-8 text-red-400"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
          />
        </svg>
      </div>

      <div className="flex flex-col gap-2">
        <h2 className="text-xl font-semibold text-zinc-100">Something went wrong</h2>
        <p className="max-w-md text-sm text-zinc-400">
          An unexpected error occurred while rendering this page.
        </p>
        {error.message && (
          <pre className="mt-2 max-w-lg overflow-auto rounded-md border border-zinc-700 bg-zinc-900 px-4 py-2 text-left text-xs text-red-400">
            {error.message}
          </pre>
        )}
      </div>

      <button
        onClick={reset}
        className="rounded-lg bg-sky-600 px-6 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500 focus:ring-offset-2 focus:ring-offset-zinc-950"
      >
        Try Again
      </button>
    </div>
  );
}
