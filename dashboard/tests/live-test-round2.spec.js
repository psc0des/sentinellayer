import { test, expect } from '@playwright/test'

const LIVE_URL = 'https://agreeable-pond-05f59310f.2.azurestaticapps.net'
const BACKEND_URL = 'https://ruriskry-core-backend-psc0des.icygrass-1e4512b3.eastus2.azurecontainerapps.io'

// ─── PAGE LOAD & NAVIGATION ─────────────────────────────────────────────────

test.describe('Live Dashboard — Page Load & Navigation', () => {
    test('Overview page loads and shows metric cards', async ({ page }) => {
        await page.goto(LIVE_URL)
        await expect(page).toHaveTitle(/ruriskry|governance/i, { timeout: 15000 })
        await expect(page.getByText(/total evaluations/i)).toBeVisible({ timeout: 15000 })
        await expect(page.getByText(/approval rate/i)).toBeVisible()
        await expect(page.getByText(/avg sri/i)).toBeVisible()
    })

    test('SPA routing works for direct URL access', async ({ page }) => {
        // /scans redirects to /agents (Scans merged into Agents page)
        await page.goto(`${LIVE_URL}/scans`)
        await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible({ timeout: 15000 })
    })

    test('All sidebar navigation links are functional', async ({ page }) => {
        await page.goto(LIVE_URL)
        await expect(page.getByText(/total evaluations/i)).toBeVisible({ timeout: 15000 })

        // Agents (Scans merged into Agents — no separate /scans sidebar link)
        await page.getByRole('link', { name: /agents/i }).click()
        await expect(page).toHaveURL(/\/agents/)
        await expect(page.getByText(/cost-optimization-agent/i)).toBeVisible({ timeout: 10000 })

        // Alerts
        await page.getByRole('link', { name: /alerts/i }).click()
        await expect(page).toHaveURL(/\/alerts/)

        // Decisions
        await page.getByRole('link', { name: /decisions/i }).click()
        await expect(page).toHaveURL(/\/decisions/)
        await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible()

        // Audit Log
        await page.getByRole('link', { name: /audit/i }).click()
        await expect(page).toHaveURL(/\/audit/)

        // Back to Overview
        await page.getByRole('link', { name: /overview/i }).click()
        await expect(page).toHaveURL(/\/overview/)
    })
})

// ─── OVERVIEW PAGE ──────────────────────────────────────────────────────────

test.describe('Live Dashboard — Overview Deep Test', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${LIVE_URL}/overview`)
        await expect(page.getByText(/total evaluations/i)).toBeVisible({ timeout: 15000 })
    })

    test('shows connected agents count', async ({ page }) => {
        await expect(page.getByText(/agents connected/i)).toBeVisible()
    })

    test('shows SRI trend chart', async ({ page }) => {
        await expect(page.getByText(/sri trend/i)).toBeVisible()
        const chart = page.locator('.recharts-wrapper, svg.recharts-surface')
        await expect(chart.first()).toBeVisible({ timeout: 10000 })
    })

    test('shows pending reviews panel', async ({ page }) => {
        await expect(page.getByText(/pending reviews/i).first()).toBeVisible()
    })

    test('shows recent scan runs table', async ({ page }) => {
        await expect(page.getByText(/recent scan runs/i)).toBeVisible()
        const table = page.locator('table').filter({ hasText: /scan id/i })
        await expect(table).toBeVisible({ timeout: 10000 })
    })

    test('shows Slack Connected status in header', async ({ page }) => {
        await expect(page.getByText(/slack connected/i)).toBeVisible()
    })

    test('shows alert activity card', async ({ page }) => {
        await expect(page.getByText(/alert activity/i)).toBeVisible()
    })
})

// ─── SCANS PAGE ─────────────────────────────────────────────────────────────

test.describe('Live Dashboard — Scans Page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${LIVE_URL}/scans`)
    })

    test('shows 3 agent scan buttons + Run All', async ({ page }) => {
        await expect(page.getByRole('button', { name: /cost scan/i })).toBeVisible({ timeout: 10000 })
        await expect(page.getByRole('button', { name: /monitoring/i })).toBeVisible()
        await expect(page.getByRole('button', { name: /deploy scan/i })).toBeVisible()
        await expect(page.getByRole('button', { name: /run all/i })).toBeVisible()
    })

    test('shows scan history table with past runs', async ({ page }) => {
        await expect(page.getByText(/scan history/i)).toBeVisible({ timeout: 10000 })
        const rows = page.locator('table tbody tr')
        await expect(rows.first()).toBeVisible({ timeout: 15000 })
        const count = await rows.count()
        expect(count).toBeGreaterThan(0)
    })
})

