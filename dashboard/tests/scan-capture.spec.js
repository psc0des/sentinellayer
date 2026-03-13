/**
 * scan-capture.spec.js
 *
 * End-to-end tests that verify ALL 3 agent scans are properly captured:
 *   1. /api/agents/{agent}/last-run  (last-run endpoint)
 *   2. Scans page scan history table (UI)
 *   3. /api/evaluations              (decisions/verdicts)
 *   4. Decisions page                (UI)
 *
 * Also includes a visual UI scan that screenshots every dashboard page.
 *
 * Run against production:
 *   npx playwright test tests/scan-capture.spec.js \
 *     --config playwright.perf.config.js \
 *     --reporter=list --timeout=900000
 */

import { test, expect } from '@playwright/test'

const BACKEND = 'https://ruriskry-core-backend-psc0des.icygrass-1e4512b3.eastus2.azurecontainerapps.io'
const DASHBOARD = 'https://agreeable-pond-05f59310f.2.azurestaticapps.net'

// ─── Agent definitions ────────────────────────────────────────────────────────
const AGENTS = [
  { name: 'deploy',     scanPath: '/api/scan/deploy',     lastRunAgent: 'deploy-agent',             label: 'Deploy' },
  { name: 'monitoring', scanPath: '/api/scan/monitoring',  lastRunAgent: 'monitoring-agent',          label: 'Monitoring' },
  { name: 'cost',       scanPath: '/api/scan/cost',        lastRunAgent: 'cost-optimization-agent',   label: 'Cost' },
]

// ─── Helper: trigger scan and poll until complete ─────────────────────────────
async function triggerAndPollScan(request, page, agent) {
  console.log(`[${agent.label}] Triggering scan...`)
  const startResp = await request.post(`${BACKEND}${agent.scanPath}`)
  expect(startResp.ok(), `POST ${agent.scanPath} failed: ${startResp.status()}`).toBeTruthy()
  const startData = await startResp.json()
  const scanId = startData.scan_id
  console.log(`[${agent.label}] Scan started: ${scanId}`)
  expect(scanId).toBeTruthy()

  // Poll status until complete (max 90 × 10s = 900s — covers 600s LLM_TIMEOUT + governance)
  let status = 'running'
  let proposals = 0
  for (let i = 0; i < 90; i++) {
    await page.waitForTimeout(10_000)
    const statusResp = await request.get(`${BACKEND}/api/scan/${scanId}/status`)
    if (statusResp.ok()) {
      const s = await statusResp.json()
      status = s.status
      proposals = s.proposals_count ?? 0
      console.log(`  [${agent.label}] [${(i + 1) * 10}s] status=${status} proposals=${proposals}`)
      if (status === 'complete' || status === 'error') break
    }
  }
  console.log(`[${agent.label}] Finished: status=${status}, proposals=${proposals}`)
  return { scanId, status, proposals }
}

