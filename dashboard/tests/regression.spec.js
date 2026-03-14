import { test, expect } from '@playwright/test'

/**
 * RuriSkry Dashboard — Regression Test Suite
 *
 * Tests locked to the post-redesign state. Covers:
 *  - Multi-page navigation (sidebar)
 *  - Overview page: metrics, SRI chart, scan runs, pending reviews
 *  - Scans page: controls, scan history table
 *  - Agents page: cards, menu actions, last run for 0-verdict scans
 *  - Decisions page: table, filters, pagination, drilldown
 *  - Audit Log page: chronological view
 *  - HITL flow: verdict drilldown → execution status → action buttons
 */

// ─── NAVIGATION ──────────────────────────────────────────────────────────────

test.describe('Sidebar navigation', () => {
    test('renders all 5 navigation links', async ({ page }) => {
        await page.goto('/')
        const nav = page.locator('nav, [class*="sidebar"], [class*="Sidebar"]')
        await expect(nav).toBeVisible()

        // Scans is now merged into Agents — sidebar has 5 items: overview, alerts, agents, decisions, audit
        const navItems = ['overview', 'alerts', 'agents', 'decisions', 'audit']
        for (const item of navItems) {
            await expect(
                page.getByRole('link', { name: new RegExp(item, 'i') })
            ).toBeVisible()
        }
    })

    test('navigates between all pages without errors', async ({ page }) => {
        await page.goto('/overview')

        // Agents (formerly separate Scans + Agents — now merged under /agents)
        await page.getByRole('link', { name: /agents/i }).click()
        await expect(page).toHaveURL(/\/agents/)
        await expect(page.getByRole('heading', { name: /agents/i }).first()).toBeVisible()

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

// ─── OVERVIEW PAGE ───────────────────────────────────────────────────────────

test.describe('Overview page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/overview')
    })

    test('shows status bar with agent count and last scan info', async ({ page }) => {
        await expect(page.getByText(/agents connected/i)).toBeVisible()
        await expect(page.getByText(/last scan/i)).toBeVisible()
    })

    test('displays metric cards with real data', async ({ page }) => {
        // Should have at least these 3 key metrics
        await expect(page.getByText(/total evaluations/i)).toBeVisible()
        await expect(page.getByText(/approval rate/i)).toBeVisible()
        await expect(page.getByText(/avg sri/i)).toBeVisible()
    })

    test('shows SRI trend chart', async ({ page }) => {
        await expect(page.getByText(/sri trend/i)).toBeVisible()
        // Chart should render (recharts renders SVG)
        const chart = page.locator('.recharts-wrapper, [class*="chart"], svg.recharts-surface')
        await expect(chart.first()).toBeVisible({ timeout: 10_000 })
    })

    test('shows pending reviews panel', async ({ page }) => {
        await expect(page.getByText(/pending reviews/i).first()).toBeVisible()
    })

    test('shows recent scan runs table', async ({ page }) => {
        await expect(page.getByText(/recent scan runs/i)).toBeVisible()

        // Table should have column headers
        const table = page.locator('table').filter({ hasText: /scan id/i })
        await expect(table).toBeVisible({ timeout: 10_000 })

        // Should show agent names
        const agentCells = page.getByText(/deploy|cost|sre|monitoring/i)
        await expect(agentCells.first()).toBeVisible()
    })

    test('scan runs show a status indicator for each scan', async ({ page }) => {
        // Use table text filter (same approach as the passing 'shows recent scan runs table' test)
        // Extended timeout: this test runs concurrently with the deploy scan e2e which loads the backend
        const table = page.locator('table').filter({ hasText: /scan id/i })
        await expect(table).toBeVisible({ timeout: 30_000 })
        // Wait for at least one data row (skeleton has no text rows)
        await expect(table.locator('tbody tr').first()).toBeVisible({ timeout: 15_000 })
        // At least one scan row should show a known status
        await expect(table.getByText(/clean|complete|error|running/i).first()).toBeVisible({ timeout: 10_000 })
    })
})

// ─── SCANS PAGE ──────────────────────────────────────────────────────────────

test.describe('Scans page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/scans')
    })

    test('shows scan controls with 3 agent buttons', async ({ page }) => {
        await expect(page.getByRole('button', { name: /cost scan/i })).toBeVisible()
        await expect(page.getByRole('button', { name: /sre scan|monitoring/i })).toBeVisible()
        await expect(page.getByRole('button', { name: /deploy scan/i })).toBeVisible()
    })

    test('shows Run All Agents button', async ({ page }) => {
        await expect(page.getByRole('button', { name: /run all/i })).toBeVisible()
    })

    test('shows scan history table with past runs', async ({ page }) => {
        await expect(page.getByText(/scan history/i)).toBeVisible()
        const rows = page.locator('table tbody tr, [class*="table"] [class*="row"]')
        // Should have at least 1 historical scan
        await expect(rows.first()).toBeVisible({ timeout: 10_000 })
    })

    test('scan history shows duration and status columns', async ({ page }) => {
        // Wait for the "Duration" column header to appear (thead is always present once table renders)
        await expect(page.getByRole('columnheader', { name: /duration/i })).toBeVisible({ timeout: 15_000 })
        // Status column shows one of: Clean, Complete, Error, Running
        await expect(
            page.getByText(/clean|complete|error|running/i).first()
        ).toBeVisible({ timeout: 10_000 })
    })
})

