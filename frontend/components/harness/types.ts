/**
 * Harness-wide shared types. These are UI presentational types — they describe
 * what a screen renders, not what the API returns. API shapes still live in
 * `lib/types.ts`; harness components convert API shapes into these on read.
 */

export type AutonomyLevel = 3 | 3.5 | 4 | 4.3 | 5;

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

export type PhaseStatus = 'done' | 'active' | 'queued' | 'failed' | 'skipped';

/** A repo registered with the harness. */
export interface HarnessRepo {
  readonly slug: string;
  readonly displayName: string;
  /** active = at least one running pipeline; idle = none; deprecated = sunset, do not launch. */
  readonly state: 'active' | 'idle' | 'deprecated';
  readonly activeRunCount: number;
  /** Detected/configured language hint (free-text per template). */
  readonly languageHint?: string;
}

/** The six top-level harness sections. */
export type HarnessSection =
  | 'fleet'
  | 'cockpit'
  | 'adversary'
  | 'gates'
  | 'admin'
  | 'skills';

export interface NavItem {
  readonly section: HarnessSection;
  readonly label: string;
  readonly href: string;
  readonly shortcut: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { section: 'fleet', label: 'Fleet Dashboard', href: '/', shortcut: '⌘1' },
  { section: 'cockpit', label: 'Run Cockpit', href: '/runs', shortcut: '⌘2' },
  { section: 'adversary', label: 'Adversary Loop', href: '/adversary', shortcut: '⌘3' },
  { section: 'gates', label: 'Trust & Gates', href: '/gates', shortcut: '⌘4' },
  { section: 'admin', label: 'Admin / Activation', href: '/admin', shortcut: '⌘5' },
  { section: 'skills', label: 'Skills Pack Mode', href: '/skills', shortcut: '⌘6' },
];
