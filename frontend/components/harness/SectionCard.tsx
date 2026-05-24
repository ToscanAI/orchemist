/**
 * SectionCard — the panel surface used everywhere on the harness for
 * grouped content (e.g. "In-flight pipeline runs", "Regression queue").
 *
 * Composes header (title + optional subtitle + right-side action) and body.
 * Tone changes the left/top border color to draw attention without shouting.
 */

import type { ReactNode } from 'react';
import type { StatusTone } from './types';

interface SectionCardProps {
  readonly title: string;
  readonly subtitle?: ReactNode;
  readonly action?: ReactNode;
  readonly tone?: StatusTone;
  readonly children: ReactNode;
  readonly className?: string;
  readonly testId?: string;
}

const TONE_BORDER: Record<StatusTone, string> = {
  success: 'h-card-teal',
  warning: 'h-card-warning',
  danger: 'h-card-danger',
  info: 'h-card-purple',
  neutral: '',
};

export function SectionCard({
  title,
  subtitle,
  action,
  tone = 'neutral',
  children,
  className,
  testId,
}: SectionCardProps) {
  return (
    <div
      className={['h-card p-5', TONE_BORDER[tone], className ?? ''].filter(Boolean).join(' ')}
      data-testid={testId}
    >
      <div className="mb-4 flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <h2 className="text-[14px] font-bold text-harness-text leading-tight">{title}</h2>
          {subtitle && (
            <div className="mt-1 text-[11px] text-harness-muted">{subtitle}</div>
          )}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}
