/**
 * sse-streaming.spec.js — Phase 33B coverage
 *
 * Verifies the live scan log renders SSE events from /api/scan/{id}/stream
 * as they arrive: the workflow engine emits executor_invoked / executor_completed
 * mapped to evaluation/reasoning events, and a final scan_complete flips the
 * panel from "Scanning…" to "Complete".
 *
 * Strategy: stub window.EventSource so the test drives event timing directly.
 * page.route can't easily fake a streaming response — replacing the constructor
 * gives precise control over the timeline.
 *
 * Run:
 *   npx playwright test tests/sse-streaming.spec.js --config playwright.config.js
 */

import { test, expect } from '@playwright/test'

// ── Mocking helpers ──────────────────────────────────────────────────────────

async function bypassAuth(page) {
  await page.addInitScript(() => {
    localStorage.setItem('ruriskry_token', 'mock-test-token')
  })
  await page.route('**/api/auth/me', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ username: 'tester' }) }),
  )
  await page.route('**/api/auth/status', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ setup_required: false }) }),
  )
}

const AGENTS = [
  { name: 'cost-optimization-agent', type: 'cost' },
  { name: 'monitoring-agent',         type: 'monitoring' },
  { name: 'deploy-agent',             type: 'deploy' },
]

async function stubAppShellApis(page) {
  const json = (body) => route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(body),
  })

  // Playwright routes match in REVERSE registration order — register the
  // catch-all FIRST so specific routes registered after it take precedence.
  await page.route(/\/api\//, route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({}),
  }))

  await page.route(/\/api\/evaluations(\?|$)/, json({ evaluations: [], total: 0 }))
  await page.route('**/api/metrics', json({
    total_evaluations: 0, approved: 0, escalated: 0, denied: 0, sri_score_avg: 0,
  }))
  await page.route('**/api/agents', json({
    agents: AGENTS.map(a => ({
      agent_name: a.name,
      agent_type: a.type,
      status: 'connected',
      last_seen: new Date().toISOString(),
      action_count: 0,
    })),
  }))
  await page.route('**/api/execution/pending-reviews', json({ pending_reviews: [] }))
  await page.route('**/api/scan-history*', json({ scans: [] }))
  await page.route('**/api/alerts*', json({ alerts: [] }))
  await page.route('**/api/inventory/status*', json({ status: 'unknown' }))
  await page.route('**/api/notifications/status', json({ slack_configured: false }))
  await page.route('**/api/config', json({ mode: 'mock', use_workflows: true }))
}

/**
 * Replaces window.EventSource with a controllable fake. Each instance is
 * registered on window.__sseInstances keyed by URL so the test can find and
 * drive it later. Status polling is mocked to keep the scan in 'running' until
 * the test explicitly emits scan_complete.
 */
async function stubEventSource(page) {
  await page.addInitScript(() => {
    window.__sseInstances = {}
    class FakeEventSource {
      constructor(url) {
        this.url = url
        this.readyState = 0 // CONNECTING
        this.onmessage = null
        this.onerror = null
        this.onopen = null
        window.__sseInstances[url] = this
        // Open synchronously on next tick so listeners can attach first.
        queueMicrotask(() => {
          this.readyState = 1
          this.onopen?.({ target: this })
        })
      }
      close() {
        this.readyState = 2
        delete window.__sseInstances[this.url]
      }
    }
    // Drives one event from outside React.
    window.__pushSseEvent = (urlSubstring, payload) => {
      const url = Object.keys(window.__sseInstances).find(u => u.includes(urlSubstring))
      if (!url) throw new Error(`No EventSource open matching ${urlSubstring}`)
      const es = window.__sseInstances[url]
      es.onmessage?.({ data: JSON.stringify(payload) })
      return url
    }
    window.EventSource = FakeEventSource
  })
}

/**
 * Mocks the scan-trigger and status endpoints. Keeps every scan in 'running'
 * forever — the test ends the scan via the SSE stream, not via status polling.
 */
async function stubScanApis(page, scanId) {
  await page.route('**/api/scan/cost', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ status: 'started', scan_id: scanId, agent_type: 'cost' }),
  }))
  await page.route(/\/api\/scan\/[^/]+\/status$/, route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      scan_id: scanId, status: 'running', agent_type: 'cost',
      proposed_actions: [], evaluations: [],
      totals: { approved: 0, escalated: 0, denied: 0 },
      event_count: 0, last_event_at: null,
    }),
  }))
}

// ── Test ─────────────────────────────────────────────────────────────────────

