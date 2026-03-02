/**
 * Home page — placeholder dashboard shell.
 *
 * This is the entry point for the UI.
 * Full dashboard content is built in subsequent issues (#304–#309).
 */
export default function HomePage() {
  return (
    <div className="flex flex-col gap-8">
      {/* Page header */}
      <section aria-labelledby="dashboard-heading">
        <h1
          id="dashboard-heading"
          className="text-2xl font-semibold tracking-tight text-zinc-100"
        >
          Dashboard
        </h1>
        <p className="mt-1 text-sm text-zinc-400">
          Monitor and manage your AI pipeline runs.
        </p>
      </section>

      {/* Status grid — placeholder cards */}
      <section
        aria-label="Pipeline status overview"
        className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4"
      >
        <StatCard label="Active Runs" value="—" status="info" />
        <StatCard label="Completed Today" value="—" status="success" />
        <StatCard label="Failed" value="—" status="error" />
        <StatCard label="Templates" value="—" status="neutral" />
      </section>

      {/* Recent runs placeholder */}
      <section aria-labelledby="recent-runs-heading">
        <h2
          id="recent-runs-heading"
          className="mb-4 text-lg font-medium text-zinc-100"
        >
          Recent Runs
        </h2>
        <div className="card flex items-center justify-center py-12 text-sm text-zinc-500">
          Pipeline run list coming in #305
        </div>
      </section>
    </div>
  );
}

/* ── Stat card sub-component ─────────────────────────────────────────────── */

type StatusVariant = 'info' | 'success' | 'error' | 'warning' | 'neutral';

interface StatCardProps {
  /** Display label */
  label: string;
  /** Current value (dash while loading) */
  value: string | number;
  /** Colour theme for the indicator dot */
  status: StatusVariant;
}

/** Small summary card for the status grid. */
function StatCard({ label, value, status }: StatCardProps) {
  const dotColors: Record<StatusVariant, string> = {
    info: 'bg-blue-500',
    success: 'bg-green-500',
    error: 'bg-red-500',
    warning: 'bg-amber-500',
    neutral: 'bg-zinc-500',
  };

  return (
    <div className="card flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span
          className={`h-2 w-2 rounded-full ${dotColors[status]}`}
          aria-hidden="true"
        />
        <span className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          {label}
        </span>
      </div>
      <p className="text-2xl font-semibold tabular-nums text-zinc-100">
        {value}
      </p>
    </div>
  );
}
