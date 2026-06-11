'use client';

/**
 * Templates list page — `/templates`.
 *
 * Displays all available pipeline templates as a card grid with search.
 */

import { useState, useMemo } from 'react';
import Link from 'next/link';
import { listTemplates } from '@/lib/api';
import { useApi } from '@/lib/useApi';
import type { TemplateSummary } from '@/lib/types';
import { TemplateCard } from '@/components/pipeline/TemplateCard';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { ErrorBanner } from '@/components/ui/ErrorBanner';
import { HarnessShell } from '@/components/harness/HarnessShell';

export default function TemplatesPage() {
  // #870 — migrated to useApi. Single-shot fetch; loading/error/data states
  // map 1:1 to the hand-rolled closure that lived here previously.
  const { data, error, loading } = useApi<TemplateSummary[]>(() => listTemplates(), []);
  const templates: readonly TemplateSummary[] = data ?? [];
  const errorMessage: string | null = error ? error.message : null;
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    if (!search.trim()) return templates;
    const q = search.toLowerCase();
    return templates.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q) ||
        (t.description ?? '').toLowerCase().includes(q)
    );
  }, [templates, search]);

  return (
    <HarnessShell
      title="Pipeline templates · YAML source of truth"
      screenIndex={2}
      breadcrumb={[{ label: 'Fleet', href: '/' }, { label: 'Templates' }]}
    >
    <div className="flex flex-col gap-6">
      <section>
        {/* h2, not h1 — the TopBar already renders the page's single h1. */}
        <h2 className="text-2xl font-semibold tracking-tight text-content-primary">
          Templates
          {!loading && !errorMessage && templates.length > 0 && (
            <span className="ml-2 text-base font-normal text-content-tertiary">({templates.length})</span>
          )}
        </h2>
        <p className="mt-1 text-sm text-content-secondary">
          Browse and launch pipeline templates.
        </p>
      </section>

      {/* Search + Create */}
      {!loading && !errorMessage && (
        <div className="flex items-center gap-3">
          {templates.length > 0 && (
            <input
              type="text"
              placeholder="Search templates..."
              aria-label="Search templates"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full max-w-sm rounded-md border border-default bg-surface-0 px-3 py-2 text-sm text-content-primary placeholder:text-content-tertiary focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
            />
          )}
          <Link href="/templates/new" className="ml-auto shrink-0">
            <Button variant="primary" size="sm">
              + Create Template
            </Button>
          </Link>
        </div>
      )}

      {/* States */}
      {loading && <Spinner message="Loading templates..." />}

      {!loading && errorMessage && (
        <ErrorBanner message={`${errorMessage} — is orch serve running?`} />
      )}

      {!loading && !errorMessage && filtered.length === 0 && (
        <div className="card flex flex-col items-center justify-center gap-2 py-12 text-center" role="status">
          <p className="text-sm text-content-secondary">
            {search ? 'No templates match your search.' : 'No templates found.'}
          </p>
        </div>
      )}

      {!loading && !errorMessage && filtered.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((t) => (
            <TemplateCard key={t.id} template={t} />
          ))}
        </div>
      )}
    </div>
    </HarnessShell>
  );
}
