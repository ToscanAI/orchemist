'use client';

/**
 * HarnessShell — wraps every harness page in the LeftRail + TopBar + BottomStrip layout.
 *
 * Defaults are populated from the API where possible and fall back to safe
 * placeholders when the engine is unreachable (so the UI never blanks out).
 *
 * Pages call `<HarnessShell title=... screenIndex=... breadcrumb=...>{children}</HarnessShell>`.
 */

import type { ReactNode } from 'react';
import { LeftRail } from './LeftRail';
import { TopBar } from './TopBar';
import { BottomStrip } from './BottomStrip';
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
  return (
    <div className="min-h-screen bg-harness-bg" style={{ backgroundImage: 'var(--tw-bg-image)' }}>
      <LeftRail
        repos={DEFAULT_REPOS}
        userInitials="RR"
        userEmail="contact@renerivera.net"
      />
      <TopBar
        title={title}
        breadcrumb={breadcrumb}
        autonomyLevel="Level 4.3"
        costToday="$12.47"
        costTrend="up"
        userInitials="RR"
        actions={actions}
      />
      <main
        className="ml-60 px-6 pt-6 pb-12"
        style={{ paddingBottom: '4rem' }}
      >
        {children}
      </main>
      <BottomStrip screenIndex={screenIndex} />
    </div>
  );
}
