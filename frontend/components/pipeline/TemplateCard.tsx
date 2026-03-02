/**
 * TemplateCard — display a single pipeline template summary as a navigable card.
 *
 * Clicking anywhere on the card navigates to `/templates/[id]` using next/link
 * (client-side navigation, no full page reload).
 *
 * Displays:
 *  - Template name (heading)
 *  - Description (body text)
 *  - Version badge (neutral)
 *  - Category badge (info)
 *  - Phase count and author (footer metadata)
 *
 * @module
 */

import Link from 'next/link';
import type { TemplateSummary } from '@/lib/types';
import { Badge } from '@/components/ui/Badge';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface TemplateCardProps {
  /** Template summary data from the API. */
  template: TemplateSummary;
}

// ---------------------------------------------------------------------------
// TemplateCard component
// ---------------------------------------------------------------------------

/**
 * Card component for a single pipeline template.
 *
 * Uses the `card` class from globals.css for consistent surface styling.
 * Wraps in a `next/link` block for SPA navigation to the detail page.
 *
 * @example
 * <TemplateCard template={summary} />
 */
export function TemplateCard({ template }: TemplateCardProps) {
  return (
    <Link
      href={`/templates/${encodeURIComponent(template.id)}`}
      className="card group flex flex-col gap-3 transition-colors hover:border-zinc-600 hover:bg-zinc-800/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500"
      aria-label={`View template: ${template.name}`}
    >
      {/* Card header: name + badges */}
      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-zinc-100 group-hover:text-white">
          {template.name}
        </h2>

        {/* Version and category badges */}
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="neutral">v{template.version}</Badge>
          <Badge variant="info">{template.category}</Badge>
        </div>
      </div>

      {/* Description */}
      <p className="flex-1 text-xs leading-relaxed text-zinc-400 line-clamp-3">
        {template.description}
      </p>

      {/* Footer metadata: phases count + author */}
      <div className="flex items-center justify-between text-xs text-zinc-500">
        <span>
          {template.phases_count} phase{template.phases_count !== 1 ? 's' : ''}
        </span>
        <span className="truncate max-w-[120px]" title={template.author}>
          {template.author}
        </span>
      </div>
    </Link>
  );
}
