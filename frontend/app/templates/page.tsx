'use client';

/**
 * Templates list page — `/templates`.
 *
 * Displays all available pipeline templates as a card grid with search.
 */

import { useState, useEffect, useMemo } from 'react';
import Link from 'next/link';
import { listTemplates, deleteTemplate, ApiError } from '@/lib/api';
import type { TemplateSummary } from '@/lib/types';
import { TemplateCard } from '@/components/pipeline/TemplateCard';
import { Button } from '@/components/ui/Button';

export default function TemplatesPage() {
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  useEffect(() => {
    let cancelled = false;
    listTemplates()
      .then((data) => { if (!cancelled) { setTemplates(data); setLoading(false); } })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load templates.');
          setLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, []);

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
    <div className="flex flex-col gap-6">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-100">
          Templates{!loading && !error && templates.length > 0 && (
            <span className="ml-2 text-base font-normal text-zinc-500">({templates.length})</span>
          )}
        </h1>
        <p className="mt-1 text-sm text-zinc-400">
          Browse and launch pipeline templates.
        </p>
      </section>

      {/* Search + Create */}
      {!loading && !error && (
        <div className="flex items-center gap-3">
          {templates.length > 0 && (
            <input
              type="text"
              placeholder="Search templates..."
              aria-label="Search templates"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full max-w-sm rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
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
      {loading && (
        <div className="flex flex-col items-center justify-center gap-4 py-16 text-zinc-400" role="status">
          <svg className="h-8 w-8 animate-spin text-sky-500" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
          <span className="text-sm">Loading templates...</span>
        </div>
      )}

      {!loading && error && (
        <div className="card border-red-500/50 bg-red-900/10" role="status">
          <p className="text-sm font-medium text-red-400">{error}</p>
          <p className="mt-1 text-xs text-zinc-500">Is orch serve running?</p>
        </div>
      )}

      {!loading && !error && filtered.length === 0 && (
        <div className="card flex flex-col items-center justify-center gap-2 py-12 text-center" role="status">
          <p className="text-sm text-zinc-400">
            {search ? 'No templates match your search.' : 'No templates found.'}
          </p>
        </div>
      )}

      {!loading && !error && filtered.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((t) => (
            <TemplateCard key={t.id} template={t} />
          ))}
        </div>
      )}
    </div>
  );
}
