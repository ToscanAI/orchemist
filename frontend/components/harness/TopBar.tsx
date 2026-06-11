'use client';

/**
 * Top bar — 64px tall, sticky. Breadcrumb left, contextual actions right.
 *
 * Right side ALWAYS shows (≥sm; pills collapse first on tiny screens):
 *   - Autonomy level pill (linked to /admin) — live from `/api/v1/admin/state`
 *   - Cost-today pill — live from `/api/v1/costs/summary`
 *   - User initials avatar
 *
 * Both pills render an em-dash while their value is unknown (loading or
 * fetch failure) — the previous hardcoded "Level 4.3" / "$12.47 ▲"
 * placeholders were fabricated data (2026-06-11 UX audit). The fake trend
 * glyph is gone with them: no trend endpoint exists yet, so none is shown.
 *
 * Below `lg` a hamburger button (left of the breadcrumb) opens the LeftRail
 * drawer via `onMenuClick`.
 *
 * Optional contextual actions (page-specific buttons) render between the
 * pills and the user avatar.
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
  /** Live autonomy level ("Level 4.3") or null while unknown → renders '—'. */
  readonly autonomyLevel: string | null;
  /** Live today-spend ("$1.23") or null while unknown → renders '—'. */
  readonly costToday: string | null;
  readonly userInitials: string;
  readonly actions?: ReactNode;
  /** Opens the LeftRail drawer (hamburger, visible below `lg` only). */
  readonly onMenuClick?: () => void;
}

export function TopBar({
  title,
  breadcrumb,
  autonomyLevel,
  costToday,
  userInitials,
  actions,
  onMenuClick,
}: TopBarProps) {
  return (
    <header
      className="sticky top-0 z-30 ml-0 flex h-16 items-center border-b border-harness-border bg-[#0E1115] px-4 sm:px-6 lg:ml-60"
    >
      {/* Hamburger — mobile/tablet only; the rail is always visible at lg+. */}
      <button
        type="button"
        onClick={onMenuClick}
        aria-label="Open navigation"
        data-testid="topbar-menu-button"
        className="mr-3 flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-harness-border text-harness-muted hover:text-harness-text lg:hidden"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
          <path d="M2 4h12M2 8h12M2 12h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>

      <div className="flex-1 flex flex-col gap-1 min-w-0">
        <nav aria-label="Breadcrumb" className="hidden items-center gap-1 text-[11px] text-harness-dim sm:flex">
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
        <h1 className="text-[16px] font-bold leading-tight text-harness-text truncate sm:text-[20px]">{title}</h1>
      </div>

      <div className="ml-3 flex min-w-0 max-w-[58vw] items-center gap-2 overflow-x-auto sm:ml-6 sm:max-w-none sm:gap-3 sm:overflow-visible [scrollbar-width:none]">
        <Link
          href="/admin"
          className="h-pill hidden md:inline-flex"
          data-testid="autonomy-pill"
          style={{ borderImage: 'linear-gradient(90deg, #7C5CFC, #2DD4BF) 1' }}
        >
          <span
            className={[
              'inline-block h-2 w-2 rounded-full',
              autonomyLevel === null ? 'bg-harness-dim' : 'bg-harness-teal',
            ].join(' ')}
            aria-hidden
          />
          Autonomy: {autonomyLevel ?? '—'}
        </Link>
        <div className="h-pill hidden sm:inline-flex" data-testid="cost-pill">
          <span className="text-harness-muted">Today:</span>
          <span className="font-semibold text-harness-text">{costToday ?? '—'}</span>
        </div>
        {actions}
        <div
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-bold text-[#0B0D10]"
          style={{ background: 'linear-gradient(90deg, #7C5CFC 0%, #2DD4BF 100%)' }}
          aria-label="User"
        >
          {userInitials}
        </div>
      </div>
    </header>
  );
}
