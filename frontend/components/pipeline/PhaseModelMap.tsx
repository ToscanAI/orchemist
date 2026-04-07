'use client';

/**
 * PhaseModelMap — per-phase model assignment table with override dropdowns.
 *
 * Shows each phase's default model tier + thinking level, and the resolved
 * model name. Each row allows an optional override via a select dropdown.
 *
 * @module
 */

import type { PhaseDetail } from '@/lib/types';

// Default model resolution (matches OpenRouterExecutor defaults)
const DEFAULT_MODELS: Record<string, string> = {
  haiku: 'anthropic/claude-haiku-4-5-20251001',
  sonnet: 'anthropic/claude-sonnet-4-6',
  opus: 'anthropic/claude-opus-4-6',
};

// Popular alternative models for the override dropdown
const MODEL_OPTIONS = [
  { label: 'Default', value: '' },
  { label: 'Claude Haiku 4.5', value: 'anthropic/claude-haiku-4-5-20251001' },
  { label: 'Claude Sonnet 4.6', value: 'anthropic/claude-sonnet-4-6' },
  { label: 'Claude Opus 4.6', value: 'anthropic/claude-opus-4-6' },
  { label: 'GPT-4o', value: 'openai/gpt-4o' },
  { label: 'GPT-4o Mini', value: 'openai/gpt-4o-mini' },
  { label: 'Gemini 2.5 Pro', value: 'google/gemini-2.5-pro' },
  { label: 'DeepSeek R1', value: 'deepseek/deepseek-r1' },
];

interface PhaseModelMapProps {
  phases: readonly PhaseDetail[];
  modelMap: Record<string, string>;
  onModelMapChange: (map: Record<string, string>) => void;
}

export function PhaseModelMap({ phases, modelMap, onModelMapChange }: PhaseModelMapProps) {
  if (!phases.length) return null;

  const handleOverride = (tier: string, value: string) => {
    const next = { ...modelMap };
    if (value) {
      next[tier] = value;
    } else {
      delete next[tier];
    }
    onModelMapChange(next);
  };

  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-medium text-zinc-400">Phase Model Assignments</label>
      <div className="overflow-hidden rounded-lg border border-zinc-800">
        <table className="w-full text-xs">
          <thead className="border-b border-zinc-800 bg-zinc-900/50">
            <tr>
              <th className="px-3 py-2 text-left font-medium text-zinc-500">Phase</th>
              <th className="px-3 py-2 text-left font-medium text-zinc-500">Tier</th>
              <th className="px-3 py-2 text-left font-medium text-zinc-500">Thinking</th>
              <th className="px-3 py-2 text-left font-medium text-zinc-500">Model</th>
              <th className="px-3 py-2 text-left font-medium text-zinc-500">Override</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {phases.map((phase) => {
              const tier = phase.model_tier ?? 'sonnet';
              const resolved = modelMap[tier] || DEFAULT_MODELS[tier] || tier;
              return (
                <tr key={phase.id}>
                  <td className="px-3 py-2 text-zinc-300">{phase.name}</td>
                  <td className="px-3 py-2">
                    <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-400">
                      {tier}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-zinc-500">{phase.thinking_level ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-zinc-400">{resolved}</td>
                  <td className="px-3 py-2">
                    <select
                      value={modelMap[tier] || ''}
                      onChange={(e) => handleOverride(tier, e.target.value)}
                      className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-300 focus:border-sky-500 focus:outline-none"
                    >
                      {MODEL_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>
                          {opt.label}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
