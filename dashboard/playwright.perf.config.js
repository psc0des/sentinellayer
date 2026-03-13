/**
 * playwright.perf.config.js
 *
 * Separate Playwright config for production performance benchmarking.
 * Does NOT spin up a local web server — tests against live production URLs directly.
 *
 * Usage:
 *   PERF_LABEL=before npx playwright test perf-benchmark --config playwright.perf.config.js
 *   PERF_LABEL=after  npx playwright test perf-benchmark --config playwright.perf.config.js
 */

import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir:  './tests',
  testMatch: '**/perf-benchmark.spec.js',

  // Serial execution — we want consistent, non-contended measurements
  workers: 1,

  // Individual test timeout: 3 min (scan pipeline test needs up to 3 min)
  timeout: 200_000,

  expect: { timeout: 20_000 },

  use: {
    // No baseURL — tests use full production URLs directly
    headless: true,
    // Generous navigation timeout for live environment
    navigationTimeout: 30_000,
    actionTimeout:     20_000,
  },

  // No webServer block — not needed for live-env benchmarking
})