test.describe('Live SSE scan log — Phase 33B', () => {
  test.beforeEach(async ({ page }) => {
    await bypassAuth(page)
    await stubAppShellApis(page)
    await stubEventSource(page)
  })

  test('renders streamed events and footer drops "streaming…" on scan_complete', async ({ page }) => {
    const scanId = 'sse-test-scan-0001'
    await stubScanApis(page, scanId)

    await page.goto('/agents')

    // Wait for the agent cards then trigger a Cost scan.
    await expect(page.getByText(/Cost Agent/i).first()).toBeVisible({ timeout: 15_000 })
    const runBtns = page.locator('button:has-text("Run Scan")')
    if (await runBtns.first().isVisible({ timeout: 3000 }).catch(() => false)) {
      // Click the Run Scan button on the Cost card. Use first card in DOM order.
      await runBtns.first().click()
      // Some flows show an inventory mode modal — pick "Skip" if present.
      const skip = page.locator('label:has-text("Skip inventory"), button:has-text("Skip")')
      if (await skip.first().isVisible({ timeout: 1500 }).catch(() => false)) {
        await skip.first().click()
      }
      const start = page.locator('button:has-text("Start Scan")')
      if (await start.isVisible({ timeout: 1500 }).catch(() => false)) {
        await start.click()
      }
    }

    // Live log panel opens with placeholder text.
    await expect(page.getByText(/Connecting to scan stream/i)).toBeVisible({ timeout: 10_000 })

    // Wait until the EventSource is registered for our scan_id.
    await expect.poll(async () => {
      return await page.evaluate((id) => {
        return Object.keys(window.__sseInstances).some(u => u.includes(id))
      }, scanId)
    }, { timeout: 5000 }).toBe(true)

    // Push a sequence of events covering the workflow → SSE mapping.
    const ts = () => new Date().toISOString()
    await page.evaluate(({ id, t }) => {
      window.__pushSseEvent(id, { event: 'scan_started', scan_id: id, timestamp: t, agent: 'cost', message: 'Starting cost scan' })
    }, { id: scanId, t: ts() })

    await page.evaluate(({ id, t }) => {
      window.__pushSseEvent(id, { event: 'discovery', scan_id: id, timestamp: t, agent: 'cost', message: 'Discovered 12 candidate resources' })
    }, { id: scanId, t: ts() })

    // executor_invoked → evaluation event in the SSE bridge
    await page.evaluate(({ id, t }) => {
      window.__pushSseEvent(id, { event: 'evaluation', scan_id: id, timestamp: t, agent: 'cost', executor: 'policy_agent', message: 'Evaluating proposal against POL-COST-001' })
    }, { id: scanId, t: ts() })

    // executor_completed → reasoning event
    await page.evaluate(({ id, t }) => {
      window.__pushSseEvent(id, { event: 'reasoning', scan_id: id, timestamp: t, agent: 'cost', executor: 'scoring', message: 'SRI composite=78 → APPROVED_IF (Tier 2)' })
    }, { id: scanId, t: ts() })

    // The placeholder vanishes once events arrive
    await expect(page.getByText(/Connecting to scan stream/i)).toBeHidden({ timeout: 5000 })

    // Each rendered message is visible
    await expect(page.getByText(/Discovered 12 candidate resources/)).toBeVisible()
    await expect(page.getByText(/Evaluating proposal against POL-COST-001/)).toBeVisible()
    await expect(page.getByText(/SRI composite=78/)).toBeVisible()

    // Footer counter reflects the total events. While streaming: "4 events · streaming…"
    await expect(page.getByText(/4 events · streaming/i)).toBeVisible()

    // Final verdict + scan_complete: SSE consumer adds to doneSet and the
    // "· streaming…" suffix disappears from the footer. We assert on the
    // footer (driven entirely by SSE) instead of the header "Complete" badge
    // (which is driven by status-poll state controlled by the parent page).
    await page.evaluate(({ id, t }) => {
      window.__pushSseEvent(id, {
        event: 'verdict', scan_id: id, timestamp: t, agent: 'cost',
        decision: 'APPROVED', sri_composite: 82, message: 'Verdict: APPROVED (SRI 82)',
      })
      window.__pushSseEvent(id, { event: 'scan_complete', scan_id: id, timestamp: t, agent: 'cost', message: 'Scan complete' })
    }, { id: scanId, t: ts() })

    await expect(page.getByText(/Verdict: APPROVED \(SRI 82\)/)).toBeVisible({ timeout: 5000 })
    // Footer no longer says "streaming…" — final state.
    await expect(page.getByText(/6 events$/)).toBeVisible({ timeout: 5000 })

    // EventSource was closed after scan_complete — instance no longer registered.
    await expect.poll(async () => {
      return await page.evaluate((id) =>
        Object.keys(window.__sseInstances).some(u => u.includes(id)),
      scanId)
    }, { timeout: 3000 }).toBe(false)
  })

  test('scan_error event renders error line and ends stream', async ({ page }) => {
    const scanId = 'sse-test-scan-error'
    await stubScanApis(page, scanId)

    await page.goto('/agents')

    await expect(page.getByText(/Cost Agent/i).first()).toBeVisible({ timeout: 15_000 })
    const runBtn = page.locator('button:has-text("Run Scan")').first()
    await runBtn.click()
    const skip = page.locator('label:has-text("Skip inventory"), button:has-text("Skip")')
    if (await skip.first().isVisible({ timeout: 1500 }).catch(() => false)) {
      await skip.first().click()
    }
    const start = page.locator('button:has-text("Start Scan")')
    if (await start.isVisible({ timeout: 1500 }).catch(() => false)) {
      await start.click()
    }

    await expect.poll(async () =>
      page.evaluate((id) => Object.keys(window.__sseInstances).some(u => u.includes(id)), scanId),
    { timeout: 5000 }).toBe(true)

    await page.evaluate(({ id, t }) => {
      window.__pushSseEvent(id, {
        event: 'scan_error', scan_id: id, timestamp: t, agent: 'cost',
        message: 'Framework error: discovery executor raised TimeoutError',
      })
    }, { id: scanId, t: new Date().toISOString() })

    await expect(page.getByText(/Framework error: discovery executor raised TimeoutError/)).toBeVisible({ timeout: 5000 })
    // SSE-driven done state: footer drops the "streaming…" suffix.
    await expect(page.getByText(/1 event$/)).toBeVisible({ timeout: 5000 })
  })
})
