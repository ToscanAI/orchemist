'use client';

/**
 * Top bar — 64px tall, sticky. Breadcrumb left, contextual actions right.
 *
 * Right side ALWAYS shows:
 *   - Autonomy level pill (linked to /admin)
 *   - Cost-today pill
 *   - User initials avatar
 *
 * Optional contextual actions (page-specific buttons) render between the
 * autonomy pill and the user avatar.
 */

import Link from 'next/link';
import type { ReactNode } from 'react';

interface BreadcrumbSegment {
  readonly label: string;
  readonly href?: string;
}

interface TopBarProps {
  readonly title: string;
  readonly breadcrumb: readonly BreadcrumbSegment[];
  readonly autonomyLevel: string;
  readonly costToday: string;
  readonly costTrend?: 'up' | 'down' | 'flat';
  readonly userInitials: string;
  readonly actions?: ReactNode;
}

export function TopBar({
  title,
  breadcrumb,
  autonomyLevel,
  costToday,
  costTrend = 'flat',
  userInitials,
  actions,
}: TopBarProps) {
  const trendGlyph = costTrend === 'up' ? '▲' : costTrend === 'down' ? '▼' : '·';
  const trendColor = costTrend === 'up' ? 'text-harness-teal'
    : costTrend === 'down' ? 'text-harness-danger'
    : 'text-harness-dim';

  return (
    <header
      className="sticky top-0 z-30 ml-60 flex h-16 items-center border-b border-harness-border bg-[#0E1115] px-6"
    >
      <div className="flex-1 flex flex-col gap-1 min-w-0">
        <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-[11px] text-harness-dim">
          {breadcrumb.map((seg, i) => (
            <span key={i} className="flex items-center gap-1">
              {seg.href ? (
                <Link href={seg.href} className="h-link no-underline hover:underline">
                  {seg.label}
                </Link>
              ) : (
                <span className={i === breadcrumb.length - 1 ? 'font-semibold text-harness-text' : ''}>
                  {seg.label}
                </span>
              )}
              {i < breadcrumb.length - 1 && <span className="text-harness-dim">/</span>}
            </span>
          ))}
        </nav>
        <h1 className="text-[20px] font-bold leading-tight text-harness-text truncate">{title}</h1>
      </div>

      <div className="flex items-center gap-3 ml-6">
        <Link
          href="/admin"
          className="h-pill"
          data-testid="autonomy-pill"
          style={{ borderImage: 'linear-gradient(90deg, #7C5CFC, #2DD4BF) 1' }}
        >
          <span className="inline-block h-2 w-2 rounded-full bg-harness-teal" aria-hidden />
          Autonomy: {autonomyLevel}
        </Link>
        <div className="h-pill" data-testid="cost-pill">
          <span className="text-harness-muted">Today:</span>
          <span className="font-semibold text-harness-text">{costToday}</span>
          <span className={trendColor}>{trendGlyph}</span>
        </div>
        {actions}
        <div
          className="flex h-7 w-7 items-center justify-center rounded-full text-[11px] font-bold text-[#0B0D10]"
          style={{ background: 'linear-gradient(90deg, #7C5CFC 0%, #2DD4BF 100%)' }}
          aria-label="User"
        >
          {userInitials}
        </div>
      </div>
    </header>
  );
}