// ─── AGENTS PAGE ────────────────────────────────────────────────────────────

test.describe('Live Dashboard — Agents Page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${LIVE_URL}/agents`)
    })

    test('shows 3 connected agents', async ({ page }) => {
        await expect(page.getByText('cost-optimization-agent')).toBeVisible({ timeout: 15000 })
        await expect(page.getByText('deploy-agent')).toBeVisible()
        await expect(page.getByText('monitoring-agent')).toBeVisible()
    })

    test('agent cards show online status', async ({ page }) => {
        await expect(page.getByText('cost-optimization-agent')).toBeVisible({ timeout: 15000 })
        await expect(page.getByText(/online/i).first()).toBeVisible()
    })
})

// ─── DECISIONS PAGE ─────────────────────────────────────────────────────────

test.describe('Live Dashboard — Decisions Page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto(`${LIVE_URL}/decisions`)
    })

    test('renders decisions table with data rows', async ({ page }) => {
        await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible({ timeout: 10000 })
        const rows = page.locator('tbody tr')
        await expect(rows.first()).toBeVisible({ timeout: 15000 })
        const count = await rows.count()
        expect(count).toBeGreaterThan(0)
    })

    test('table shows verdict badges', async ({ page }) => {
        await expect(page.locator('tbody tr').first()).toBeVisible({ timeout: 15000 })
        await expect(
            page.locator('tbody').getByText(/approved|escalated|denied/i).first()
        ).toBeVisible({ timeout: 10000 })
    })

    test('has search and filter functionality', async ({ page }) => {
        // Decisions page uses select dropdowns for filtering, not a placeholder input
        const filterControls = page.locator('select')
            .or(page.getByPlaceholder(/search/i))
            .or(page.locator('[class*="filter"], [class*="Filter"]'))
        await expect(filterControls.first()).toBeVisible({ timeout: 10000 })
    })

    test('clicking a row opens drilldown with SRI details', async ({ page }) => {
        const firstRow = page.locator('tbody tr').first()
        await expect(firstRow).toBeVisible({ timeout: 15000 })
        await firstRow.click()

        await expect(
            page.getByText(/governance verdict|sri|risk/i).first()
        ).toBeVisible({ timeout: 15000 })
    })

    test('drilldown shows execution status section', async ({ page }) => {
        const firstRow = page.locator('tbody tr').first()
        await expect(firstRow).toBeVisible({ timeout: 15000 })
        await firstRow.click()

        // Execution Status is at the bottom of the drilldown — scroll it into view first.
        const executionStatus = page.getByText(/execution status/i)
        await executionStatus.first().scrollIntoViewIfNeeded()
        await expect(executionStatus.first()).toBeVisible({ timeout: 15000 })
    })

    test('drilldown back button returns to decisions list', async ({ page }) => {
        const firstRow = page.locator('tbody tr').first()
        await expect(firstRow).toBeVisible({ timeout: 15000 })
        await firstRow.click()

        await expect(
            page.getByText(/governance verdict|execution status/i).first()
        ).toBeVisible({ timeout: 15000 })

        const backButton = page.getByRole('button', { name: /back/i }).or(page.getByText(/← back/i))
        await backButton.first().click()

        await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible({ timeout: 5000 })
    })
})

// ─── ALERTS PAGE ────────────────────────────────────────────────────────────

test.describe('Live Dashboard — Alerts Page', () => {
    test('shows alert table with historical alerts', async ({ page }) => {
        await page.goto(`${LIVE_URL}/alerts`)
        const rows = page.locator('table tbody tr, [class*="row"]')
        await expect(rows.first()).toBeVisible({ timeout: 15000 })
    })

    test('has severity and status filters', async ({ page }) => {
        await page.goto(`${LIVE_URL}/alerts`)
        // Filters may be <select> elements, custom styled dropdowns, or search inputs.
        const filters = page.locator('select')
            .or(page.getByPlaceholder(/search/i))
            .or(page.locator('[class*="filter"], [class*="Filter"]'))
        await expect(filters.first()).toBeVisible({ timeout: 10000 })
    })
})

