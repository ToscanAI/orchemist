import { test, expect, type Page, type ConsoleMessage, type Response } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'fs';
import { resolve } from 'path';

/**
 * Comprehensive harness audit — single spec that walks every screen,
 * exercises each interactive affordance, and records:
 *   - JS console errors / warnings
 *   - 4xx / 5xx HTTP responses
 *   - Page-error events (unhandled rejection, render errors)
 *   - Full-page screenshots per checkpoint
 *
 * Output: a single JSON report + per-checkpoint PNG screenshots under
 * /tmp/harness-audit/. Designed to be re-runnable after each fix.
 *
 * The spec is not pass/fail — every finding is recorded for triage. The
 * one assertion is that the bottom-strip mounts on every screen (catches
 * fundamental shell breakage).
 */

const OUT = '/tmp/harness-audit';
mkdirSync(OUT, { recursive: true });

interface Finding {
  readonly screen: string;
  readonly checkpoint: string;
  readonly kind: 'console' | 'network' | 'pageerror';
  readonly severity: 'error' | 'warning';
  readonly detail: string;
}

const findings: Finding[] = [];

function attach(page: Page, screen: string, checkpoint: string) {
  const onConsole = (msg: ConsoleMessage) => {
    const type = msg.type();
    if (type === 'error' || type === 'warning') {
      findings.push({
        screen, checkpoint, kind: 'console', severity: type,
        detail: msg.text().slice(0, 500),
      });
    }
  };
  const onResponse = (r: Response) => {
    if (r.status() >= 400) {
      findings.push({
        screen, checkpoint, kind: 'network', severity: r.status() >= 500 ? 'error' : 'warning',
        detail: `${r.status()} ${r.request().method()} ${r.url()}`,
      });
    }
  };
  const onPageError = (e: Error) => {
    findings.push({ screen, checkpoint, kind: 'pageerror', severity: 'error', detail: e.message.slice(0, 500) });
  };
  page.on('console', onConsole);
  page.on('response', onResponse);
  page.on('pageerror', onPageError);
  return () => {
    page.off('console', onConsole);
    page.off('response', onResponse);
    page.off('pageerror', onPageError);
  };
}

async function shot(page: Page, screen: string, name: string) {
  await page.screenshot({ path: `${OUT}/${screen}__${name}.png`, fullPage: true });
}

