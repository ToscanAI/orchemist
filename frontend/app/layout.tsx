/**
 * Root layout — dark theme shell with Geist fonts and top navigation.
 *
 * Applied to every page in the app via Next.js App Router.
 * This is the single place to configure fonts, metadata, and global structure.
 */
import type { Metadata } from 'next';
import { GeistSans } from 'geist/font/sans';
import { GeistMono } from 'geist/font/mono';
import { TopNav } from '@/components/TopNav';
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
