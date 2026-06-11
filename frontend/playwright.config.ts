import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the Orchemist Harness frontend.
 *
 * Local default: spins up `next dev` on :3000 and runs against it.
 * Set `PW_BASE_URL` to point at an external host (e.g. CI / staging).
 *
 * Engine dependency (post-#888): the harness REQUIRES a reachable engine —
 * with the engine offline every route renders the EngineOfflineGuard error
 * UI. `harness-screens.spec.ts` mocks `/api/v1/*` as offline and asserts
 * exactly that guard, so it needs no `orch serve`; `harness-live.spec.ts`
 * runs against a real `orch serve --port 8374`.
 */
export default defineConfig({
  testDir: './tests-e2e',
  fullyParallel: true,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 2 : 0,
  workers: process.env['CI'] ? 1 : undefined,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: process.env['PW_BASE_URL'] ?? 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // When PW_BASE_URL is set (e.g. CI workflow brings up Next manually on its
  // own port), Playwright must NOT spawn its own webServer — otherwise two
  // dev servers compete (one workflow-managed, one Playwright-managed) and
  // Playwright blocks for 120s waiting for its self-spawned server at :3000.
  // See PR #893 for the original motivation; the CI workflow drives bringup.
  webServer: process.env['PW_BASE_URL']
    ? undefined
    : {
        command: 'npm run dev -- --port 3000',
        url: 'http://localhost:3000',
        reuseExistingServer: !process.env['CI'],
        timeout: 120_000,
      },
});
