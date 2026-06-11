'use client';

/**
 * HarnessShell — wraps every harness page in the LeftRail + TopBar + BottomStrip layout.
 *
 * Owns two pieces of cross-cutting state (2026-06-11 UX audit):
 *
 *   1. **Responsive drawer** — below the `lg` breakpoint the LeftRail is an
 *      off-canvas drawer toggled from the TopBar hamburger; `<main>` drops
 *      its rail margin. Desktop layout (≥lg) is unchanged: fixed 240px rail.
 *
 *   2. **Truthful TopBar/BottomStrip data** — autonomy level, today's spend
 *      and mode activation are fetched from the live engine
 *      (`/api/v1/admin/state`, `/api/v1/costs/summary`) instead of the
 *      previous hardcoded placeholders ("Level 4.3" / "$12.47" / static
 *      mode dots). While loading or when a fetch fails, the pills render
 *      an em-dash — never a fabricated value. (The engine-offline case is
 *      handled one level up by `EngineOfflineGuard`.)
 *
 * Pages call `<HarnessShell title=... screenIndex=... breadcrumb=...>{children}</HarnessShell>`.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { LeftRail } from './LeftRail';
import { TopBar } from './TopBar';
import { BottomStrip } from './BottomStrip';
import { getAdminState, getCostsSummary, type AdminState } from '@/lib/api';
import type { HarnessRepo } from './types';

interface HarnessShellProps {
  readonly title: string;
  readonly screenIndex: number;
  readonly breadcrumb: readonly { label: string; href?: string }[];
  readonly actions?: ReactNode;
  readonly children: ReactNode;
}

// The canonical four-repo set under management. Currently sourced from the
// pivot memory + 2026-05-24 audit; will become API-driven once
// `GET /api/v1/repos` lands (Sprint 12 multi-repo orchestration).
const DEFAULT_REPOS: readonly HarnessRepo[] = [
  { slug: 'orchemist', displayName: 'orchemist', state: 'active', activeRunCount: 0, languageHint: 'python' },
  { slug: 'orchemist-skills', displayName: 'orchemist-skills', state: 'active', activeRunCount: 0, languageHint: 'markdown' },
  { slug: 'orchemist-website', displayName: 'orchemist-website', state: 'idle', activeRunCount: 0, languageHint: 'typescript' },
  { slug: 'orchemist-ide', displayName: 'orchemist-ide', state: 'deprecated', activeRunCount: 0 },
];

export function HarnessShell({
  title,
  screenIndex,
  breadcrumb,
  actions,
  children,
}: HarnessShellProps) {
  const [railOpen, setRailOpen] = useState(false);

  // Live shell telemetry. `null` → not yet known → render '—', never a lie.
  const [autonomyLevel, setAutonomyLevel] = useState<string | null>(null);
  const [costToday, setCostToday] = useState<string | null>(null);
  const [modes, setModes] = useState<AdminState['modes'] | null>(null);

  useEffect(() => {
    let cancelled = false;

    getAdminState()
      .then((s) => {
        if (cancelled) return;
        setAutonomyLevel(`Level ${s.autonomy_level}`);
        setModes(s.modes);
      })
      .catch(() => {
        /* leave null → pill renders '—' */
      });

    const today = new Date().toISOString().slice(0, 10);
    getCostsSummary({ start: today, end: today, limit: 1 })
      .then((r) => {
        if (cancelled) return;
        const item = r.items.find((i) => i.day === today);
        // No row for today simply means no spend recorded yet — $0.00 is
        // the truthful rendering, not a fallback.
        setCostToday(`$${(item?.total_cost ?? 0).toFixed(2)}`);
      })
      .catch(() => {
        /* leave null → pill renders '—' */
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="min-h-screen bg-harness-bg" style={{ backgroundImage: 'var(--tw-bg-image)' }}>
      <LeftRail
        repos={DEFAULT_REPOS}
        userInitials="RR"
        userEmail="contact@renerivera.net"
        open={railOpen}
        onClose={() => setRailOpen(false)}
      />
      <TopBar
        title={title}
        breadcrumb={breadcrumb}
        autonomyLevel={autonomyLevel}
        costToday={costToday}
        userInitials="RR"
        actions={actions}
        onMenuClick={() => setRailOpen(true)}
      />
      <main
        className="ml-0 px-4 pt-6 pb-12 sm:px-6 lg:ml-60"
        style={{ paddingBottom: '4rem' }}
      >
        {children}
      </main>
      <BottomStrip screenIndex={screenIndex} modes={modes} />
    </div>
  );
}
