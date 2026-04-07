'use client';

/**
 * Template detail page — `/templates/[id]`.
 *
 * Displays full template metadata, the ordered phase execution plan, and a
 * launch form that starts a pipeline run in the chosen mode.
 *
 * Client component: uses `useState`, `useEffect`, and `useRouter` for
 * runtime data fetching and form handling. The app uses `output: 'export'`
 * (static export) in next.config.js, so all data fetching must be
 * client-side — `generateStaticParams` returns `[]` to satisfy the static
 * export requirement for dynamic `[id]` segments.
 *
 * @module
 */

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter, useParams } from 'next/navigation';
import { getTemplate, startRun, deleteTemplate, duplicateTemplate, ApiError } from '@/lib/api';
import type { TemplateDetail, RunMode } from '@/lib/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { PhaseList } from '@/components/pipeline/PhaseList';
import { SchemaForm } from '@/components/pipeline/SchemaForm';
import { ProviderSelector } from '@/components/pipeline/ProviderSelector';
import { PhaseModelMap } from '@/components/pipeline/PhaseModelMap';

// ---------------------------------------------------------------------------
// Static params — required for output: 'export' with dynamic segments
// ---------------------------------------------------------------------------

/**
 * Tells Next.js static export that there are no pre-rendered paths for
 * `/templates/[id]`. The page is rendered entirely client-side at runtime
 * via `useEffect` + API fetch. Returning an empty array prevents the build
 * error: "Page is missing generateStaticParams()".
 * NOTE: moved to layout.tsx — 'use client' pages cannot export generateStaticParams.
 */

// ---------------------------------------------------------------------------
// Run mode options
// ---------------------------------------------------------------------------

const MODES: RunMode[] = ['dry-run', 'standalone', 'openclaw', 'openrouter'];

// ---------------------------------------------------------------------------
// TemplateDetailPage
// ---------------------------------------------------------------------------

/**
 * Template detail page component.
 *
 * On mount, decodes the `id` URL segment and fetches the full template from
 * the API. Renders:
 *  - Loading spinner (same pattern as `page.tsx`)
 *  - Error card on fetch failure
 *  - Template metadata: name, version, description, author, tags
 *  - Phase execution plan via `<PhaseList>`
 *  - Launch form: mode selector + JSON input + submit button
 *
 * On submit, calls `startRun` with the parsed JSON payload and navigates
 * to `/runs/{run_id}` on success.
 *
 * `useParams` (App Router hook) is used instead of a `params` prop because
 * in `output: 'export'` mode, client components do not receive `params` at
 * runtime — only server components do at build time.
 */
