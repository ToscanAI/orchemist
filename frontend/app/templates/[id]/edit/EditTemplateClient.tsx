'use client';

/**
 * Edit template page — `/templates/[id]/edit`.
 *
 * Loads the existing template's YAML content and provides a form for editing.
 */

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter, useParams } from 'next/navigation';
import { getTemplate, updateTemplate, extractApiErrorMessage } from '@/lib/api';
import type { TemplateDetail } from '@/lib/types';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { useStaticExportParam } from '@/hooks/useStaticExportParam';

export default function EditTemplateClient() {
  const params = useParams<{ id: string }>();
  const router = useRouter();

  // In static export mode, resolve the real ID from the URL. The shared hook
  // (#774) takes `segmentIndexFromEnd: 1` because the URL is
  // `/templates/{id}/edit` — the id is one segment from the end.
  const id = useStaticExportParam(params.id, { segmentIndexFromEnd: 1 });

  const [template, setTemplate] = useState<TemplateDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [yamlContent, setYamlContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    getTemplate(id)
      .then((data) => {
        if (!cancelled) {
          setTemplate(data);
          setYamlContent(data.yaml_content ?? '');
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setFetchError(
            err instanceof Error ? err.message : 'Failed to load template.',
          );
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [id]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !template) return;

    setSubmitting(true);
    setError(null);

    try {
      await updateTemplate(id, {
        content: yamlContent,
        source: (template.source as 'user' | 'project') ?? 'project',
      });
      router.push(`/templates/${encodeURIComponent(id)}`);
    } catch (err: unknown) {
      setError(extractApiErrorMessage(err));
      setSubmitting(false);
    }
  }

  if (loading) {
    return <Spinner message="Loading template…" />;
  }

  if (fetchError !== null || template === null) {
    return (
      <div className="flex flex-col gap-4">
        <Link
          href="/templates"
          className="text-sm text-sky-400 hover:text-sky-300 self-start"
        >
          ← Back to templates
        </Link>
        <div className="card border-red-500/50 bg-red-900/10" role="alert">
          <p className="text-sm font-medium text-red-400">
            {fetchError ?? 'Template not found.'}
          </p>
        </div>
      </div>
    );
  }

  const isReadOnly = template.source === 'bundled';

  return (
    <div className="flex flex-col gap-6">
      {/* Back navigation */}
      <Link
        href={`/templates/${encodeURIComponent(id)}`}
        className="text-sm text-sky-400 hover:text-sky-300 self-start"
      >
        ← Back to {template.name}
      </Link>

      <section>
        <h1 className="text-2xl font-semibold tracking-tight text-content-primary">
          Edit Template
        </h1>
        <p className="mt-1 text-sm text-content-secondary">
          Editing <span className="text-content-primary font-medium">{template.name}</span>
        </p>
      </section>

      {isReadOnly && (
        <div
          className="rounded-lg bg-yellow-900/10 border border-yellow-500/50 px-3 py-2"
          role="alert"
        >
          <p className="text-xs text-yellow-400">
            This is a built-in template and cannot be edited. Duplicate it first
            to create an editable copy.
          </p>
        </div>
      )}

      <form onSubmit={handleSubmit} className="card flex flex-col gap-5" noValidate>
        {/* YAML Editor */}
        <div className="flex flex-col gap-2">
          <label
            htmlFor="yaml-editor"
            className="text-xs font-medium text-content-secondary"
          >
            Template YAML
          </label>
          <textarea
            id="yaml-editor"
            value={yamlContent}
            onChange={(e) => {
              setYamlContent(e.target.value);
              setError(null);
            }}
            rows={24}
            spellCheck={false}
            readOnly={isReadOnly}
            className="w-full rounded-md border border-default bg-surface-0 px-3 py-2 font-mono text-sm text-content-primary placeholder:text-content-tertiary focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 resize-y disabled:opacity-50"
            placeholder="Template YAML content..."
          />
        </div>

        {/* Error */}
        {error !== null && (
          <div
            className="rounded-lg bg-red-900/10 border border-red-500/50 px-3 py-2"
            role="alert"
          >
            <p className="text-xs text-red-400 whitespace-pre-wrap">{error}</p>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3">
          <Button
            type="submit"
            variant="primary"
            loading={submitting}
            disabled={submitting || isReadOnly || !yamlContent.trim()}
          >
            {submitting ? 'Saving…' : 'Save Changes'}
          </Button>
          <Link href={`/templates/${encodeURIComponent(id)}`}>
            <Button type="button" variant="secondary">
              Cancel
            </Button>
          </Link>
        </div>
      </form>
    </div>
  );
}
