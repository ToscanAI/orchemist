import type { Config } from 'jest';
import nextJest from 'next/jest.js';

const createJestConfig = nextJest({
  // Points to the Next.js app root — loads next.config.js and .env files in test runs
  dir: './',
});

const config: Config = {
  coverageProvider: 'v8',
  testEnvironment: 'jsdom',
  // Run jest.setup.ts after the test framework is installed
  setupFilesAfterEnv: ['<rootDir>/jest.setup.ts'],
  moduleNameMapper: {
    // Support the @/ path alias defined in tsconfig.json
    '^@/(.*)$': '<rootDir>/$1',
  },
  testMatch: [
    '**/__tests__/**/*.[jt]s?(x)',
    '**/?(*.)+(spec|test).[jt]s?(x)',
  ],
  // Playwright specs live in tests-e2e/ and import @playwright/test —
  // loading them under jest throws "Class extends value undefined".
  // They run via `npx playwright test`, never jest (2026-06-11 UX audit).
  testPathIgnorePatterns: ['/node_modules/', '<rootDir>/tests-e2e/'],
};

export default createJestConfig(config);
