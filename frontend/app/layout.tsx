/**
 * Root layout — Orchemist Harness shell.
 *
 * Removed TopNav (legacy) — every harness page now renders its own
 * `<HarnessShell>` wrapper with the LeftRail + TopBar + BottomStrip
 * primitives. Legacy pages (/runs, /templates) opt in by wrapping themselves.
 *
 * This file is intentionally minimal: it only configures fonts, sets the
 * global background, and wraps children in `<EngineOfflineGuard>` so every
 * harness page short-circuits to a clear error UI when the engine is
 * unreachable (#888 — harness graduation). Per-page chrome lives in the
 * shell; the guard probes `/api/v1/health` once on mount and renders the
 * offline error UI on rejection.
 */
import type { Metadata } from 'next';
import { GeistSans } from 'geist/font/sans';
import { GeistMono } from 'geist/font/mono';
import './globals.css';
import { EngineOfflineGuard } from '@/components/harness/EngineOfflineGuard';

export const metadata: Metadata = {
  title: {
    default: 'Orchemist Harness',
    template: '%s · Orchemist Harness',
  },
  description:
    'Operator surface for the Orchemist orchestration engine — cross-model adversarial review at phase boundaries.',
};

interface RootLayoutProps {
  children: React.ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html
      lang="en"
      className={`dark ${GeistSans.variable} ${GeistMono.variable}`}
    >
      <body className="min-h-screen bg-harness-bg text-harness-text antialiased">
        <EngineOfflineGuard>{children}</EngineOfflineGuard>
      </body>
    </html>
  );
}
