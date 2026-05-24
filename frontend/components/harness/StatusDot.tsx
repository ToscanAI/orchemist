/**
 * StatusDot — small colored dot used in tables and lists to signal a
 * status without taking horizontal space.
 *
 * `pulse` adds a soft pulse animation for live signals.
 */

import type { StatusTone } from './types';

const TONE_COLORS: Record<StatusTone, string> = {
  success: '#2DD4BF',
  warning: '#F59E0B',
  danger: '#EF4444',
  info: '#7C5CFC',
  neutral: '#5A6371',
};

interface StatusDotProps {
  readonly tone: StatusTone;
  readonly pulse?: boolean;
  readonly size?: number;
  readonly label?: string;
}

export function StatusDot({ tone, pulse, size = 6, label }: StatusDotProps) {
  return (
    <span
      className={['inline-block rounded-full', pulse ? 'animate-pulse-soft' : ''].join(' ')}
      style={{
        width: `${size}px`,
        height: `${size}px`,
        backgroundColor: TONE_COLORS[tone],
      }}
      aria-label={label}
    />
  );
}
