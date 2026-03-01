/**
 * TemplateCard — clickable card displayed on the dashboard template grid.
 *
 * Navigates to /templates/[id] when clicked.
 */

import Link from "next/link";
import type { TemplateSummary } from "@/lib/types";
import { Badge } from "@/components/ui/Badge";

interface Props {
  template: TemplateSummary;
}

/**
 * Render a summary card for a pipeline template.
 */
export function TemplateCard({ template }: Props) {
  return (
    <Link
      href={`/templates/${encodeURIComponent(template.id)}`}
      className={[
        "card block cursor-pointer no-underline",
        "hover:border-border-emphasis hover:bg-surface-elevated",
        "transition-colors duration-150",
        "focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2",
        "focus-visible:ring-offset-surface-canvas outline-none",
      ].join(" ")}
    >
      {/* Header */}
      <div className="mb-2 flex items-start justify-between gap-2">
        <h2 className="text-sm font-semibold text-text-primary leading-snug">
          {template.name}
        </h2>
        <Badge variant="muted" className="shrink-0">
          {template.version}
        </Badge>
      </div>

      {/* Description */}
      {template.description && (
        <p className="mb-3 text-xs text-text-secondary line-clamp-2">
          {template.description}
        </p>
      )}

      {/* Footer meta */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted">
        <span>
          {template.phases_count} phase
          {template.phases_count !== 1 ? "s" : ""}
        </span>

        {template.category && (
          <>
            <span aria-hidden="true">·</span>
            <Badge variant="info">{template.category}</Badge>
          </>
        )}

        {template.author && (
          <>
            <span aria-hidden="true">·</span>
            <span>{template.author}</span>
          </>
        )}
      </div>
    </Link>
  );
}
