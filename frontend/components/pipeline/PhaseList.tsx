/**
 * PhaseList — ordered list of pipeline phases shown on the template detail page.
 *
 * Renders phase ID, name, model tier, task type, and dependencies in a
 * visually connected vertical list.
 */

import type { PhaseDetail } from "@/lib/types";
import { Badge } from "@/components/ui/Badge";

interface Props {
  phases: PhaseDetail[];
}

/**
 * Map model_tier string to a Badge variant.
 */
function tierVariant(
  tier: string
): "success" | "warning" | "error" | "info" | "muted" {
  switch (tier.toLowerCase()) {
    case "tier1":
    case "light":
      return "success";
    case "tier2":
    case "medium":
      return "info";
    case "tier3":
    case "heavy":
      return "warning";
    default:
      return "muted";
  }
}

/**
 * Render the phase execution plan for a template.
 */
export function PhaseList({ phases }: Props) {
  if (phases.length === 0) {
    return (
      <p className="text-sm text-text-muted">No phases defined.</p>
    );
  }

  return (
    <ol className="space-y-2">
      {phases.map((phase, index) => (
        <li
          key={phase.id}
          className={[
            "flex gap-4 rounded-md border border-border bg-surface-card px-4 py-3",
            "hover:border-border-emphasis transition-colors",
          ].join(" ")}
        >
          {/* Step number */}
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-overlay text-xs font-semibold text-text-muted">
            {index + 1}
          </span>

          {/* Phase info */}
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-text-primary">
                {phase.name}
              </span>
              <code className="rounded bg-surface-elevated px-1 py-0.5 font-mono text-xs text-text-muted">
                {phase.id}
              </code>
            </div>

            {phase.description && (
              <p className="mt-0.5 text-xs text-text-secondary">
                {phase.description}
              </p>
            )}

            {/* Metadata badges */}
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {phase.model_tier && (
                <Badge variant={tierVariant(phase.model_tier)}>
                  {phase.model_tier}
                </Badge>
              )}
              {phase.task_type && (
                <Badge variant="muted">{phase.task_type}</Badge>
              )}
              {phase.thinking_level && phase.thinking_level !== "none" && (
                <Badge variant="muted">
                  thinking: {phase.thinking_level}
                </Badge>
              )}
              {phase.depends_on.length > 0 && (
                <span className="text-xs text-text-muted">
                  depends on: {phase.depends_on.join(", ")}
                </span>
              )}
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}
