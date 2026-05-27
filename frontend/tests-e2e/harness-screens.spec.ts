import { test, expect } from '@playwright/test';

/**
 * Harness screens · offline-UI smoke + screenshot canon.
 *
 * Post-#888 (harness graduation): the harness REQUIRES a reachable engine.
 * When the engine is mocked offline (every /api/v1/* returns 503), the
 * top-level `EngineOfflineGuard` short-circuits every route to the same
 * offline error UI. So instead of asserting per-route demo-data test-ids,
 * each route's mock-mode test asserts the offline-UI test-ids that the
 * guard renders.
 *
 * Live-mode tests (`harness-live.spec.ts`) are unchanged — they run with
 * the real engine reachable so the guard is transparent and the route
 * renders normally.
 */

const ROUTES: ReadonlyArray<{
  readonly path: string;
  readonly slug: string;
  readonly screenIndex: number;
}> = [
  { path: '/', slug: '01-fleet-dashboard', screenIndex: 1 },
  // `_` is the SPA-fallback param from generateStaticParams (see next.config.js
  // output:'export'); in dev mode this is the only id pre-generated. In prod,
  // any /runs/<id> renders via the SPA fallback HTML shell.
  { path: '/runs/_', slug: '02-run-cockpit', screenIndex: 2 },
  { path: '/adversary', slug: '03-adversary-loop', screenIndex: 3 },
  { path: '/gates', slug: '04-trust-gates', screenIndex: 4 },
  { path: '/admin', slug: '05-admin-activation', screenIndex: 5 },
  { path: '/skills', slug: '06-skills-pack-mode', screenIndex: 6 },
];

// Mock the engine as offline so EngineOfflineGuard takes over.
async function mockEngineOffline(page: import('@playwright/test').Page) {
  // /api/v1/* → 503 so the client treats engine as offline. The
  // EngineOfflineGuard's getHealth() probe will reject and render the
  // offline error UI on every route.
  await page.route(/\/api\/v1\/.*/, async (route) => {
    await route.fulfill({
      status: 503,
      body: '{"detail":"engine offline (mock)"}',
      headers: { 'content-type': 'application/json' },
    });
  });
}

for (const route of ROUTES) {
  test(`harness screen · ${route.slug}`, async ({ page }, testInfo) => {
    await mockEngineOffline(page);
    await page.goto(route.path);

    // Per #888, every route renders the EngineOfflineGuard error UI when
    // the engine is mocked offline. Assert all three offline-UI landmarks:
    //   - the guard region itself
    //   - the Retry button
    //   - the docs link
    await expect(
      page.getByTestId('engine-offline-guard'),
      'EngineOfflineGuard error region rendered',
    ).toBeVisible({ timeout: 10_000 });

    await expect(
      page.getByTestId('engine-offline-retry'),
      'Retry button rendered',
    ).toBeVisible({ timeout: 10_000 });

    await expect(
      page.getByTestId('engine-offline-docs-link'),
      'docs link rendered',
    ).toBeVisible({ timeout: 10_000 });

    // The error heading must include the canonical phrase + the API base.
    // We use the regex anchor (per behavioral C19 / sub-check 1c) rather
    // than a bare 503 substring that would false-positive on framework
    // debug payloads.
    const region = page.getByTestId('engine-offline-guard');
    await expect(region).toContainText(/Engine unreachable at .+/);

    // The retry button must be a focusable <button> (keyboard a11y).
    const retryTag = await page.getByTestId('engine-offline-retry').evaluate((el) => el.tagName);
    expect(retryTag).toBe('BUTTON');

    // The docs link must point at the orchemist quickstart URL.
    const docsHref = await page.getByTestId('engine-offline-docs-link').getAttribute('href');
    expect(docsHref ?? '').toMatch(/https:\/\/github\.com\/ToscanAI\/orchemist/);
    expect(docsHref ?? '').toMatch(/quickstart/);

    // Full-page screenshot saved into the per-test output dir AND the canonical
    // screenshots folder so the visual-diff workflow keeps producing artifacts.
    const screenshotPath = testInfo.outputPath(`${route.slug}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: true });
    const canonical = `../docs/harness-redesign-2026-05-24/screenshots/${route.slug}.png`;
    await page.screenshot({ path: canonical, fullPage: true });
  });
}
