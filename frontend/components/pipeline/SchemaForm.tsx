'use client';

/**
 * SchemaForm — renders typed input fields from a JSON Schema `config_schema`.
 *
 * Supports: string, number, integer, boolean, enum, defaults, required fields.
 * Falls back to raw JSON textarea when no schema properties exist.
 *
 * @module
 */

import { useState, useEffect, useCallback, useRef } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SchemaProperty {
  type?: string;
  description?: string;
  default?: unknown;
  enum?: readonly string[];
}

interface ConfigSchema {
  properties?: Record<string, SchemaProperty>;
  required?: readonly string[];
}

interface SchemaFormProps {
  schema: ConfigSchema | Record<string, unknown>;
  exampleInput?: Record<string, unknown> | null;
  onChange: (values: Record<string, unknown>) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SchemaForm({ schema, exampleInput, onChange }: SchemaFormProps) {
  const properties = (schema as ConfigSchema).properties;
  const required = new Set((schema as ConfigSchema).required ?? []);
  const hasSchema = properties && Object.keys(properties).length > 0;

  // Stable ref for onChange to avoid stale closures
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Field values
  const [values, setValues] = useState<Record<string, unknown>>({});
  // Fallback raw JSON textarea
  const [rawJson, setRawJson] = useState('{}');
  const [rawJsonError, setRawJsonError] = useState<string | null>(null);

  // Initialize defaults from schema
  useEffect(() => {
    if (!hasSchema || !properties) return;
    const defaults: Record<string, unknown> = {};
    for (const [key, prop] of Object.entries(properties)) {
      if (prop.default !== undefined) {
        defaults[key] = prop.default;
      }
    }
    setValues(defaults);
    onChangeRef.current(defaults);
  }, [hasSchema, properties]);

  const updateField = useCallback(
    (key: string, value: unknown) => {
      setValues((prev) => {
        const next = { ...prev, [key]: value };
        onChange(next);
        return next;
      });
    },
    [onChange],
  );

  const loadExample = useCallback(() => {
    if (!exampleInput) return;
    if (hasSchema) {
      setValues((prev) => {
        const next = { ...prev, ...exampleInput };
        onChange(next);
        return next;
      });
    } else {
      const json = JSON.stringify(exampleInput, null, 2);
      setRawJson(json);
      setRawJsonError(null);
      onChange(exampleInput);
    }
  }, [exampleInput, hasSchema, onChange]);

  // Fallback: raw JSON textarea
  if (!hasSchema) {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <label htmlFor="json-input" className="text-xs font-medium text-zinc-400">
            Input (JSON)
          </label>
          {exampleInput && (
            <button
              type="button"
              onClick={loadExample}
              className="text-xs text-sky-400 hover:underline"
            >
              Load Example
            </button>
          )}
        </div>
        <textarea
          id="json-input"
          value={rawJson}
          onChange={(e) => {
            const v = e.target.value;
            setRawJson(v);
            try {
              const parsed = JSON.parse(v);
              setRawJsonError(null);
              onChange(parsed);
            } catch (err) {
              setRawJsonError(err instanceof Error ? err.message : 'Invalid JSON');
            }
          }}
          className="min-h-[140px] w-full rounded-lg bg-zinc-900 border border-zinc-700 px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none focus:ring-2 focus:ring-sky-500 resize-y"
          aria-invalid={rawJsonError !== null}
          spellCheck={false}
        />
        {rawJsonError && (
          <p className="text-xs text-red-400">{rawJsonError}</p>
        )}
      </div>
    );
  }

  // Schema-driven form
  return (
    <div className="flex flex-col gap-4">
      {exampleInput && (
        <div className="flex justify-end">
          <button
            type="button"
            onClick={loadExample}
            className="text-xs text-sky-400 hover:underline"
          >
            Load Example
          </button>
        </div>
      )}

      {Object.entries(properties!).map(([key, prop]) => {
        const isRequired = required.has(key);
        const fieldId = `schema-field-${key}`;
        const value = values[key];

        // Enum → select
        if (prop.enum && prop.enum.length > 0) {
          return (
            <div key={key} className="flex flex-col gap-1.5">
              <label htmlFor={fieldId} className="text-xs font-medium text-zinc-400">
                {key}
                {isRequired && <span className="ml-0.5 text-red-400">*</span>}
              </label>
              <select
                id={fieldId}
                value={String(value ?? '')}
                onChange={(e) => updateField(key, e.target.value)}
                required={isRequired}
                className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-sky-500 focus:outline-none"
              >
                <option value="">Select…</option>
                {prop.enum.map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
              {prop.description && (
                <p className="text-xs text-zinc-500">{prop.description}</p>
              )}
            </div>
          );
        }

        // Boolean → checkbox
        if (prop.type === 'boolean') {
          return (
            <div key={key} className="flex flex-col gap-1.5">
              <label className="flex items-center gap-2 text-xs font-medium text-zinc-400">
                <input
                  type="checkbox"
                  checked={!!value}
                  onChange={(e) => updateField(key, e.target.checked)}
                  className="rounded border-zinc-600 bg-zinc-900 text-sky-500 focus:ring-sky-500"
                />
                {key}
                {isRequired && <span className="ml-0.5 text-red-400">*</span>}
              </label>
              {prop.description && (
                <p className="text-xs text-zinc-500">{prop.description}</p>
              )}
            </div>
          );
        }

        // Number / integer → number input
        if (prop.type === 'number' || prop.type === 'integer') {
          return (
            <div key={key} className="flex flex-col gap-1.5">
              <label htmlFor={fieldId} className="text-xs font-medium text-zinc-400">
                {key}
                {isRequired && <span className="ml-0.5 text-red-400">*</span>}
              </label>
              <input
                id={fieldId}
                type="number"
                step={prop.type === 'integer' ? '1' : 'any'}
                value={value !== undefined ? String(value) : ''}
                onChange={(e) => {
                  const v = e.target.value;
                  updateField(key, v === '' ? undefined : Number(v));
                }}
                required={isRequired}
                className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-sky-500 focus:outline-none"
              />
              {prop.description && (
                <p className="text-xs text-zinc-500">{prop.description}</p>
              )}
            </div>
          );
        }

        // Default: string → text input (or textarea for long descriptions)
        return (
          <div key={key} className="flex flex-col gap-1.5">
            <label htmlFor={fieldId} className="text-xs font-medium text-zinc-400">
              {key}
              {isRequired && <span className="ml-0.5 text-red-400">*</span>}
            </label>
            <input
              id={fieldId}
              type="text"
              value={String(value ?? '')}
              onChange={(e) => updateField(key, e.target.value)}
              required={isRequired}
              placeholder={prop.description ?? ''}
              className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 placeholder-zinc-600 focus:border-sky-500 focus:outline-none"
            />
            {prop.description && (
              <p className="text-xs text-zinc-500">{prop.description}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
