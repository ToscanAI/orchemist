import type { Config } from "jest";
import nextJest from "next/jest.js";

const createJestConfig = nextJest({
  // Provide the path to your Next.js app to load next.config.js and .env
  // files in your test environment.
  dir: "./",
});

const config: Config = {
  coverageProvider: "v8",
  testEnvironment: "jsdom",
  // Add any custom module name mappers here.
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/$1",
  },
  setupFilesAfterFramework: ["<rootDir>/jest.setup.ts"],
};

// createJestConfig merges Next.js defaults with the config above.
export default createJestConfig(config);
