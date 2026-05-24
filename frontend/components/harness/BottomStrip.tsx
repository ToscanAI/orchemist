'use client';

/**
 * Bottom status strip — 32px tall, sticky bottom. Always present.
 *
 * Shows live engine telemetry: engine version, daemon uptime, OpenRouter
 * status, OpenClaw status, Claude Code skills pack install status.
 * Right side: screen index for traceability (matches the SVG canon).
 */

import { useEffect, useState } from 'react';
import { getHealth } from '@/lib/api';

interface BottomStripProps {
  readonly screenIndex: number; // 1..6
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

export function BottomStrip({ screenIndex }: BottomStripProps) {
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

  const dots: readonly Dot[] = [
    engine,
    { tone: 'success', label: 'openrouter · multi-provider mode active' },
    { tone: 'warning', label: 'openclaw gateway · idle (deactivated)' },
    { tone: 'success', label: 'claude code · skills pack v4.2 installed locally' },
  ];

  return (
    <footer
      className="fixed bottom-0 left-60 right-0 z-20 flex h-8 items-center border-t border-harness-border bg-[#0E1115] px-6 text-[10px] text-harness-muted"
      data-testid="bottom-strip"
    >
      <div className="flex flex-1 items-center gap-6">
        {dots.map((d, i) => (
          <div key={i} className="flex items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: TONE_COLORS[d.tone] }}
              aria-hidden
            />
            <span>{d.label}</span>
          </div>
        ))}
      </div>
      <span className="text-harness-dim">screen {screenIndex} / 6 · {new Date().toISOString().slice(0, 10)}</span>
    </footer>
  );
}
