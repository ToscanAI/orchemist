/**
 * Frontend phase-label derivation utility (#842).
 *
 * Backend `GET /api/v1/phases` returns raw phase metadata (id, name,
 * model_tier, task_type, depends_on, order). The harness UI wants
 * human-readable labels like "1a · spec" and tier badges like "OPUS ·
 * cross-model gate". Those UX conventions are NOT in the backend YAML
 * (and shouldn't be — they're pure presentation) so we derive them
 * here, in ONE place, instead of hardcoding 12-entry arrays in every
 * page component.
 *
 * Drift policy: when a new phase is added to the YAML it appears in
 * /api/v1/phases automatically and gets a sensible default label from
 * `derivePhaseDef`. Custom labels for the canonical pipeline live in
 * `STANDARD_PIPELINE_OVERRIDES` below; add an entry there when a new
 * phase needs special presentation.
 */

import type { PhaseMetaRecord } from './api';

export type PhaseTier = 'sonnet' | 'opus' | 'engine';

export interface PhaseDef {
  readonly id: string;
  readonly label: string;
  readonly subtitle?: string;
  readonly tier: PhaseTier;
}

/**
 * Engine-phase task_types (no LLM dispatched). When `task_type` matches
 * one of these, the tier badge is 'engine' regardless of `model_tier`
 * (some engine phases have model_tier='sonnet' in the YAML for
 * historical reasons; the task_type is the load-bearing signal).
 *
 * Note: the standard pipeline's `test` phase uses `task_type: command`
 * (shells out via `command:` field), and `acceptance_run` has its own
 * `task_type: acceptance_run` (engine runs pytest directly). Both are
 * engine task types for our purposes.
 */
const ENGINE_TASK_TYPES: ReadonlySet<string> = new Set([
  'acceptance_run',
  'command',
]);

/**
 * Per-phase UX overrides for the standard coding pipeline. Keyed by
 * phase id. Each override merges into the derived defaults from the
 * backend record. Add an entry here when a new phase needs special
 * labelling (e.g. sub-letter ordering, "OPUS · cross-model gate"
 * subtitle).
 */
const STANDARD_PIPELINE_OVERRIDES: Readonly<Record<string, Partial<Omit<PhaseDef, 'id'>>>> = {
  existing_symbols_inventory: {
    label: '0 · existing_symbols_inventory',
    subtitle: 'sticky inventory · v4.2',
  },
  spec:               { label: '1a · spec' },
  behavioral:         { label: '1b · behavioral' },
  spec_adversary:     { label: '1c · spec_adversary', subtitle: 'OPUS · cross-model gate' },
  postmortem_spec:    { label: '1d · postmortem_spec', subtitle: 'exhaustion analysis' },
  acceptance_test:    { label: '2 · acceptance_test' },
  implement:          { label: '3 · implement' },
  acceptance_run:     { label: '3b · acceptance_run', subtitle: 'engine · no LLM' },
  review:             { label: '4 · review', subtitle: 'OPUS' },
  fix:                { label: '4b · fix' },
  postmortem_review:  { label: '4c · postmortem_review', subtitle: 'exhaustion analysis' },
  test:               { label: '5 · test', subtitle: 'engine · no LLM' },
};

/**
 * Derive a presentation-ready PhaseDef from a backend PhaseMetaRecord.
 *
 * - `tier`: 'engine' if `task_type` is an engine task type; 'opus' if
 *   `model_tier` is 'opus'; otherwise 'sonnet'.
 * - `label`: from the override map keyed by phase id; otherwise
 *   `"${order} · ${id}"`.
 * - `subtitle`: from the override map if present; otherwise undefined.
 */
export function derivePhaseDef(p: PhaseMetaRecord): PhaseDef {
  const tier: PhaseTier =
    p.task_type && ENGINE_TASK_TYPES.has(p.task_type)
      ? 'engine'
      : p.model_tier === 'opus'
      ? 'opus'
      : 'sonnet';
  const override = STANDARD_PIPELINE_OVERRIDES[p.id] ?? {};
  return {
    id: p.id,
    label: override.label ?? `${p.order} · ${p.id}`,
    subtitle: override.subtitle,
    tier,
  };
}

/**
 * Convenience: map a list of backend records to PhaseDefs.
 */
export function derivePhaseDefs(records: readonly PhaseMetaRecord[]): readonly PhaseDef[] {
  return records.map(derivePhaseDef);
}