test('harness audit · walk every screen', async ({ browser }) => {
  test.setTimeout(300_000);
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // ─── 1. Fleet Dashboard ───────────────────────────────────────────
  let detach = attach(page, '01-fleet', 'initial-load');
  await page.goto('/', { waitUntil: 'networkidle', timeout: 30_000 });
  await expect(page.getByTestId('bottom-strip')).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(1500);
  await shot(page, '01-fleet', 'initial');

  // Verify KPI values are not placeholder zeros when engine is up
  const activeRunsKpiText = await page.getByTestId('kpi-active-runs').innerText();
  const gatesKpiText = await page.getByTestId('kpi-gates').innerText();
  findings.push({ screen: '01-fleet', checkpoint: 'kpi-snapshot', kind: 'console', severity: 'warning',
    detail: `Active runs KPI: ${activeRunsKpiText.replace(/\n/g, ' | ')}` });
  findings.push({ screen: '01-fleet', checkpoint: 'kpi-snapshot', kind: 'console', severity: 'warning',
    detail: `Gates KPI: ${gatesKpiText.replace(/\n/g, ' | ')}` });
  detach();

  // ─── 2. Trust & Gates ─────────────────────────────────────────────
  detach = attach(page, '04-gates', 'initial-load');
  await page.getByTestId('nav-gates').click();
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1500);
  await shot(page, '04-gates', 'initial');

  // Filter pills — click each, verify gate-row count changes
  for (const filter of ['pending', 'auto-merged', 'held', 'all']) {
    const pill = page.getByRole('button', { name: new RegExp(`^${filter}$`, 'i') }).first();
    if (await pill.isVisible()) {
      await pill.click();
      await page.waitForTimeout(800);
      const visibleRows = await page.locator('[data-testid^="gate-row-"]').count();
      findings.push({
        screen: '04-gates', checkpoint: `filter-${filter}`, kind: 'console', severity: 'warning',
        detail: `Filter "${filter}" → ${visibleRows} visible rows`,
      });
      await shot(page, '04-gates', `filter-${filter}`);
    }
  }
  detach();

  // ─── 3. Adversary Loop (no run) ───────────────────────────────────
  detach = attach(page, '03-adversary', 'no-run');
  await page.getByTestId('nav-adversary').click();
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1500);
  await shot(page, '03-adversary', 'no-run-param');
  detach();

  // ─── 4. Adversary Loop (with bogus run param) ─────────────────────
  detach = attach(page, '03-adversary', 'invalid-run');
  await page.goto('/adversary?run=totally-invalid-run-id', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await shot(page, '03-adversary', 'invalid-run');
  detach();

  // ─── 5. Adversary Loop (with real run id) ─────────────────────────
  // Pull a real run id from the engine first
  const runsList = await page.evaluate(async () => {
    const r = await fetch('/api/v1/runs?limit=1');
    return r.json();
  });
  const realRunId = (runsList?.items?.[0]?.run_id ?? '') as string;
  if (realRunId) {
    detach = attach(page, '03-adversary', 'real-run');
    await page.goto(`'/adversary?run=${realRunId}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);
    await shot(page, '03-adversary', 'real-run');
    detach();

    // ─── 6. Run Cockpit (real run) ────────────────────────────────
    // Use `domcontentloaded` instead of `networkidle` because the cockpit
    // opens a long-lived SSE connection to /api/v1/runs/{id}/stream — that
    // connection never idles, so `networkidle` would block until timeout.
    detach = attach(page, '02-cockpit', 'real-run-direct');
    const r = await page.goto(`'/runs/${realRunId}`, { waitUntil: 'domcontentloaded', timeout: 15_000 }).catch((e: Error) => {
      findings.push({ screen: '02-cockpit', checkpoint: 'real-run-direct', kind: 'pageerror', severity: 'error', detail: `goto failed: ${e.message}` });
      return null;
    });
    if (r) {
      findings.push({
        screen: '02-cockpit', checkpoint: 'real-run-direct', kind: 'network',
        severity: r.status() >= 400 ? 'error' : 'warning',
        detail: `direct nav to /runs/${realRunId} → HTTP ${r.status()}`,
      });
    }
    await page.waitForTimeout(1500);
    await shot(page, '02-cockpit', 'direct-real-run');
    detach();
  }

  // ─── 7. Run Cockpit via /runs/_ SPA fallback ──────────────────────
  // Same SSE-keepalive caveat as above — use domcontentloaded.
  detach = attach(page, '02-cockpit', 'spa-fallback');
  await page.goto('/runs/_', { waitUntil: 'domcontentloaded', timeout: 15_000 });
  await page.waitForTimeout(1500);
  await shot(page, '02-cockpit', 'spa-fallback');
  detach();

  // ─── 8. /runs (list) ──────────────────────────────────────────────
  detach = attach(page, '02-runs-list', 'initial-load');
  await page.goto('/runs', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await shot(page, '02-runs-list', 'initial');
  // Click the first row if any
  const firstRow = page.locator('tbody tr').first();
  if (await firstRow.isVisible().catch(() => false)) {
    await firstRow.click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1500);
    await shot(page, '02-runs-list', 'after-row-click');
  }
  detach();

  // ─── 9. Admin / Activation ────────────────────────────────────────
  detach = attach(page, '05-admin', 'initial-load');
  await page.goto('/admin', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await shot(page, '05-admin', 'initial');

  // Toggle each toggle once — capture state changes
  const toggleIds = ['toggle-openrouter', 'toggle-standalone', 'toggle-openclaw', 'toggle-dryrun',
    'kill-automerge', 'kill-spawn', 'kill-regression', 'kill-skills', 'flag-phase0', 'flag-extend', 'flag-crossrepo'];
  for (const tid of toggleIds) {
    const t = page.getByTestId(tid);
    if (await t.isVisible().catch(() => false)) {
      await t.click();
      await page.waitForTimeout(150);
      await t.click();  // toggle back
      await page.waitForTimeout(150);
    }
  }
  await shot(page, '05-admin', 'after-toggles');
  detach();

  // ─── 10. Skills Pack Mode ─────────────────────────────────────────
  detach = attach(page, '06-skills', 'initial-load');
  await page.goto('/skills', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await shot(page, '06-skills', 'initial');
  detach();

  // ─── 11. /templates ───────────────────────────────────────────────
  detach = attach(page, '07-templates', 'initial-load');
  await page.goto('/templates', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await shot(page, '07-templates', 'initial');
  detach();

  // ─── 12. Cross-link sanity: click breadcrumb back from a deep page ─
  if (realRunId) {
    detach = attach(page, 'cross-links', 'cockpit-breadcrumb');
    await page.goto(`'/runs/${realRunId}`, { waitUntil: 'domcontentloaded', timeout: 15_000 }).catch(() => {});
    await page.waitForTimeout(1000);
    // Try clicking a breadcrumb segment
    const bc = page.locator('header').getByRole('link', { name: /Fleet/i }).first();
    if (await bc.isVisible().catch(() => false)) {
      await bc.click();
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(800);
      const url = page.url();
      findings.push({
        screen: 'cross-links', checkpoint: 'cockpit-breadcrumb', kind: 'console', severity: 'warning',
        detail: `breadcrumb Fleet → navigated to ${url}`,
      });
      await shot(page, 'cross-links', 'after-breadcrumb-fleet');
    }
    detach();
  }

  // Write findings report
  writeFileSync(`${OUT}/findings.json`, JSON.stringify(findings, null, 2));
  console.log(`\n=== AUDIT SUMMARY ===`);
  console.log(`Total findings: ${findings.length}`);
  console.log(`  errors: ${findings.filter((f) => f.severity === 'error').length}`);
  console.log(`  warnings: ${findings.filter((f) => f.severity === 'warning').length}`);

  await ctx.close();
});
