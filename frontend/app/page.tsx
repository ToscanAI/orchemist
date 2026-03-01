/**
 * Dashboard page — template list.
 *
 * Fetches all available pipeline templates from GET /api/templates and renders
 * them as a card grid.  Clicking a card navigates to the template detail page
 * where the user can inspect phases and launch a run.
 */

"use client";

import { useEffect, useState } from "react";
import { listTemplates, ApiError } from "@/lib/api";
import type { TemplateSummary } from "@/lib/types";
import { TemplateCard } from "@/components/pipeline/TemplateCard";

export default function DashboardPage() {
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    listTemplates()
      .then((data) => {
        if (!cancelled) {
          setTemplates(data);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message =
            err instanceof ApiError
              ? `API error ${err.status}: ${err.body}`
              : String(err);
          setError(message);
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div>
      {/* Page header */}
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-text-primary">
          Pipeline Templates
        </h1>
        <p className="mt-1 text-sm text-text-secondary">
          Select a template to view its phases and launch a run.
        </p>
      </div>

      {/* Loading state */}
      {loading && (
        <div className="flex items-center gap-3 text-text-secondary">
          <div className="spinner h-5 w-5" />
          <span className="text-sm">Loading templates…</span>
        </div>
      )}

      {/* Error state */}
      {!loading && error && (
        <div className="rounded-md border border-status-error/30 bg-status-error/10 p-4 text-sm text-status-error">
          <p className="font-medium">Failed to load templates</p>
          <p className="mt-1 text-xs opacity-80">{error}</p>
          <p className="mt-2 text-xs text-text-muted">
            Is <code className="font-mono">orch serve</code> running?
          </p>
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && templates.length === 0 && (
        <div className="rounded-md border border-border bg-surface-card p-8 text-center text-sm text-text-secondary">
          <p className="font-medium">No templates found</p>
          <p className="mt-1 text-xs">
            Place YAML pipeline templates in{" "}
            <code className="font-mono">~/.orchestration-engine/templates/</code>{" "}
            or the current working directory.
          </p>
        </div>
      )}

      {/* Template grid */}
      {!loading && !error && templates.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {templates.map((template) => (
            <TemplateCard key={template.id} template={template} />
          ))}
        </div>
      )}
    </div>
  );
}
