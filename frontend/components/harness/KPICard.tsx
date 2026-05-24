/**
 * KPI Card — the four-across header card pattern from Fleet Dashboard.
 *
 * Value is rendered large, label small uppercase, optional sublabel underneath.
 * Color is driven by tone, default neutral.
 */

import type { StatusTone } from './types';
import type { ReactNode } from 'react';

interface KPICardProps {
  readonly label: string;
  readonly value: string | number;
  readonly tone?: StatusTone;
  readonly sublabel?: ReactNode;
  readonly testId?: string;
}

const TONE_TEXT: Record<StatusTone, string> = {
  success: 'text-harness-teal',
  warning: 'text-harness-warning',
  danger: 'text-harness-danger',
  info: 'text-harness-purple',
  neutral: 'text-harness-text',
};

export function KPICard({ label, value, tone = 'neutral', sublabel, testId }: KPICardProps) {
  return (
    <div className="h-card p-5" data-testid={testId}>
      <div className="h-section-label">{label}</div>
      <div className={`mt-3 text-[38px] font-bold leading-none ${TONE_TEXT[tone]}`}>{value}</div>
      {sublabel && <div className="mt-3 text-[11px] text-harness-muted">{sublabel}</div>}
    </div>
  );
}
