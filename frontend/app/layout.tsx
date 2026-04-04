/**
 * Root layout — dark theme shell with Geist fonts and top navigation.
 *
 * Applied to every page in the app via Next.js App Router.
 * This is the single place to configure fonts, metadata, and global structure.
 */
import type { Metadata } from 'next';
import { GeistSans } from 'geist/font/sans';
import { GeistMono } from 'geist/font/mono';
import './globals.css';

export const metadata: Metadata = {
  title: {
    default: 'Orchestration Engine',
    template: '%s | Orchestration Engine',
  },
  description:
    'Scenario-driven orchestration engine for multi-agent AI pipelines.',
};

interface RootLayoutProps {
  children: React.ReactNode;
}

/**
 * Top navigation bar.
 * Placeholder — will be replaced by full nav component in #304.
 */
function TopNav() {
  return (
    <header className="sticky top-0 z-50 border-b border-zinc-800 bg-zinc-950/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-screen-xl items-center justify-between px-4 sm:px-6 lg:px-8">
        {/* Logo / brand */}
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold tracking-tight text-zinc-100">
            Orchestration Engine
          </span>
          <span className="rounded-full bg-sky-500/10 px-2 py-0.5 text-xs font-medium text-sky-400 ring-1 ring-sky-500/20">
            v0.3
          </span>
        </div>

        {/* Primary nav links */}
        <nav aria-label="Primary navigation">
          <ul className="flex items-center gap-1">
            <li>
              <a href="/" className="nav-item">
                Dashboard
              </a>
            </li>
            <li>
              <a href="/runs" className="nav-item">
                Runs
              </a>
            </li>
            <li>
              <a href="/templates" className="nav-item">
                Templates
              </a>
            </li>
          </ul>
        </nav>
      </div>
    </header>
  );
}

/**
 * Root layout wrapping every page.
 * Sets font CSS variables consumed by Tailwind font-family tokens.
 */
export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html
      lang="en"
      className={`dark ${GeistSans.variable} ${GeistMono.variable}`}
    >
      <body className="min-h-screen bg-zinc-950 text-zinc-100 antialiased">
        <TopNav />
        <main className="mx-auto max-w-screen-xl px-4 py-8 sm:px-6 lg:px-8">
          {children}
        </main>
      </body>
    </html>
  );
}
