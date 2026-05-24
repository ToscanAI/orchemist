'use client';

/**
 * Left navigation rail — 240px wide, matches SVG canon.
 *
 * Layout (top to bottom):
 *   - Logo + subtitle (40px tall block)
 *   - 6 nav items, each 40px tall (rounded chip when active)
 *   - "REPOSITORIES" section listing the repos under management
 *   - Footer with user identity
 *
 * Active state is derived from `usePathname()` — top-level prefix match.
 */

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Logo } from './Logo';
import { NAV_ITEMS, type HarnessRepo } from './types';

interface LeftRailProps {
  readonly repos: readonly HarnessRepo[];
  readonly userInitials: string;
  readonly userEmail: string;
}

function isActive(pathname: string, href: string): boolean {
  if (href === '/') return pathname === '/';
  return pathname === href || pathname.startsWith(href + '/');
}

function repoDot(state: HarnessRepo['state']): string {
  switch (state) {
    case 'active': return '#2DD4BF';
    case 'idle': return '#F59E0B';
    case 'deprecated': return '#5A6371';
  }
}

export function LeftRail({ repos, userInitials, userEmail }: LeftRailProps) {
  const pathname = usePathname();

  return (
    <aside
      className="fixed left-0 top-0 bottom-0 z-40 flex w-60 flex-col border-r border-harness-border bg-[#0E1115]"
      aria-label="Primary navigation"
    >
      {/* Logo block */}
      <div className="px-6 pt-7 pb-5">
        <Logo />
      </div>

      {/* Primary nav */}
      <nav className="px-3 pb-4">
        <ul className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <li key={item.section}>
                <Link
                  href={item.href}
                  data-testid={`nav-${item.section}`}
                  className={[
                    'flex h-9 items-center gap-3 rounded-md px-3 text-[13px] font-semibold transition-colors',
                    active
                      ? 'border border-harness-purple bg-harness-surface3 text-harness-text'
                      : 'text-harness-muted hover:bg-harness-surface3 hover:text-harness-text',
                  ].join(' ')}
                  aria-current={active ? 'page' : undefined}
                >
                  <span
                    className={[
                      'inline-block h-2 w-2 rounded-full',
                      active ? 'bg-harness-purple' : 'border border-harness-dim',
                    ].join(' ')}
                    aria-hidden
                  />
                  <span className="flex-1">{item.label}</span>
                  <span className="text-[11px] font-medium text-harness-dim">
                    {item.shortcut}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Repos */}
      <div className="px-6 pt-6 pb-2">
        <div className="h-section-label">REPOSITORIES ({repos.length})</div>
      </div>
      <ul className="px-3 flex flex-col gap-1">
        {repos.map((repo) => (
          <li
            key={repo.slug}
            data-testid={`repo-${repo.slug}`}
            className="flex h-7 items-center gap-3 rounded-md px-3 text-[12px]"
          >
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: repoDot(repo.state) }}
              aria-hidden
            />
            <span
              className={[
                'flex-1',
                repo.state === 'deprecated' ? 'line-through text-harness-muted' : 'text-harness-text',
              ].join(' ')}
            >
              {repo.displayName}
            </span>
            <span
              className={[
                'text-[10px]',
                repo.state === 'idle' ? 'text-harness-warning' :
                repo.state === 'deprecated' ? 'text-harness-danger' :
                'text-harness-dim',
              ].join(' ')}
            >
              {repo.state === 'active'
                ? `${repo.activeRunCount} active`
                : repo.state === 'idle'
                ? 'idle'
                : 'deprecated'}
            </span>
          </li>
        ))}
      </ul>
      <div className="px-6 pt-3">
        <button
          type="button"
          className="text-[11px] text-harness-dim hover:text-harness-text transition-colors"
          aria-label="Add repository"
        >
          + Add repository
        </button>
      </div>

      <div className="flex-1" />

      {/* Footer / user */}
      <div className="px-6 pb-6 text-[11px] text-harness-dim">
        <div className="font-medium text-harness-muted">{userInitials}</div>
        <div className="truncate">{userEmail}</div>
      </div>
    </aside>
  );
}
