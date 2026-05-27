'use client';

/**
 * Create template page — `/templates/new`.
 *
 * Provides a form with a raw YAML editor for creating new pipeline templates.
 */

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { createTemplate, extractApiErrorMessage } from '@/lib/api';
import { Button } from '@/components/ui/Button';

const DEFAULT_YAML = `id: my-new-template
name: "My New Template"
version: "1.0.0"
description: "A new pipeline template."
author: ""
phases:
  - id: phase-one
    name: "Phase One"
    description: "First phase of the pipeline."
    model_tier: haiku
    task_type: generate
    prompt: |
      Process the input: {input}
`;

export default function CreateTemplatePage() {
  const router = useRouter();

  const [yamlContent, setYamlContent] = useState(DEFAULT_YAML);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;

    setSubmitting(true);
    setError(null);

    try {
      const result = await createTemplate({
        content: yamlContent,
        source: 'user',
      });
      router.push(`/templates/${encodeURIComponent(result.id)}`);
    } catch (err: unknown) {
      setError(extractApiErrorMessage(err));
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Back navigation */}
      <Link
        href="/templates"
        className="text-sm text-sky-400 hover:text-sky-300 self-start"
      >
        ← Back to templates
      </Link>

      <section>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-100">
          Create Template
        </h1>
        <p className="mt-1 text-sm text-zinc-400">
          Define a new pipeline template using YAML.
        </p>
      </section>

      <form onSubmit={handleSubmit} className="card flex flex-col gap-5" noValidate>
        {/* YAML Editor */}
        <div className="flex flex-col gap-2">
          <label
            htmlFor="yaml-editor"
            className="text-xs font-medium text-zinc-400"
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
            className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 resize-y"
            placeholder="Paste or write your template YAML here..."
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
            disabled={submitting || !yamlContent.trim()}
          >
            {submitting ? 'Creating…' : 'Create Template'}
          </Button>
          <Link href="/templates">
            <Button type="button" variant="secondary">
              Cancel
            </Button>
          </Link>
        </div>
      </form>
    </div>
  );
}