// ─── AUDIT LOG PAGE ─────────────────────────────────────────────────────────

test.describe('Live Dashboard — Audit Log Page', () => {
    test('shows scan run entries with agent/status columns', async ({ page }) => {
        await page.goto(`${LIVE_URL}/audit`)
        const rows = page.locator('tbody tr')
        await expect(rows.first()).toBeVisible({ timeout: 15000 })
    })

    test('has search, agent, and status filters', async ({ page }) => {
        await page.goto(`${LIVE_URL}/audit`)
        await expect(page.getByPlaceholder(/agent, scan id/i).or(page.getByPlaceholder(/search/i)).first()).toBeVisible({ timeout: 10000 })
    })

    test('has CSV and JSON export buttons', async ({ page }) => {
        await page.goto(`${LIVE_URL}/audit`)
        await expect(page.getByRole('button', { name: /csv/i })).toBeVisible({ timeout: 10000 })
        await expect(page.getByRole('button', { name: /json/i })).toBeVisible()
    })
})

// ─── ADMIN PAGE ─────────────────────────────────────────────────────────────

test.describe('Live Dashboard — Admin Page', () => {
    test('shows system configuration', async ({ page }) => {
        await page.goto(`${LIVE_URL}/admin`)
        // "System Configuration" appears in both heading and subtitle — use heading role to avoid strict mode violation
        await expect(page.getByRole('heading', { name: /system configuration/i })).toBeVisible({ timeout: 10000 })
        await expect(page.getByText(/mode/i).first()).toBeVisible()
        // "Live (Azure)" is rendered as a styled badge — text is split across DOM nodes.
        // Check for "Live" and "Azure" independently to avoid cross-node regex failure.
        await expect(page.getByText(/\blive\b/i).first()).toBeVisible()
        await expect(page.getByText(/azure/i).first()).toBeVisible()
        await expect(page.getByText(/llm timeout/i)).toBeVisible()
        await expect(page.getByText(/600s/i)).toBeVisible()
    })

    test('shows Danger Zone with reset button', async ({ page }) => {
        await page.goto(`${LIVE_URL}/admin`)
        await expect(page.getByText(/danger zone/i)).toBeVisible({ timeout: 10000 })
        await expect(page.getByRole('button', { name: /reset/i })).toBeVisible()
    })
})

// ─── BACKEND API HEALTH ─────────────────────────────────────────────────────

test.describe('Live Backend — API Health', () => {
    test('health endpoint returns 200', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/health`)
        expect(response.ok()).toBeTruthy()
    })

    test('metrics endpoint returns valid data', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/metrics`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('total_evaluations')
        expect(data).toHaveProperty('decisions')
        expect(data).toHaveProperty('decision_percentages')
        expect(data).toHaveProperty('sri_composite')
        expect(data.total_evaluations).toBeGreaterThan(0)
    })

    test('evaluations endpoint returns data', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/evaluations?limit=5`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data.evaluations.length).toBeLessThanOrEqual(5)
        expect(data.evaluations.length).toBeGreaterThan(0)
    })

    test('agents endpoint returns 3 agents', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/agents`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data.count).toBe(3)
        expect(data.agents).toHaveLength(3)
    })

    test('notification-status endpoint shows Slack configured', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/notification-status`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data.slack_configured).toBe(true)
        expect(data.slack_enabled).toBe(true)
    })

    test('config endpoint returns system info', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/config`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('mode')
        expect(data).toHaveProperty('llm_timeout')
        expect(data).toHaveProperty('execution_gateway_enabled')
    })

    test('alerts endpoint returns alert records', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/alerts`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('count')
        expect(data).toHaveProperty('alerts')
        expect(data.count).toBeGreaterThan(0)
    })

    test('scan-history endpoint returns scan records', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/scan-history`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('count')
        expect(data).toHaveProperty('scans')
        expect(data.count).toBeGreaterThan(0)
    })

    test('active-count endpoint returns number', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/alerts/active-count`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('active_count')
        expect(typeof data.active_count).toBe('number')
    })

    test('pending-reviews endpoint returns array', async ({ request }) => {
        const response = await request.get(`${BACKEND_URL}/api/execution/pending-reviews`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('count')
        expect(data).toHaveProperty('reviews')
    })
})
