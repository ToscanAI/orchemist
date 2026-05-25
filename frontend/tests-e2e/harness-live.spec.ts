import { test, expect } from '@playwright/test';
import { mkdirSync } from 'fs';
import { resolve } from 'path';

/**
 * Live engine dogfood · screenshots against `orch serve` real data.
 *
 * Unlike `harness-screens.spec.ts` (which mocks the engine offline so demo
 * data renders), this spec lets the frontend hit `/api/v1/*` for real via the
 * Next.js dev-server proxy to port 8374. The engine must be running
 * (`orch serve --port 8374 --no-open`) before this spec runs.
 *
 * Screenshots land under `docs/harness-redesign-2026-05-24/screenshots/live/`.
 */

const LIVE_DIR = resolve(__dirname, '../../docs/harness-redesign-2026-05-24/screenshots/live');
mkdirSync(LIVE_DIR, { recursive: true });

const ROUTES: ReadonlyArray<{ readonly path: string; readonly slug: string; readonly screenIndex: number }> = [
  { path: '/', slug: '01-fleet-dashboard-live', screenIndex: 1 },
  { path: '/runs/_', slug: '02-run-cockpit-live', screenIndex: 2 },
  { path: '/adversary', slug: '03-adversary-loop-live', screenIndex: 3 },
  { path: '/gates', slug: '04-trust-gates-live', screenIndex: 4 },
  { path: '/admin', slug: '05-admin-activation-live', screenIndex: 5 },
  { path: '/skills', slug: '06-skills-pack-mode-live', screenIndex: 6 },
];

for (const route of ROUTES) {
  test(`live · ${route.slug}`, async ({ page }) => {
    // No route mocking — frontend talks to the real engine on port 8374.
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(`page error: ${e.message}`));
    page.on('response', (r) => {
      if (r.url().includes('/api/v1/') && r.status() >= 500) {
        errors.push(`5xx: ${r.url()} → ${r.status()}`);
      }
    });

    await page.goto(route.path);
    await expect(page.getByTestId('bottom-strip')).toBeVisible({ timeout: 20_000 });

    // Wait for the health probe to resolve — engine reachable should turn the
    // engine indicator green within ~30s.
    await page.waitForTimeout(2000);

    await page.screenshot({
      path: `${LIVE_DIR}/${route.slug}.png`,
      fullPage: true,
    });

    // No assertions on data shape — we want the screenshot regardless. Surface
    // any 5xx as a soft warning in the report.
    if (errors.length > 0) {
      console.warn(`[${route.slug}] runtime errors:\n  ${errors.join('\n  ')}`);
    }
  });
}