export default function TemplateDetailClient() {
  const params = useParams<{ id: string }>();
  const router = useRouter();

  // In static export mode, useParams may return the placeholder ("_") from
  // generateStaticParams instead of the real URL segment. Fall back to
  // reading the last path segment from the browser URL.
  const rawId = params.id && params.id !== '_' ? params.id : (() => {
    if (typeof window === 'undefined') return '_';
    const segments = window.location.pathname.split('/').filter(Boolean);
    return segments[segments.length - 1] ?? '_';
  })();
  const id = decodeURIComponent(rawId);

  // ── Data fetch state ──────────────────────────────────────────────────────
  const [template, setTemplate] = useState<TemplateDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // ── Launch form state ─────────────────────────────────────────────────────
  const [selectedMode, setSelectedMode] = useState<RunMode>('dry-run');
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});
  const [apiKey, setApiKey] = useState<string>('');
  const [modelMap, setModelMap] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [apiError, setApiError] = useState<string | null>(null);

  // ── CRUD action state ─────────────────────────────────────────────────────
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [duplicating, setDuplicating] = useState(false);

  // ── Fetch template on mount ───────────────────────────────────────────────
  useEffect(() => {
    // Guard against state updates after unmount to avoid React warnings.
    let cancelled = false;

    getTemplate(id)
      .then((data) => {
        if (!cancelled) {
          setTemplate(data);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          if (err instanceof ApiError) {
            setFetchError(err.message);
          } else if (err instanceof Error) {
            setFetchError(err.message);
          } else {
            setFetchError('An unexpected error occurred.');
          }
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [id]);

  // ── Form handlers ─────────────────────────────────────────────────────────

  /**
   * Switches the active run mode and clears any stale `apiError`.
   */
  function handleModeChange(mode: RunMode) {
    setSelectedMode(mode);
    setApiError(null);
  }

  /**
   * Submits the launch form:
   * 1. Parses JSON (defence-in-depth — `jsonError` may be stale).
   * 2. Calls `startRun` with template ID, mode, and parsed payload.
   * 3. Navigates to `/runs/{run_id}` on success.
   * 4. Sets `apiError` and re-enables the button on failure.
   */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!template || submitting) return;

    setSubmitting(true);
    setApiError(null);

    try {
      const run = await startRun({
        template: template.id,
        mode: selectedMode,
        input: { ...formValues },
        ...(apiKey ? { api_key: apiKey } : {}),
        ...(Object.keys(modelMap).length > 0 ? { model_map: modelMap } : {}),
      });
      setApiKey('');
      router.push(`/runs/${run.run_id}`);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setApiError(err.message);
      } else if (err instanceof Error) {
        setApiError(err.message);
      } else {
        setApiError('Failed to launch run.');
      }
      setSubmitting(false);
    }
  }

  // ── Loading state ─────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-4 py-16 text-zinc-400"
        role="status"
        aria-live="polite"
      >
        {/* Accessible spinner — same markup as page.tsx for visual consistency */}
        <svg
          className="h-8 w-8 animate-spin text-sky-500"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
          />
        </svg>
        <span className="text-sm">Loading template…</span>
      </div>
    );
  }

  // ── Fetch error state ─────────────────────────────────────────────────────
  if (fetchError !== null || template === null) {
    return (
      <div className="flex flex-col gap-4">
        <Link
          href="/templates"
          className="text-sm text-sky-400 hover:text-sky-300 self-start"
        >
          ← Back to templates
        </Link>
        <div
          className="card border-red-500/50 bg-red-900/10"
          role="alert"
        >
          <p className="text-sm font-medium text-red-400">
            {fetchError ?? 'Template not found.'}
          </p>
        </div>
      </div>
    );
  }

  // ── Loaded state ──────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-8">
      {/* Back navigation */}
      <Link
        href="/templates"
        className="text-sm text-sky-400 hover:text-sky-300 self-start"
      >
        ← Back to templates
      </Link>

      {/* ── Template metadata ────────────────────────────────────────────── */}
      <section aria-labelledby="template-heading">
        <div className="flex flex-wrap items-center gap-3">
          <h1
            id="template-heading"
            className="text-2xl font-semibold tracking-tight text-zinc-100"
          >
            {template.name}
          </h1>
          <Badge variant="neutral">v{template.version}</Badge>
          {template.source === 'bundled' && (
            <Badge variant="warning">Built-in</Badge>
          )}
        </div>

        <p className="mt-2 text-sm text-zinc-400">{template.description}</p>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
          <span>By {template.author}</span>
          {template.tags.map((tag) => (
            <Badge key={tag} variant="info">
              {tag}
            </Badge>
          ))}
        </div>

        {/* ── Action buttons ──────────────────────────────────────────── */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {template.source !== 'bundled' ? (
            <Link href={`/templates/${encodeURIComponent(id)}/edit`}>
              <Button variant="secondary" size="sm">
                ✏️ Edit
              </Button>
            </Link>
          ) : (
            <span title="Built-in templates cannot be edited">
              <Button variant="secondary" size="sm" disabled>
                ✏️ Edit
              </Button>
            </span>
          )}

          <Button
            variant="secondary"
            size="sm"
            loading={duplicating}
            disabled={duplicating}
            onClick={async () => {
              setDuplicating(true);
              setApiError(null);
              try {
                const dup = await duplicateTemplate(id);
                router.push(`/templates/${encodeURIComponent(dup.id)}`);
              } catch (err: unknown) {
                setApiError(
                  err instanceof Error ? err.message : 'Failed to duplicate.',
                );
                setDuplicating(false);
              }
            }}
          >
            {duplicating ? 'Duplicating…' : '📋 Duplicate'}
          </Button>

          {template.source !== 'bundled' ? (
            <Button
              variant="danger"
              size="sm"
              onClick={() => setShowDeleteModal(true)}
            >
              🗑️ Delete
            </Button>
          ) : (
            <span title="Built-in templates cannot be deleted">
              <Button variant="danger" size="sm" disabled>
                🗑️ Delete
              </Button>
            </span>
          )}
        </div>
      </section>

      {/* ── Delete confirmation modal ────────────────────────────────────── */}
      {showDeleteModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="card max-w-md w-full mx-4 flex flex-col gap-4">
            <h2 className="text-lg font-semibold text-zinc-100">
              Delete Template
            </h2>
            <p className="text-sm text-zinc-400">
              Are you sure you want to delete{' '}
              <span className="text-zinc-200 font-medium">{template.name}</span>?
              This action cannot be undone.
            </p>
            {apiError && (
              <div className="rounded-lg bg-red-900/10 border border-red-500/50 px-3 py-2" role="alert">
                <p className="text-xs text-red-400">{apiError}</p>
              </div>
            )}
            <div className="flex justify-end gap-3">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setShowDeleteModal(false);
                  setApiError(null);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                size="sm"
                loading={deleting}
                disabled={deleting}
                onClick={async () => {
                  setDeleting(true);
                  setApiError(null);
                  try {
                    await deleteTemplate(id);
                    router.push('/templates');
                  } catch (err: unknown) {
                    setApiError(
                      err instanceof Error ? err.message : 'Failed to delete.',
                    );
                    setDeleting(false);
                  }
                }}
              >
                {deleting ? 'Deleting…' : 'Delete'}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* ── Phase execution plan ─────────────────────────────────────────── */}
      <section aria-labelledby="phases-heading">
        <h2
          id="phases-heading"
          className="mb-3 text-base font-semibold text-zinc-200"
        >
          Phase Execution Plan
        </h2>
        <PhaseList phases={template.phases} />
      </section>

      {/* ── Launch form ──────────────────────────────────────────────────── */}
      <section aria-labelledby="launch-heading">
        <h2
          id="launch-heading"
          className="mb-3 text-base font-semibold text-zinc-200"
        >
          Launch Run
        </h2>

        <form
          onSubmit={handleSubmit}
          className="card flex flex-col gap-5"
          noValidate
        >
          {/* Mode selector */}
          <div className="flex flex-col gap-2">
            <label className="text-xs font-medium text-zinc-400">Mode</label>
            <div
              className="flex flex-wrap gap-2"
              role="group"
              aria-label="Run mode"
            >
              {MODES.map((mode) => (
                <Button
                  key={mode}
                  type="button"
                  variant={selectedMode === mode ? 'primary' : 'secondary'}
                  size="sm"
                  onClick={() => handleModeChange(mode)}
                  aria-pressed={selectedMode === mode}
                >
                  {mode}
                </Button>
              ))}
            </div>
          </div>

          {/* Provider credentials */}
          <ProviderSelector
            mode={selectedMode}
            apiKey={apiKey}
            onApiKeyChange={setApiKey}
          />

          {/* Schema-driven input form */}
          <SchemaForm
            schema={template.config_schema ?? {}}
            exampleInput={template.example_input}
            onChange={(values) => {
              setFormValues(values);
              setApiError(null);
            }}
          />

          {/* Phase model assignments */}
          {(selectedMode === 'standalone' || selectedMode === 'openrouter') && template.phases.length > 0 && (
            <PhaseModelMap
              phases={template.phases}
              modelMap={modelMap}
              onModelMapChange={setModelMap}
            />
          )}

          {/* API error */}
          {apiError !== null && (
            <div
              className="rounded-lg bg-red-900/10 border border-red-500/50 px-3 py-2"
              role="alert"
            >
              <p className="text-xs text-red-400">{apiError}</p>
            </div>
          )}

          {/* Submit */}
          <Button
            type="submit"
            variant="primary"
            loading={submitting}
            disabled={submitting}
          >
            {submitting ? 'Launching…' : 'Launch Run'}
          </Button>
        </form>
      </section>
    </div>
  );
}
