/**
 * Root layout for the Orchestration Engine UI.
 *
 * Applies the `dark` class to <html> (enabling Tailwind's dark-mode utilities),
 * loads global CSS, and renders the top-level navigation shell.
 */

import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Orchestration Engine",
  description: "Local web UI for running orchestration pipelines",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-surface-canvas text-text-primary min-h-screen">
        {/* Top navigation bar */}
        <header className="sticky top-0 z-50 border-b border-border bg-surface-canvas/80 backdrop-blur-sm">
          <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3">
            {/* Brand */}
            <a
              href="/"
              className="flex items-center gap-2 text-text-primary no-underline hover:no-underline"
            >
              {/* Simple SVG icon */}
              <svg
                width="20"
                height="20"
                viewBox="0 0 20 20"
                fill="none"
                aria-hidden="true"
              >
                <circle cx="10" cy="10" r="9" stroke="#388bfd" strokeWidth="1.5" />
                <circle cx="10" cy="10" r="3" fill="#388bfd" />
                <line x1="10" y1="1" x2="10" y2="7" stroke="#388bfd" strokeWidth="1.5" />
                <line x1="10" y1="13" x2="10" y2="19" stroke="#388bfd" strokeWidth="1.5" />
                <line x1="1" y1="10" x2="7" y2="10" stroke="#388bfd" strokeWidth="1.5" />
                <line x1="13" y1="10" x2="19" y2="10" stroke="#388bfd" strokeWidth="1.5" />
              </svg>
              <span className="font-semibold text-sm tracking-tight">
                Orchestration Engine
              </span>
            </a>

            {/* Nav links */}
            <nav className="flex items-center gap-4 text-sm text-text-secondary">
              <a
                href="/"
                className="hover:text-text-primary no-underline hover:no-underline transition-colors"
              >
                Templates
              </a>
            </nav>
          </div>
        </header>

        {/* Page content */}
        <main className="mx-auto max-w-7xl px-4 py-8">{children}</main>
      </body>
    </html>
  );
}
