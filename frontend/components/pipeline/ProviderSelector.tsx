'use client';

/**
 * ProviderSelector — credential fields based on execution mode.
 *
 * - standalone: optional Anthropic API key
 * - openrouter: OpenRouter API key field
 * - openclaw / dry-run: no fields shown
 *
 * @module
 */

import type { RunMode } from '@/lib/types';

interface ProviderSelectorProps {
  mode: RunMode;
  apiKey: string;
  onApiKeyChange: (key: string) => void;
}

export function ProviderSelector({ mode, apiKey, onApiKeyChange }: ProviderSelectorProps) {
  if (mode === 'dry-run' || mode === 'openclaw') {
    return null;
  }

  return (
    <div className="flex flex-col gap-3">
      <label className="text-xs font-medium text-content-secondary">
        {mode === 'openrouter' ? 'OpenRouter API Key' : 'Anthropic API Key'}
        <span className="ml-1 text-content-tertiary">(optional if set via env var)</span>
      </label>
      <input
        type="password"
        placeholder={mode === 'openrouter' ? 'sk-or-...' : 'sk-ant-...'}
        value={apiKey ?? ''}
        onChange={(e) => onApiKeyChange(e.target.value)}
        className="rounded-md border border-default bg-surface-0 px-3 py-1.5 text-sm text-content-primary placeholder:text-content-tertiary focus:outline-none"
        autoComplete="off"
      />
    </div>
  );
}