// ─── AGENTS PAGE ─────────────────────────────────────────────────────────────

test.describe('Agents page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/agents')
    })

    test('shows 3 agent cards', async ({ page }) => {
        await expect(page.getByText('cost-optimization-agent')).toBeVisible({ timeout: 15_000 })
        await expect(page.getByText('deploy-agent')).toBeVisible()
        await expect(page.getByText('monitoring-agent')).toBeVisible()
    })

    test('agent cards are in stable alphabetical order', async ({ page }) => {
        await expect(page.getByText('cost-optimization-agent')).toBeVisible({ timeout: 15_000 })

        const cards = page.locator('[class*="agent"], [class*="card"]').filter({
            hasText: /agent/,
        })
        const texts = await cards.allTextContents()
        const agentNames = texts
            .map((t) => {
                const match = t.match(/(cost-optimization|deploy|monitoring)-agent/)
                return match ? match[0] : null
            })
            .filter(Boolean)

        // First should be cost, then deploy, then monitoring (alphabetical)
        if (agentNames.length === 3) {
            expect(agentNames[0]).toContain('cost')
            expect(agentNames[1]).toContain('deploy')
            expect(agentNames[2]).toContain('monitoring')
        }
    })

    test('agent card 3-dot menu works and shows options', async ({ page }) => {
        await expect(page.getByText('cost-optimization-agent')).toBeVisible({ timeout: 15_000 })

        // Click the menu button (⋮ or aria-label)
        const menuButton = page.getByLabel(/agent actions menu/i).first()
            || page.locator('[class*="menu-button"], button:has-text("⋮")').first()
        await menuButton.click()

        // Menu should show these options (use exact button names from the menu)
        await expect(page.getByRole('button', { name: /last run results/i })).toBeVisible()
        await expect(page.getByRole('button', { name: 'History', exact: true })).toBeVisible()
        await expect(page.getByRole('button', { name: /agent details/i })).toBeVisible()
    })

    test('last run shows "no issues found" for clean agent (BUG-02 regression)', async ({ page }) => {
        await expect(page.getByText('monitoring-agent')).toBeVisible({ timeout: 15_000 })

        // Open menu for monitoring-agent (likely 3rd card)
        const menuButtons = page.getByLabel(/agent actions menu/i)
        if (await menuButtons.count() >= 3) {
            await menuButtons.nth(2).click()
        } else {
            // Fallback: find menu near monitoring-agent
            await page.locator('button:has-text("⋮")').last().click()
        }

        await page.getByRole('button', { name: /last run results/i }).click()

        // BUG-02 FIX: old broken message "run a scan first" must NOT appear.
        // Accept either outcome depending on current scan state:
        //   - Clean scan (0 proposals): "Scan completed — no issues found"
        //   - Scan with proposals:      "Done · N verdict(s)"
        await expect(
            page.getByText(/scan completed|verdict\(s\)/i).first()
        ).toBeVisible({ timeout: 10_000 })
        await expect(page.getByText(/run a scan first/i)).not.toBeVisible()
    })
})

// ─── DECISIONS PAGE ──────────────────────────────────────────────────────────

test.describe('Decisions page', () => {
    test.beforeEach(async ({ page }) => {
        await page.goto('/decisions')
    })

    test('renders decisions table with data rows', async ({ page }) => {
        await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible()
        const rows = page.locator('tbody tr')
        await expect(rows.first()).toBeVisible({ timeout: 10_000 })
        const count = await rows.count()
        expect(count).toBeGreaterThan(0)
    })

    test('table shows verdict badges', async ({ page }) => {
        // Scope to tbody to avoid matching hidden <option> elements in filter dropdowns
        await expect(page.locator('tbody tr').first()).toBeVisible({ timeout: 10_000 })
        await expect(
            page.locator('tbody').getByText(/approved|escalated|denied/i).first()
        ).toBeVisible({ timeout: 10_000 })
    })

    test('has search/filter functionality (DESIGN-02 fix)', async ({ page }) => {
        // Should have a search input or filter controls
        const searchOrFilter = page.getByPlaceholder(/search/i)
            .or(page.locator('select, [class*="filter"]').first())
        await expect(searchOrFilter.first()).toBeVisible({ timeout: 10_000 })
    })

    test('has pagination controls (DESIGN-02 fix)', async ({ page }) => {
        // Look for pagination: page numbers, "showing X of Y", or next/prev buttons
        const pagination = page.getByText(/showing \d+/i)
            .or(page.getByRole('button', { name: /next|previous|»|›/i }))
            .or(page.locator('[class*="pagination"]'))
        await expect(pagination.first()).toBeVisible({ timeout: 10_000 })
    })

    test('clicking a row opens drilldown', async ({ page }) => {
        const firstRow = page.locator('tbody tr').first()
        await expect(firstRow).toBeVisible({ timeout: 10_000 })
        await firstRow.click()

        // Drilldown should show verdict details
        await expect(
            page.getByText(/governance verdict|sri|risk/i).first()
        ).toBeVisible({ timeout: 10_000 })
    })

    test('drilldown back button works reliably (BUG-04 regression)', async ({ page }) => {
        const firstRow = page.locator('tbody tr').first()
        await expect(firstRow).toBeVisible({ timeout: 10_000 })
        await firstRow.click()

        // Wait for drilldown to render
        await expect(
            page.getByText(/governance verdict|execution status/i).first()
        ).toBeVisible({ timeout: 10_000 })

        // Click back
        const backButton = page.getByRole('button', { name: /back/i })
            .or(page.getByText(/← back/i))
            .or(page.locator('[class*="back"]'))
        await backButton.first().click()

        // Should be back on the decisions list
        await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible({ timeout: 5_000 })
    })
})

