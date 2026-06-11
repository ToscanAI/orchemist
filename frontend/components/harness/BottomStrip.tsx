'use client';

/**
 * Bottom status strip — 32px tall, sticky bottom. Always present.
 *
 * Every dot is LIVE (2026-06-11 UX audit — previously three of four dots
 * were hardcoded, including a stale skills-pack version claim):
 *   - engine: `/api/v1/health` probe (version + reachability), 30s refresh
 *   - one dot per execution mode from `/api/v1/admin/state` `modes`
 *     (standalone / openrouter / openclaw / dry-run), on=teal off=dim
 *
 * While admin state is unknown (loading or fetch failure) only the engine
 * dot renders — the strip never fabricates mode status.
 *
 * Right side: screen index for traceability (matches the SVG canon).
 */

import { useEffect, useState } from 'react';
import { getHealth, type AdminState } from '@/lib/api';

interface BottomStripProps {
  readonly screenIndex: number; // 1..6
  /** Live mode toggles from admin state; null while unknown. */
  readonly modes: AdminState['modes'] | null;
}

interface Dot {
  readonly tone: 'success' | 'warning' | 'danger' | 'dim';
  readonly label: string;
}

const TONE_COLORS = {
  success: '#2DD4BF',
  warning: '#F59E0B',
  danger: '#EF4444',
  dim: '#5A6371',
} as const;

/** Display order + labels for the admin-state mode toggles. */
const MODE_LABELS: ReadonlyArray<{ key: keyof AdminState['modes']; label: string }> = [
  { key: 'standalone', label: 'standalone' },
  { key: 'openrouter', label: 'openrouter' },
  { key: 'openclaw', label: 'openclaw' },
  { key: 'dry_run', label: 'dry-run' },
];

export function BottomStrip({ screenIndex, modes }: BottomStripProps) {
  const [engine, setEngine] = useState<Dot>({ tone: 'dim', label: 'engine · checking…' });

  useEffect(() => {
    let cancelled = false;
    const check = () => {
      getHealth()
        .then((h) => {
          if (cancelled) return;
          setEngine({
            tone: h.status === 'ok' ? 'success' : 'warning',
            label: h.status === 'ok' ? `engine ${h.version} · daemon up` : `engine ${h.version} · ${h.status}`,
          });
        })
        .catch(() => {
          if (!cancelled) {
            setEngine({ tone: 'danger', label: 'engine · unreachable' });
          }
        });
    };
    check();
    const id = setInterval(check, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const modeDots: readonly Dot[] = modes
    ? MODE_LABELS.map(({ key, label }) => ({
        tone: modes[key] ? ('success' as const) : ('dim' as const),
        label: `${label} · ${modes[key] ? 'on' : 'off'}`,
      }))
    : [];

  const dots: readonly Dot[] = [engine, ...modeDots];

  return (
    <footer
      className="fixed bottom-0 left-0 right-0 z-20 flex h-8 items-center border-t border-harness-border bg-[#0E1115] px-4 text-[10px] text-harness-muted sm:px-6 lg:left-60"
      data-testid="bottom-strip"
    >
      <div className="flex flex-1 items-center gap-4 overflow-x-auto whitespace-nowrap sm:gap-6 [scrollbar-width:none]">
        {dots.map((d, i) => (
          <div key={i} className="flex shrink-0 items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: TONE_COLORS[d.tone] }}
              aria-hidden
            />
            <span>{d.label}</span>
          </div>
        ))}
      </div>
      <span className="hidden shrink-0 pl-3 text-harness-dim sm:inline">
        screen {screenIndex} / 6 · {new Date().toISOString().slice(0, 10)}
      </span>
    </footer>
  );
}
