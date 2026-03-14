import { defineConfig } from '@playwright/test'

const LIVE_URL = process.env.PLAYWRIGHT_BASE_URL

export default defineConfig({
  testDir: './tests',
  timeout: 180_000,
  expect: {
    timeout: 15_000,
  },
  use: {
    baseURL: LIVE_URL || 'http://127.0.0.1:4173',
    headless: true,
  },
  // Only start the local dev server when not targeting a live URL.
  // Set PLAYWRIGHT_BASE_URL=https://... to test against staging/production.
  ...(LIVE_URL
    ? {}
    : {
        webServer: {
          command: 'npm run dev -- --host 127.0.0.1 --port 4173',
          url: 'http://127.0.0.1:4173',
          reuseExistingServer: true,
          timeout: 120_000,
        },
      }),
})
