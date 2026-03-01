/**
 * RunStatusBadge — maps a RunStatus string to a styled Badge.
 */

import type { RunStatus } from "@/lib/types";
import type { BadgeVariant } from "@/components/ui/Badge";
import { Badge } from "@/components/ui/Badge";

interface Props {
  status: RunStatus | string;
}

function statusVariant(status: string): BadgeVariant {
  switch (status) {
    case "completed":
      return "success";
    case "running":
    case "starting":
      return "running";
    case "paused":
      return "warning";
    case "error":
    case "aborted":
      return "error";
    case "cancelled":
      return "muted";
    default:
      return "muted";
  }
}

const STATUS_LABELS: Record<string, string> = {
  starting: "Starting",
  running: "Running",
  paused: "Paused",
  completed: "Completed",
  aborted: "Aborted",
  error: "Error",
  cancelled: "Cancelled",
};

/**
 * Render a coloured status badge for a pipeline run.
 */
export function RunStatusBadge({ status }: Props) {
  const label = STATUS_LABELS[status] ?? status;
  return <Badge variant={statusVariant(status)}>{label}</Badge>;
}
