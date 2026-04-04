'use client';

/**
 * TopNav — sticky top navigation with active page highlighting and health indicator.
 *
 * Uses `usePathname()` for SPA-aware active link detection.
 * Uses `Link` from next/link for client-side navigation (no full reloads).
 */

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { getHealth } from '@/lib/api';

// ---------------------------------------------------------------------------
// Nav items
// ---------------------------------------------------------------------------

const NAV_ITEMS = [
  { href: '/', label: 'Dashboard', match: (p: string) => p === '/' },
  { href: '/runs', label: 'Runs', match: (p: string) => p.startsWith('/runs') },
  { href: '/templates', label: 'Templates', match: (p: string) => p.startsWith('/templates') },
] as const;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function TopNav() {
  const pathname = usePathname();
  const [health, setHealth] = useState<{ ok: boolean; version: string } | null>(null);

  // Health check on mount + every 30s
  useEffect(() => {
    let cancelled = false;

    function check() {
      getHealth()
        .then((data) => {
          if (!cancelled) setHealth({ ok: data.status === 'ok', version: data.version });
        })
        .catch(() => {
          if (!cancelled) setHealth({ ok: false, version: '' });
        });
    }

    check();
    const interval = setInterval(check, 30_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  return (
    <header className="sticky top-0 z-50 border-b border-zinc-800 bg-zinc-950/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-screen-xl items-center justify-between px-4 sm:px-6 lg:px-8">
        {/* Logo / brand */}
        <div className="flex items-center gap-3">
          <Link href="/" className="flex items-center gap-3">
            <span className="text-sm font-semibold tracking-tight text-zinc-100">
              Orchestration Engine
            </span>
          </Link>
          <span className="rounded-full bg-sky-500/10 px-2 py-0.5 text-xs font-medium text-sky-400 ring-1 ring-sky-500/20">
            {health?.version ? `v${health.version}` : 'v0.3'}
          </span>
          {/* Health indicator */}
          {health !== null && (
            <span
              className={`h-2 w-2 rounded-full ${health.ok ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`}
              title={health.ok ? 'API connected' : 'API unreachable'}
              aria-label={health.ok ? 'API connected' : 'API unreachable'}
            />
          )}
        </div>

        {/* Primary nav links */}
        <nav aria-label="Primary navigation">
          <ul className="flex items-center gap-1">
            {NAV_ITEMS.map((item) => {
              const isActive = item.match(pathname);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    className={isActive ? 'nav-item nav-item-active' : 'nav-item'}
                    aria-current={isActive ? 'page' : undefined}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
      </div>
    </header>
  );
}