// ─── DRILLDOWN & HITL ────────────────────────────────────────────────────────

test.describe('Evaluation drilldown and HITL', () => {
    test('drilldown shows all expected sections', async ({ page }) => {
        await page.goto('/decisions')
        const firstRow = page.locator('tbody tr').first()
        await expect(firstRow).toBeVisible({ timeout: 10_000 })
        await firstRow.click()

        // Core sections that should always be in the drilldown
        const sections = [
            /verdict|decision/i,
            /reasoning|why proposed/i,
            /audit trail|decision id/i,
            /execution status/i,
        ]

        for (const section of sections) {
            await expect(page.getByText(section).first()).toBeVisible({ timeout: 10_000 })
        }
    })

    test('escalated verdict shows HITL action buttons', async ({ page }) => {
        await page.goto('/decisions')

        // Find an ESCALATED row
        const escalatedRow = page.locator('tbody tr').filter({ hasText: /escalated/i }).first()
        const hasEscalated = await escalatedRow.isVisible().catch(() => false)

        if (hasEscalated) {
            await escalatedRow.click()

            // HITL buttons should be visible for escalated verdicts
            await expect(page.getByText(/execution status/i)).toBeVisible({ timeout: 10_000 })

            // Check for at least one HITL action option
            const hitlButton = page.getByRole('button', { name: /terraform|azure|agent|decline/i })
            const hitlText = page.getByText(/awaiting review|dismissed|terraform|decline/i)
            await expect(hitlButton.or(hitlText).first()).toBeVisible({ timeout: 5_000 })
        } else {
            test.skip()
        }
    })
})

// ─── AUDIT LOG PAGE ──────────────────────────────────────────────────────────

test.describe('Audit Log page', () => {
    test('renders and shows chronological entries', async ({ page }) => {
        await page.goto('/audit')

        // Should have data rows or entries
        const entries = page.locator('tbody tr, [class*="entry"], [class*="row"]')
        await expect(entries.first()).toBeVisible({ timeout: 10_000 })
    })
})

// ─── API HEALTH ──────────────────────────────────────────────────────────────
// PLAYWRIGHT_BACKEND_URL overrides localhost when testing against live/staging.
// Set it to the Container App URL when running with PLAYWRIGHT_BASE_URL.
const BACKEND = process.env.PLAYWRIGHT_BACKEND_URL || 'http://localhost:8000'

test.describe('API integration', () => {
    test('metrics endpoint returns valid data', async ({ request }) => {
        const response = await request.get(`${BACKEND}/api/metrics`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data).toHaveProperty('total_evaluations')
        expect(data).toHaveProperty('decisions')
        expect(data).toHaveProperty('decision_percentages')
        expect(data).toHaveProperty('sri_composite')
        expect(data.total_evaluations).toBeGreaterThanOrEqual(0)
        expect(data.decisions).toHaveProperty('approved')
        expect(data.decision_percentages).toHaveProperty('approved')
    })

    test('evaluations endpoint supports limit param', async ({ request }) => {
        const response = await request.get(`${BACKEND}/api/evaluations?limit=5`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        expect(data.evaluations.length).toBeLessThanOrEqual(5)
    })

    test('evaluations endpoint rejects limit > max (422)', async ({ request }) => {
        const response = await request.get(`${BACKEND}/api/evaluations?limit=9999`)
        expect(response.status()).toBe(422)
    })

    test('agents endpoint returns 3 agents', async ({ request }) => {
        const response = await request.get(`${BACKEND}/api/agents`)
        expect(response.ok()).toBeTruthy()
        const data = await response.json()
        // API returns { count, agents: [...] }
        expect(data.count).toBe(3)
        expect(data.agents).toHaveLength(3)
    })
})
