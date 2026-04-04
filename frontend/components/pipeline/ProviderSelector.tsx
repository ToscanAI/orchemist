'use client';

/**
 * ProviderSelector — credential fields based on execution mode.
 *
 * - standalone: optional Anthropic API key, optional custom base URL
 * - openrouter: OpenRouter API key field
 * - openclaw / dry-run: no fields shown
 *
 * @module
 */

import { useState } from 'react';
import type { RunMode } from '@/lib/types';

interface ProviderSelectorProps {
  mode: RunMode;
  onApiKeyChange: (key: string) => void;
  onBaseUrlChange?: (url: string) => void;
}

export function ProviderSelector({ mode, onApiKeyChange, onBaseUrlChange }: ProviderSelectorProps) {
  const [showCustom, setShowCustom] = useState(false);

  if (mode === 'dry-run' || mode === 'openclaw') {
    return null;
  }

  return (
    <div className="flex flex-col gap-3">
      <label className="text-xs font-medium text-zinc-400">
        {mode === 'openrouter' ? 'OpenRouter API Key' : 'Anthropic API Key'}
        <span className="ml-1 text-zinc-600">(optional if set via env var)</span>
      </label>
      <input
        type="password"
        placeholder={mode === 'openrouter' ? 'sk-or-...' : 'sk-ant-...'}
        onChange={(e) => onApiKeyChange(e.target.value)}
        className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 placeholder-zinc-600 focus:border-sky-500 focus:outline-none"
        autoComplete="off"
      />

      {mode === 'standalone' && (
        <>
          <label className="flex items-center gap-2 text-xs text-zinc-500">
            <input
              type="checkbox"
              checked={showCustom}
              onChange={(e) => setShowCustom(e.target.checked)}
              className="rounded border-zinc-600 bg-zinc-900 text-sky-500 focus:ring-sky-500"
            />
            Custom endpoint (OpenAI-compatible)
          </label>
          {showCustom && (
            <input
              type="url"
              placeholder="https://your-proxy.example.com/v1"
              onChange={(e) => onBaseUrlChange?.(e.target.value)}
              className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 placeholder-zinc-600 focus:border-sky-500 focus:outline-none"
            />
          )}
        </>
      )}
    </div>
  );
}
