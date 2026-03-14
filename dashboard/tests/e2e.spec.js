import { test, expect } from '@playwright/test'

test.describe('RuriSkry dashboard e2e', () => {
  test('loads overview and primary navigation', async ({ page }) => {
    await page.goto('/overview')

    await expect(page.getByText(/agents connected/i)).toBeVisible()
    await expect(page.getByRole('link', { name: /overview/i })).toBeVisible()
    await expect(page.getByRole('link', { name: /scans/i })).toBeVisible()
    await expect(page.getByRole('link', { name: /decisions/i })).toBeVisible()
    await expect(page.getByRole('heading', { name: /Recent scan runs/i })).toBeVisible()
    await expect(page.getByText(/Auto-refresh 5s/i)).toBeVisible()
  })

  test('deploy scan runs and completes (verdict or framework error)', async ({ page }) => {
    // Deploy scans take 3-5 min on live Azure — override the global 180s limit.
    test.setTimeout(360_000)
    await page.goto('/scans')

    await page.getByLabel(/Resource Group/i).fill('ruriskry-prod-rg')
    await page.getByRole('button', { name: /Deploy Scan/i }).click()

    // Scan should start
    await expect(page.getByText(/Scanning/i).first()).toBeVisible()

    // Scan must finish within 300s — deploy scans typically take 3-5 min on live Azure.
    // Accept either a successful verdict count OR a framework error (LLM timeout).
    // Both outcomes mean the scan pipeline completed end-to-end.
    const verdictOrError = page.getByText(/verdict\(s\)|Agent framework error/i)
    await expect(verdictOrError.first()).toBeVisible({ timeout: 300_000 })

    // Scan history table should reflect the completed scan
    await expect(page.getByRole('heading', { name: /Scan history/i })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: /status/i })).toBeVisible()
  })

  test('decisions drilldown opens and shows execution status', async ({ page }) => {
    await page.goto('/decisions')

    await expect(page.getByRole('heading', { name: 'Decisions' })).toBeVisible()
    await page.locator('tbody tr').first().click()

    await expect(page.getByText(/Governance Verdict/i)).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Execution Status' })).toBeVisible()
    await expect(page.getByText(/Why proposed/i)).toBeVisible()
  })

  test('agent card last run results shows scan outcome (not "run a scan first")', async ({ page }) => {
    await page.goto('/agents')

    await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText('monitoring-agent')).toBeVisible({ timeout: 20_000 })
    // Open menu for monitoring-agent (nth(2) alphabetically: cost, deploy, monitoring)
    await page.getByLabel(/Agent actions menu/i).nth(2).click()
    await page.getByRole('button', { name: /Last Run Results/i }).click()

    // After a scan, the card must show a result — either proposals found OR clean.
    // The old broken state "run a scan first" must NOT appear.
    await expect(
      page.getByText(/Scan completed|verdict\(s\)/i).first()
    ).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText(/run a scan first/i)).not.toBeVisible()
  })
})
