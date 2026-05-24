import { test, expect } from '@playwright/test';

/**
 * Harness screens · smoke + screenshot canon.
 *
 * For each of the six screens, this spec:
 *   1. Mocks the engine API as offline → page falls back to demo data.
 *   2. Navigates to the screen.
 *   3. Asserts a few load-bearing test-IDs are present (nav link, page-specific marker).
 *   4. Captures a full-page screenshot into `docs/harness-redesign-2026-05-24/screenshots/`.
 *
 * The screenshots become the visual diff target — every PR that touches a
 * harness screen should re-generate these and the reviewer compares against
 * the canonical SVG mockups in `screens/`.
 */

const ROUTES: ReadonlyArray<{
  readonly path: string;
  readonly slug: string;
  readonly screenIndex: number;
  readonly assertions: ReadonlyArray<{ readonly testId: string; readonly description: string }>;
}> = [
  {
    path: '/',
    slug: '01-fleet-dashboard',
    screenIndex: 1,
    assertions: [
      { testId: 'nav-fleet', description: 'Fleet Dashboard nav active' },
      { testId: 'kpi-active-runs', description: 'Active runs KPI rendered' },
      { testId: 'kpi-gates', description: 'Gates KPI rendered' },
      { testId: 'section-inflight', description: 'In-flight section rendered' },
      { testId: 'autonomy-ramp', description: 'Autonomy ramp rendered' },
    ],
  },
  {
    // `_` is the SPA-fallback param from generateStaticParams (see next.config.js
    // output:'export'); in dev mode this is the only id pre-generated. In prod,
    // any /runs/<id> renders via the SPA fallback HTML shell.
    path: '/runs/_',
    slug: '02-run-cockpit',
    screenIndex: 2,
    assertions: [
      { testId: 'nav-cockpit', description: 'Run Cockpit nav active' },
      { testId: 'phase-existing_symbols_inventory', description: 'Phase 0 card rendered' },
      { testId: 'phase-implement', description: 'Implement phase card rendered' },
      { testId: 'section-tool-stream', description: 'Live tool-call stream section rendered' },
    ],
  },
  {
    path: '/adversary',
    slug: '03-adversary-loop',
    screenIndex: 3,
    assertions: [
      { testId: 'nav-adversary', description: 'Adversary nav active' },
    ],
  },
  {
    path: '/gates',
    slug: '04-trust-gates',
    screenIndex: 4,
    assertions: [
      { testId: 'nav-gates', description: 'Gates nav active' },
      { testId: 'section-gates', description: 'Approval queue rendered' },
      { testId: 'section-trust', description: 'Trust profiles rendered' },
      { testId: 'gate-row-0', description: 'First gate row rendered' },
    ],
  },
  {
    path: '/admin',
    slug: '05-admin-activation',
    screenIndex: 5,
    assertions: [
      { testId: 'nav-admin', description: 'Admin nav active' },
      { testId: 'toggle-openrouter', description: 'OpenRouter toggle rendered' },
      { testId: 'panic-button', description: 'PANIC button rendered' },
    ],
  },
  {
    path: '/skills',
    slug: '06-skills-pack-mode',
    screenIndex: 6,
    assertions: [
      { testId: 'nav-skills', description: 'Skills nav active' },
      { testId: 'section-install-state', description: 'Install state banner rendered' },
    ],
  },
];

// Mock the engine as offline (or empty) so every page renders demo data.
async function mockEngineOffline(page: import('@playwright/test').Page) {
  // /api/v1/* → 503 so the client treats engine as offline
  await page.route(/\/api\/v1\/.*/, async (route) => {
    await route.fulfill({ status: 503, body: '{"detail":"engine offline (mock)"}', headers: { 'content-type': 'application/json' } });
  });
}

for (const route of ROUTES) {
  test(`harness screen · ${route.slug}`, async ({ page }, testInfo) => {
    await mockEngineOffline(page);
    await page.goto(route.path);
    // Wait for left rail to mount — proves the shell loaded
    await expect(page.getByTestId('bottom-strip')).toBeVisible({ timeout: 15_000 });

    for (const a of route.assertions) {
      const el = page.getByTestId(a.testId);
      await expect(el, a.description).toBeVisible({ timeout: 10_000 });
    }

    // Full-page screenshot saved under docs/harness-redesign-2026-05-24/screenshots/
    const screenshotPath = testInfo.outputPath(`${route.slug}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: true });
    // Also store a copy in the canonical screenshots folder
    const canonical = `../docs/harness-redesign-2026-05-24/screenshots/${route.slug}.png`;
    await page.screenshot({ path: canonical, fullPage: true });
  });
}