// ─── Helper: verify scan capture (last-run, UI, decisions) ────────────────────
async function verifyScanCapture(request, page, agent, scanId, testInfo) {
  // ── Verify last-run endpoint ──────────────────────────────────────────────
  console.log(`[${agent.label}] Checking last-run endpoint...`)
  const lastRunResp = await request.get(`${BACKEND}/api/agents/${agent.lastRunAgent}/last-run`)
  expect(lastRunResp.ok()).toBeTruthy()
  const lastRun = await lastRunResp.json()
  console.log(`  [${agent.label}] last-run: status=${lastRun.status}, scan_id=${lastRun.scan_id?.slice(0, 8)}`)

  expect(lastRun.status, `[${agent.label}] last-run returned no_data — scan not persisted`).not.toBe('no_data')
  expect(lastRun.scan_id, `[${agent.label}] last-run scan_id mismatch`).toBe(scanId)
  expect(['complete', 'error']).toContain(lastRun.status)
  console.log(`  [${agent.label}] ✓ last-run verified`)

  // ── Scans page shows this scan ────────────────────────────────────────────
  console.log(`[${agent.label}] Checking Scans page...`)
  await page.goto(`${DASHBOARD}/scans`)
  await expect(page.getByText(/scan history/i)).toBeVisible({ timeout: 10_000 })

  await expect(
    page.locator('table tbody tr').first(),
    `[${agent.label}] Scan history table is empty`
  ).toBeVisible({ timeout: 15_000 })

  const scanIdPrefix = scanId.slice(0, 8)
  await expect(
    page.getByText(new RegExp(scanIdPrefix, 'i')),
    `[${agent.label}] Scan ID ${scanIdPrefix}… not found in history table`
  ).toBeVisible({ timeout: 10_000 })
  console.log(`  [${agent.label}] ✓ Scan ID visible in history table`)

  // ── Decisions captured (only if scan found proposals) ─────────────────────
  const evalCount = lastRun.evaluations_count ?? 0
  console.log(`  [${agent.label}] evaluations_count=${evalCount}`)

  if (evalCount > 0) {
    console.log(`[${agent.label}] Checking decisions...`)
    const evalsResp = await request.get(`${BACKEND}/api/evaluations?limit=10`)
    expect(evalsResp.ok()).toBeTruthy()
    const evalsData = await evalsResp.json()
    expect(
      evalsData.evaluations.length,
      `[${agent.label}] Evaluations endpoint returned 0 rows`
    ).toBeGreaterThan(0)

    const latestEval = evalsData.evaluations[0]
    console.log(`  [${agent.label}] Latest decision: resource=${latestEval.resource_id}, verdict=${latestEval.verdict}`)

    // Decisions page
    await page.goto(`${DASHBOARD}/decisions`)
    await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible()
    await expect(
      page.locator('tbody tr').first(),
      `[${agent.label}] Decisions table is empty`
    ).toBeVisible({ timeout: 10_000 })

    await expect(
      page.locator('tbody').getByText(/approved|escalated|denied/i).first()
    ).toBeVisible({ timeout: 10_000 })
    console.log(`  [${agent.label}] ✓ Decisions page shows verdicts`)

    // Drilldown — execution status must NOT be "Failed"
    console.log(`[${agent.label}] Checking drilldown...`)
    await page.locator('tbody tr').first().click()
    await expect(
      page.getByText(/governance verdict|execution status/i).first()
    ).toBeVisible({ timeout: 10_000 })

    const isFailed = await page.getByText(/^failed$/i).isVisible().catch(() => false)
    if (isFailed) {
      const errorText = await page.getByText(/error|github|api/i).allTextContents().catch(() => [])
      throw new Error(`[${agent.label}] Execution status is Failed — details: ${errorText.join(' | ')}`)
    }
    console.log(`  [${agent.label}] ✓ Drilldown execution status is not Failed`)
  } else {
    console.log(`  [${agent.label}] 0 proposals (clean environment) — skipping decisions check`)
    testInfo.annotations.push({
      type: 'note',
      description: `${agent.label} scan ran clean (0 proposals) — decisions capture not exercised`,
    })
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// Test Suite 1: All 3 Agent Scans — End-to-End Capture
// ═══════════════════════════════════════════════════════════════════════════════

test.describe('Scan capture — all 3 agents', () => {

  // Each agent scan can take 10+ minutes. Set generous timeout per test.
  test.setTimeout(900_000)  // 15 minutes

  for (const agent of AGENTS) {
    test(`${agent.label} scan is captured in last-run, scan history, and decisions`, async ({ page, request }) => {
      const { scanId, status } = await triggerAndPollScan(request, page, agent)
      expect(['complete', 'error'], `[${agent.label}] Unexpected status: ${status}`).toContain(status)
      await verifyScanCapture(request, page, agent, scanId, test.info())
      console.log(`[${agent.label}] All capture checks passed ✓`)
    })
  }
})


// ═══════════════════════════════════════════════════════════════════════════════
// Test Suite 2: Concurrent 3-Agent Scan (triggers all at once, polls in parallel)
// ═══════════════════════════════════════════════════════════════════════════════

test.describe('Concurrent 3-agent scan', () => {

  test.setTimeout(900_000)

  test('trigger all 3 scans concurrently and verify capture', async ({ page, request }) => {

    // ── Trigger all 3 scans simultaneously ────────────────────────────────────
    console.log('Triggering all 3 scans concurrently...')
    const triggerResults = await Promise.all(
      AGENTS.map(async (agent) => {
        const resp = await request.post(`${BACKEND}${agent.scanPath}`)
        expect(resp.ok(), `POST ${agent.scanPath} failed: ${resp.status()}`).toBeTruthy()
        const data = await resp.json()
        console.log(`  [${agent.label}] scan_id=${data.scan_id}`)
        return { agent, scanId: data.scan_id }
      })
    )

    // ── Poll all 3 until complete ─────────────────────────────────────────────
    console.log('Polling all 3 scans...')
    const scanStates = triggerResults.map(t => ({
      ...t,
      status: 'running',
      proposals: 0,
      done: false,
    }))

    for (let i = 0; i < 90; i++) {
      await page.waitForTimeout(10_000)

      for (const scan of scanStates) {
        if (scan.done) continue
        const resp = await request.get(`${BACKEND}/api/scan/${scan.scanId}/status`)
        if (resp.ok()) {
          const s = await resp.json()
          scan.status = s.status
          scan.proposals = s.proposals_count ?? 0
          if (s.status === 'complete' || s.status === 'error') {
            scan.done = true
            console.log(`  [${scan.agent.label}] DONE at ${(i + 1) * 10}s — status=${s.status} proposals=${scan.proposals}`)
          }
        }
      }

      // Log progress every 30s
      if ((i + 1) % 3 === 0) {
        const summary = scanStates.map(s => `${s.agent.label}=${s.done ? s.status : 'running'}`).join(', ')
        console.log(`  [${(i + 1) * 10}s] ${summary}`)
      }

      if (scanStates.every(s => s.done)) break
    }

    // ── Verify all finished ───────────────────────────────────────────────────
    for (const scan of scanStates) {
      console.log(`[${scan.agent.label}] Final: status=${scan.status}, proposals=${scan.proposals}`)
      expect(
        ['complete', 'error'],
        `[${scan.agent.label}] Unexpected status: ${scan.status}`
      ).toContain(scan.status)
    }

    // ── Verify capture for each agent ─────────────────────────────────────────
    for (const scan of scanStates) {
      await verifyScanCapture(request, page, scan.agent, scan.scanId, test.info())
    }

    console.log('All 3 concurrent scans captured ✓')
  })
})


// ═══════════════════════════════════════════════════════════════════════════════
// Test Suite 3: Visual UI Scan — Screenshots of Every Dashboard Page
// ═══════════════════════════════════════════════════════════════════════════════

test.describe('Visual UI scan — all pages', () => {

  test.setTimeout(120_000)

  const PAGES = [
    { path: '/overview',   name: 'Overview',   waitFor: /overview|total scans/i },
    { path: '/scans',      name: 'Scans',      waitFor: /scan history|trigger scan/i },
    { path: '/agents',     name: 'Agents',     waitFor: /connected agents/i },
    { path: '/decisions',  name: 'Decisions',  waitFor: /decisions/i },
    { path: '/alerts',     name: 'Alerts',     waitFor: /alerts|alert activity/i },
    { path: '/audit',      name: 'AuditLog',   waitFor: /audit|scan history/i },
    { path: '/admin',      name: 'Admin',      waitFor: /admin|system configuration/i },
  ]

  for (const pg of PAGES) {
    test(`screenshot ${pg.name} page`, async ({ page }) => {
      await page.goto(`${DASHBOARD}${pg.path}`)

      // Wait for page content to load
      await expect(
        page.getByText(pg.waitFor).first()
      ).toBeVisible({ timeout: 15_000 })

      // Small delay for animations and data fetch
      await page.waitForTimeout(2_000)

      // Take full-page screenshot
      await page.screenshot({
        path: `screenshots/visual-scan-${pg.name.toLowerCase()}.png`,
        fullPage: true,
      })

      console.log(`✓ ${pg.name} screenshot captured`)
    })
  }
})
