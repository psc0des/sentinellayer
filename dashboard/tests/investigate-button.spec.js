/**
 * investigate-button.spec.js
 *
 * Verifies the manual alert investigation UI changes:
 *  - Alerts page renders correctly
 *  - Status filter shows "Pending" (not "Firing")
 *  - Table shows "Actions" column header
 *  - Pending alerts show "🔍 Investigate" button
 *  - Investigated/resolved alerts do NOT show the button
 *  - Clicking Investigate calls POST /api/alerts/{id}/investigate
 */

import { test, expect } from '@playwright/test'

const PENDING_ALERT = {
  alert_id: 'test-pending-001',
  status: 'pending',
  resource_id: '/subscriptions/demo/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-web-01',
  resource_name: 'vm-web-01',
  metric: 'Percentage CPU',
  value: 92.5,
  threshold: 80.0,
  severity: '2',
  resource_group: 'test-rg',
  fired_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
  received_at: new Date(Date.now() - 4 * 60 * 1000).toISOString(),
  investigating_at: null,
  resolved_at: null,
  proposals_count: 0,
  proposals: [],
  verdicts: [],
  totals: { approved: 0, escalated: 0, denied: 0 },
  error: null,
}

const RESOLVED_ALERT = {
  alert_id: 'test-resolved-001',
  status: 'resolved',
  resource_id: '/subscriptions/demo/resourceGroups/test-rg/providers/Microsoft.Compute/virtualMachines/vm-dr-01',
  resource_name: 'vm-dr-01',
  metric: 'Heartbeat',
  value: null,
  threshold: null,
  severity: '1',
  resource_group: 'test-rg',
  fired_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
  received_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
  investigating_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
  resolved_at: new Date(Date.now() - 1 * 60 * 60 * 1000).toISOString(),
  proposals_count: 1,
  proposals: [],
  verdicts: [],
  totals: { approved: 1, escalated: 0, denied: 0 },
  error: null,
}

test.describe('Manual alert investigation — Alerts page', () => {
  test.beforeEach(async ({ page }) => {
    // Mock all API calls the Alerts page depends on
    await page.route('**/api/alerts**', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ count: 2, alerts: [PENDING_ALERT, RESOLVED_ALERT] }),
      })
    )
    await page.route('**/api/alerts/active-count**', route =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ active_count: 1 }),
      })
    )
    // Mock other endpoints the layout calls on load
    await page.route('**/api/metrics**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ total_evaluations: 0, approval_rate: 0, avg_sri: 0, denied_count: 0, escalated_count: 0 }) })
    )
    await page.route('**/api/agents**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ agents: [] }) })
    )
    await page.route('**/api/evaluations**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ count: 0, evaluations: [] }) })
    )
    await page.route('**/api/scan-history**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ count: 0, scans: [] }) })
    )
    await page.route('**/api/executions/pending-reviews**', route =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ count: 0, pending_reviews: [] }) })
    )

    await page.goto('/alerts')
  })

  test('Alerts page loads and shows alert table', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /alerts/i })).toBeVisible()
    await expect(page.getByRole('table')).toBeVisible()
  })

  test('status filter shows "Pending" not "Firing"', async ({ page }) => {
    const statusFilter = page.locator('select').filter({ hasText: /all statuses/i })
    await expect(statusFilter).toBeVisible()

    // "Pending" option must exist
    await expect(statusFilter.locator('option[value="pending"]')).toHaveCount(1)
    // "Firing" option must NOT exist
    await expect(statusFilter.locator('option[value="firing"]')).toHaveCount(0)
  })

  test('table has "Actions" column header', async ({ page }) => {
    await expect(page.getByRole('columnheader', { name: /actions/i })).toBeVisible()
  })

  test('pending alert row shows Investigate button', async ({ page }) => {
    const investigateBtn = page.getByRole('button', { name: /investigate/i })
    await expect(investigateBtn).toBeVisible()
    await expect(investigateBtn).toBeEnabled()
  })

  test('resolved alert row does NOT show Investigate button (only one button total)', async ({ page }) => {
    // There is only 1 pending alert and 1 resolved alert.
    // Only the pending one should have an Investigate button.
    const investigateBtns = page.getByRole('button', { name: /investigate/i })
    await expect(investigateBtns).toHaveCount(1)
  })

  test('clicking Investigate calls POST /api/alerts/{id}/investigate', async ({ page }) => {
    // Track the API call
    let investigateCalled = false
    let calledAlertId = null

    await page.route('**/api/alerts/*/investigate', async route => {
      const url = route.request().url()
      calledAlertId = url.split('/alerts/')[1].split('/investigate')[0]
      investigateCalled = true
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'investigating', alert_id: calledAlertId }),
      })
    })

    const btn = page.getByRole('button', { name: /investigate/i })
    await btn.click()

    // Give the click handler time to execute
    await page.waitForTimeout(500)

    expect(investigateCalled).toBe(true)
    expect(calledAlertId).toBe('test-pending-001')
  })

  test('Investigate button shows spinner and "Starting…" while pending', async ({ page }) => {
    // Delay the investigate response to observe loading state
    await page.route('**/api/alerts/*/investigate', async route => {
      await new Promise(r => setTimeout(r, 2000))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'investigating', alert_id: 'test-pending-001' }),
      })
    })

    const btn = page.getByRole('button', { name: /investigate/i })
    await btn.click()

    // During the API call the button should show loading state
    await expect(page.getByText(/starting/i)).toBeVisible({ timeout: 1000 })
  })
})
