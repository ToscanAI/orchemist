'use client';

/**
 * PhaseModelMap — tier-grouped model assignment table with override dropdowns.
 *
 * Each unique pipeline-phase tier (haiku / sonnet / opus / engine) gets ONE
 * row with its own override dropdown. The phases that belong to the tier are
 * listed as chips under the tier label so operators can see which phases an
 * override will affect.
 *
 * Issue #772 fix: previously, rendering one row per phase suggested per-phase
 * override controls, but `handleOverride` keyed on `tier`, so changing one
 * sonnet-tier dropdown silently changed all sonnet phases. The data model is
 * per-tier; the rendering now matches.
 *
 * Issue #761: legacy zinc-* classes migrated to harness/content semantic
 * tokens.
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

/**
 * Group phases by tier preserving the order of first appearance, so the UI
 * ordering remains stable across re-renders (tier insertion order = order
 * the phases array first introduces that tier).
 */
function groupPhasesByTier(
  phases: readonly PhaseDetail[],
): readonly { tier: string; phases: readonly PhaseDetail[] }[] {
  const order: string[] = [];
  const byTier: Record<string, PhaseDetail[]> = {};
  for (const p of phases) {
    const tier = p.model_tier ?? 'sonnet';
    if (!byTier[tier]) {
      byTier[tier] = [];
      order.push(tier);
    }
    byTier[tier].push(p);
  }
  return order.map((tier) => ({ tier, phases: byTier[tier] }));
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

  const tierGroups = groupPhasesByTier(phases);

  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-medium text-content-secondary">
        Phase Model Assignments
      </label>
      <p className="text-[11px] text-content-tertiary -mt-1">
        Model overrides are applied per <span className="font-semibold">tier</span>. Each
        override below affects every phase in that tier.
      </p>
      <div className="overflow-hidden rounded-lg border border-default">
        <table className="w-full text-xs">
          <thead className="border-b border-default bg-surface-0">
            <tr>
              <th className="px-3 py-2 text-left font-medium text-content-secondary">Tier</th>
              <th className="px-3 py-2 text-left font-medium text-content-secondary">Phases in tier</th>
              <th className="px-3 py-2 text-left font-medium text-content-secondary">Resolved model</th>
              <th className="px-3 py-2 text-left font-medium text-content-secondary">Override</th>
            </tr>
          </thead>
          <tbody>
            {tierGroups.map(({ tier, phases: tierPhases }, idx) => {
              const resolved = modelMap[tier] || DEFAULT_MODELS[tier] || tier;
              return (
                <tr
                  key={tier}
                  data-tier={tier}
                  className={idx === 0 ? '' : 'border-t border-default'}
                >
                  <td className="px-3 py-2 align-top">
                    <span className="rounded-full bg-surface-2 px-2 py-0.5 text-content-secondary">
                      {tier}
                    </span>
                  </td>
                  <td className="px-3 py-2 align-top">
                    <div className="flex flex-wrap gap-1">
                      {tierPhases.map((phase) => (
                        <span
                          key={phase.id}
                          className="inline-flex items-center rounded-full border border-default bg-surface-0 px-2 py-0.5 text-[11px] text-content-secondary"
                          title={`${phase.name} — thinking: ${phase.thinking_level ?? '—'}`}
                        >
                          {phase.name}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-2 align-top font-mono text-content-secondary">
                    {resolved}
                  </td>
                  <td className="px-3 py-2 align-top">
                    <select
                      value={modelMap[tier] || ''}
                      onChange={(e) => handleOverride(tier, e.target.value)}
                      className="rounded border border-default bg-surface-0 px-2 py-1 text-xs text-content-primary focus:outline-none"
                      aria-label={`Model override for ${tier} tier`}
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
