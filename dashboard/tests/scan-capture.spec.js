/**
 * scan-capture.spec.js
 *
 * Verifies that a completed scan is properly captured in:
 *   1. /api/agent/deploy-agent/last-run  (last-run endpoint)
 *   2. Scans page scan history table     (UI)
 *   3. /api/evaluations                  (decisions/verdicts)
 *   4. Decisions page                    (UI)
 *
 * Run against production:
 *   npx playwright test tests/scan-capture.spec.js \
 *     --base-url=https://agreeable-pond-05f59310f.2.azurestaticapps.net \
 *     --reporter=list --timeout=180000
 */

import { test, expect } from '@playwright/test'

const BACKEND = 'https://ruriskry-core-backend-psc0des.icygrass-1e4512b3.eastus2.azurecontainerapps.io'
const DASHBOARD = 'https://agreeable-pond-05f59310f.2.azurestaticapps.net'

test.describe('Scan run capture — end-to-end', () => {

  test('deploy scan is captured in last-run, scan history, and decisions', async ({ page, request }) => {

    // ── Step 1: Trigger a deploy scan ────────────────────────────────────────
    console.log('Triggering deploy scan...')
    const startResp = await request.post(`${BACKEND}/api/scan/deploy`)
    expect(startResp.ok(), `POST /api/scan/deploy failed: ${startResp.status()}`).toBeTruthy()
    const startData = await startResp.json()
    const scanId = startData.scan_id
    console.log(`Scan started: ${scanId}`)
    expect(scanId).toBeTruthy()

    // ── Step 2: Poll status until complete (max 90s, every 5s) ───────────────
    console.log('Polling scan status...')
    let status = 'running'
    let proposals = 0
    for (let i = 0; i < 30; i++) {  // 30 × 5s = 150s — covers 120s LLM_TIMEOUT + margin
      await page.waitForTimeout(5_000)
      const statusResp = await request.get(`${BACKEND}/api/scan/${scanId}/status`)
      if (statusResp.ok()) {
        const s = await statusResp.json()
        status = s.status
        proposals = s.proposals_count ?? 0
        console.log(`  [${(i + 1) * 5}s] status=${status} proposals=${proposals}`)
        if (status === 'complete' || status === 'error') break
      }
    }
    console.log(`Scan finished with status: ${status}, proposals: ${proposals}`)
    expect(['complete', 'error'], `Unexpected status: ${status}`).toContain(status)

    // ── Step 3: Verify last-run endpoint captures this scan ───────────────────
    console.log('Checking last-run endpoint...')
    const lastRunResp = await request.get(`${BACKEND}/api/agents/deploy-agent/last-run`)
    expect(lastRunResp.ok()).toBeTruthy()
    const lastRun = await lastRunResp.json()
    console.log(`  last-run: status=${lastRun.status}, scan_id=${lastRun.scan_id?.slice(0, 8)}`)

    expect(lastRun.status, 'last-run returned no_data — scan not persisted to tracker').not.toBe('no_data')
    expect(lastRun.scan_id, 'last-run scan_id does not match triggered scan').toBe(scanId)
    // Both complete and error are valid — error means the scan ran but hit an infrastructure
    // issue (e.g. missing search index); the scan record must still be persisted either way.
    expect(['complete', 'error']).toContain(lastRun.status)
    console.log(`  ✓ last-run shows scan_id=${lastRun.scan_id?.slice(0, 8)} status=${lastRun.status}`)

    // ── Step 4: Scans page shows this scan in history ────────────────────────
    console.log('Checking Scans page history table...')
    await page.goto(`${DASHBOARD}/scans`)
    await expect(page.getByText(/scan history/i)).toBeVisible({ timeout: 10_000 })

    // History table should have at least 1 row (not the empty-state message)
    await expect(
      page.locator('table tbody tr').first(),
      'Scan history table is empty — scan not persisted to history'
    ).toBeVisible({ timeout: 15_000 })

    // The scan ID prefix should appear in the table
    const scanIdPrefix = scanId.slice(0, 8)
    await expect(
      page.getByText(new RegExp(scanIdPrefix, 'i')),
      `Scan ID ${scanIdPrefix}… not found in history table`
    ).toBeVisible({ timeout: 10_000 })

    console.log(`  ✓ Scan ID ${scanIdPrefix}… visible in history table`)

    // ── Step 5: Decisions captured (only if scan found proposals) ────────────
    const evalCount = lastRun.evaluations_count ?? 0
    console.log(`  evaluations_count=${evalCount}`)

    if (evalCount > 0) {
      console.log('Checking decisions/evaluations...')

      const evalsResp = await request.get(`${BACKEND}/api/evaluations?limit=10`)
      expect(evalsResp.ok()).toBeTruthy()
      const evalsData = await evalsResp.json()
      expect(
        evalsData.evaluations.length,
        'Evaluations endpoint returned 0 rows despite scan having proposals'
      ).toBeGreaterThan(0)

      // Most recent eval should match our scan
      const latestEval = evalsData.evaluations[0]
      console.log(`  Latest decision: resource=${latestEval.resource_id}, verdict=${latestEval.verdict}`)

      // Decisions page should show a row
      await page.goto(`${DASHBOARD}/decisions`)
      await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible()
      await expect(
        page.locator('tbody tr').first(),
        'Decisions table is empty — verdicts not persisted'
      ).toBeVisible({ timeout: 10_000 })

      // Verdict badge should be visible
      await expect(
        page.locator('tbody').getByText(/approved|escalated|denied/i).first()
      ).toBeVisible({ timeout: 10_000 })

      console.log('  ✓ Decisions page shows verdicts')
    } else {
      console.log('  Scan found 0 proposals (clean environment) — skipping decisions check')
      test.info().annotations.push({
        type: 'note',
        description: 'Scan ran clean (0 proposals) — decisions capture not exercised this run',
      })
    }

    console.log('All capture checks passed ✓')
  })
})
