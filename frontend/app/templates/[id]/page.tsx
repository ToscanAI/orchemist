/**
 * Template detail page — shows phase plan and provides a run launch form.
 *
 * Route: /templates/[id]
 *
 * Fetches the full template detail from GET /api/templates/:id and renders:
 *   - Template metadata (name, version, description, author, tags)
 *   - Phase execution plan (ordered list with model tiers)
 *   - Launch form (mode selector, free-form JSON input, pause_after config)
 */

"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getTemplate, startRun, ApiError } from "@/lib/api";
import type { TemplateDetail, RunMode } from "@/lib/types";
import { PhaseList } from "@/components/pipeline/PhaseList";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";

interface Props {
  params: { id: string };
}

export default function TemplateDetailPage({ params }: Props) {
  const { id } = params;
  const router = useRouter();

  const [template, setTemplate] = useState<TemplateDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Launch form state
  const [mode, setMode] = useState<RunMode>("dry-run");
  const [inputJson, setInputJson] = useState("{}");
  const [inputError, setInputError] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);

  useEffect(() => {
    let cancelled = false;

    getTemplate(id)
      .then((data) => {
        if (!cancelled) {
          setTemplate(data);
          // Pre-fill input with example_input if available
          if (data.example_input && Object.keys(data.example_input).length > 0) {
            setInputJson(JSON.stringify(data.example_input, null, 2));
          }
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
  }, [id]);

  async function handleLaunch() {
    // Validate JSON input
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(inputJson) as Record<string, unknown>;
    } catch {
      setInputError("Invalid JSON — please fix the syntax.");
      return;
    }
    setInputError(null);
    setLaunching(true);

    try {
      const response = await startRun({
        template: id,
        mode,
        input: parsed,
      });
      // Navigate to the run detail page
      router.push(`/runs/${response.run_id}`);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `Failed to start run: ${err.body}`
          : `Failed to start run: ${String(err)}`;
      setInputError(message);
      setLaunching(false);
    }
  }

  // ── Loading / error states ──────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex items-center gap-3 text-text-secondary">
        <div className="spinner h-5 w-5" />
        <span className="text-sm">Loading template…</span>
      </div>
    );
  }

  if (error || !template) {
    return (
      <div className="rounded-md border border-status-error/30 bg-status-error/10 p-4 text-sm text-status-error">
        <p className="font-medium">Failed to load template</p>
        <p className="mt-1 text-xs opacity-80">{error}</p>
      </div>
    );
  }

  // ── Main render ─────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">
      {/* Back link */}
      <a href="/" className="text-sm text-text-secondary hover:text-text-primary no-underline">
        ← Back to templates
      </a>

      {/* Template header */}
      <div>
        <div className="flex flex-wrap items-start gap-3">
          <h1 className="text-2xl font-semibold text-text-primary">
            {template.name}
          </h1>
          <Badge variant="muted">{template.version}</Badge>
        </div>

        {template.description && (
          <p className="mt-2 text-sm text-text-secondary">
            {template.description}
          </p>
        )}

        <div className="mt-3 flex flex-wrap gap-2">
          {template.author && (
            <span className="text-xs text-text-muted">
              by {template.author}
            </span>
          )}
          {template.tags.map((tag) => (
            <Badge key={tag} variant="info">
              {tag}
            </Badge>
          ))}
        </div>
      </div>

      {/* Phase plan */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
          Execution Plan ({template.phases.length} phases)
        </h2>
        <PhaseList phases={template.phases} />
      </section>

      {/* Launch form */}
      <section className="card space-y-4">
        <h2 className="text-sm font-semibold text-text-primary">
          Launch a Run
        </h2>

        {/* Mode selector */}
        <div>
          <label className="mb-1 block text-xs font-medium text-text-secondary">
            Execution Mode
          </label>
          <div className="flex gap-2">
            {(["dry-run", "standalone", "openclaw"] as RunMode[]).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={[
                  "rounded px-3 py-1.5 text-xs font-medium transition-colors",
                  mode === m
                    ? "bg-accent text-white"
                    : "bg-surface-elevated text-text-secondary hover:text-text-primary",
                ].join(" ")}
              >
                {m}
              </button>
            ))}
          </div>
        </div>

        {/* JSON input */}
        <div>
          <label
            htmlFor="input-json"
            className="mb-1 block text-xs font-medium text-text-secondary"
          >
            Input (JSON)
          </label>
          <textarea
            id="input-json"
            rows={8}
            className={[
              "w-full rounded-md border bg-surface-elevated font-mono text-xs text-text-primary",
              "px-3 py-2 placeholder:text-text-muted",
              "focus:outline-none focus:ring-2 focus:ring-accent",
              inputError ? "border-status-error" : "border-border",
            ].join(" ")}
            value={inputJson}
            onChange={(e) => {
              setInputJson(e.target.value);
              setInputError(null);
            }}
            spellCheck={false}
          />
          {inputError && (
            <p className="mt-1 text-xs text-status-error">{inputError}</p>
          )}
        </div>

        {/* Submit */}
        <Button
          onClick={handleLaunch}
          disabled={launching}
          loading={launching}
        >
          {launching ? "Launching…" : "Launch Run"}
        </Button>
      </section>
    </div>
  );
}
