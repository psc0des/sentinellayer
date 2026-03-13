/**
 * perf-benchmark.spec.js
 *
 * Before/after performance benchmark for model migration (gpt-4.1 → gpt-5-mini).
 *
 * Run against production backend directly:
 *   PERF_LABEL=before npx playwright test perf-benchmark --config playwright.perf.config.js
 *   PERF_LABEL=after  npx playwright test perf-benchmark --config playwright.perf.config.js
 *
 * Results saved to: dashboard/tests/results/perf-<label>-<timestamp>.json
 * Summary printed to stdout for quick comparison.
 *
 * What is measured:
 *   1. API latency   — GET endpoints called 5× each, reports p50/p95/max (ms)
 *   2. Page load     — each dashboard page, time to meaningful content visible
 *   3. Scan pipeline — trigger deploy scan, measure time to first verdict + total time
 *   4. Verdict throughput — proposals/min derived from scan timing
 */

import { test, expect } from '@playwright/test'
import * as fs from 'fs'
import * as path from 'path'

// ── Config ─────────────────────────────────────────────────────────────────

const BACKEND = process.env.PERF_BACKEND_URL
  || 'https://ruriskry-core-backend-psc0des.icygrass-1e4512b3.eastus2.azurecontainerapps.io'

const FRONTEND = process.env.PERF_FRONTEND_URL
  || 'https://agreeable-pond-05f59310f.2.azurestaticapps.net'

const LABEL   = process.env.PERF_LABEL || 'unlabelled'
const SCAN_RG = process.env.PERF_SCAN_RG || 'ruriskry-prod-rg'

// ── Helpers ────────────────────────────────────────────────────────────────

/** Run fn() N times, return sorted array of durations (ms). */
async function measureN(fn, n = 5) {
  const times = []
  for (let i = 0; i < n; i++) {
    const start = Date.now()
    await fn()
    times.push(Date.now() - start)
  }
  return times.sort((a, b) => a - b)
}

function p50(sorted)  { return sorted[Math.floor(sorted.length * 0.50)] }
function p95(sorted)  { return sorted[Math.floor(sorted.length * 0.95)] ?? sorted[sorted.length - 1] }
function avg(sorted)  { return Math.round(sorted.reduce((s, v) => s + v, 0) / sorted.length) }

function statsOf(sorted) {
  return { p50: p50(sorted), p95: p95(sorted), max: sorted[sorted.length - 1], avg: avg(sorted) }
}

function saveResults(results) {
  const dir = path.join(import.meta.dirname, 'results')
  fs.mkdirSync(dir, { recursive: true })
  const ts  = new Date().toISOString().replace(/[:.]/g, '-')
  const file = path.join(dir, `perf-${LABEL}-${ts}.json`)
  fs.writeFileSync(file, JSON.stringify(results, null, 2))
  console.log(`\n📁 Results saved → ${file}`)
  return file
}

function printTable(results) {
  console.log(`\n${'═'.repeat(68)}`)
  console.log(`  RuriSkry Performance Benchmark  [${results.label}]  ${results.timestamp}`)
  console.log(`  Model: ${results.model_label}   Backend: ${BACKEND.split('/').pop()}`)
  console.log(`${'═'.repeat(68)}`)

  console.log('\n── API Latency (ms, 5 samples each) ──────────────────────────────')
  console.log(`  ${'Endpoint'.padEnd(30)} ${'p50'.padStart(6)} ${'p95'.padStart(6)} ${'max'.padStart(6)} ${'avg'.padStart(6)}`)
  console.log(`  ${'-'.repeat(54)}`)
  for (const [name, s] of Object.entries(results.api_latency)) {
    console.log(`  ${name.padEnd(30)} ${String(s.p50).padStart(6)} ${String(s.p95).padStart(6)} ${String(s.max).padStart(6)} ${String(s.avg).padStart(6)}`)
  }

  console.log('\n── Page Load (ms, time to visible content) ───────────────────────')
  for (const [page, ms] of Object.entries(results.page_load)) {
    const bar = '█'.repeat(Math.min(30, Math.round(ms / 100)))
    console.log(`  ${page.padEnd(16)} ${String(ms).padStart(5)} ms  ${bar}`)
  }

  if (results.scan_pipeline) {
    const s = results.scan_pipeline
    console.log('\n── Scan Pipeline (deploy scan, live LLM) ─────────────────────────')
    console.log(`  Proposals found:      ${s.proposals_found}`)
    console.log(`  Time to first verdict: ${s.time_to_first_verdict_ms ?? 'n/a'} ms`)
    console.log(`  Total scan time:       ${s.total_scan_time_ms} ms`)
    console.log(`  Throughput:            ${s.verdicts_per_min.toFixed(1)} verdicts/min`)
    console.log(`  Scan outcome:          ${s.outcome}`)
  }

  console.log(`\n${'═'.repeat(68)}\n`)
}

// ── Test suite ─────────────────────────────────────────────────────────────

let results = {}

test.describe.serial('Performance Benchmark', () => {

  test.beforeAll(async () => {
    results = {
      label:     LABEL,
      timestamp: new Date().toISOString(),
      model_label: LABEL === 'before' ? 'gpt-4.1 (current)' : 'gpt-5-mini (new)',
      api_latency:   {},
      page_load:     {},
      scan_pipeline: null,
    }
  })

  // ── 1. API Latency ───────────────────────────────────────────────────────

  test('API: GET /api/health', async ({ request }) => {
    const times = await measureN(() => request.get(`${BACKEND}/health`), 5)
    results.api_latency['GET /health'] = statsOf(times)
  })

  test('API: GET /api/evaluations', async ({ request }) => {
    const times = await measureN(
      () => request.get(`${BACKEND}/api/evaluations?limit=20`), 5
    )
    results.api_latency['GET /evaluations'] = statsOf(times)
  })

  test('API: GET /api/metrics', async ({ request }) => {
    const times = await measureN(() => request.get(`${BACKEND}/api/metrics`), 5)
    results.api_latency['GET /metrics']      = statsOf(times)
  })

  test('API: GET /api/scan-history', async ({ request }) => {
    const times = await measureN(
      () => request.get(`${BACKEND}/api/scan-history?limit=10`), 5
    )
    results.api_latency['GET /scan-history'] = statsOf(times)
  })

  test('API: GET /api/alerts', async ({ request }) => {
    const times = await measureN(() => request.get(`${BACKEND}/api/alerts`), 5)
    results.api_latency['GET /alerts']       = statsOf(times)
  })

  test('API: GET /api/config', async ({ request }) => {
    const times = await measureN(() => request.get(`${BACKEND}/api/config`), 5)
    results.api_latency['GET /config']       = statsOf(times)
  })

  // ── 2. Page Load ─────────────────────────────────────────────────────────

  test('Page load: /overview', async ({ page }) => {
    const start = Date.now()
    await page.goto(`${FRONTEND}/overview`)
    await expect(page.getByText(/agents connected/i)).toBeVisible({ timeout: 15_000 })
    results.page_load['overview'] = Date.now() - start
  })

  test('Page load: /scans', async ({ page }) => {
    const start = Date.now()
    await page.goto(`${FRONTEND}/scans`)
    await expect(page.getByText(/Scan history/i)).toBeVisible({ timeout: 15_000 })
    results.page_load['scans'] = Date.now() - start
  })

  test('Page load: /decisions', async ({ page }) => {
    const start = Date.now()
    await page.goto(`${FRONTEND}/decisions`)
    await expect(page.getByRole('heading', { name: /decisions/i })).toBeVisible({ timeout: 15_000 })
    results.page_load['decisions'] = Date.now() - start
  })

  test('Page load: /alerts', async ({ page }) => {
    const start = Date.now()
    await page.goto(`${FRONTEND}/alerts`)
    await expect(page.getByText(/Alerts|Alert/i).first()).toBeVisible({ timeout: 15_000 })
    results.page_load['alerts'] = Date.now() - start
  })

  test('Page load: /audit', async ({ page }) => {
    const start = Date.now()
    await page.goto(`${FRONTEND}/audit`)
    await expect(page.getByRole('heading', { name: /audit log/i })).toBeVisible({ timeout: 15_000 })
    results.page_load['audit'] = Date.now() - start
  })

  // ── 3. Scan Pipeline (live LLM latency) ──────────────────────────────────

  test('Scan pipeline: deploy scan end-to-end', async ({ request }) => {
    // Trigger deploy scan
    const triggerStart = Date.now()
    const trigRes = await request.post(`${BACKEND}/api/scan/deploy`, {
      data: { resource_group: SCAN_RG },
    })
    expect(trigRes.ok()).toBeTruthy()
    const { scan_id } = await trigRes.json()

    let firstVerdictMs = null
    let totalMs        = null
    let outcome        = 'unknown'
    let proposals      = 0
    const pollStart    = Date.now()
    const TIMEOUT_MS   = 180_000

    // Poll until complete (max 3 min)
    while (Date.now() - pollStart < TIMEOUT_MS) {
      await new Promise(r => setTimeout(r, 3_000))

      const statusRes = await request.get(`${BACKEND}/api/scan/${scan_id}/status`)
      if (!statusRes.ok()) continue
      const status = await statusRes.json()

      // Detect first verdict
      if (firstVerdictMs === null && (status.evaluations ?? []).length > 0) {
        firstVerdictMs = Date.now() - triggerStart
      }

      if (status.status !== 'running') {
        totalMs   = Date.now() - triggerStart
        proposals = (status.evaluations ?? []).length
        outcome   = status.scan_error
          ? (status.scan_error.includes('429') ? '429_rate_limit' : 'error')
          : status.status
        break
      }
    }

    if (totalMs === null) {
      totalMs = TIMEOUT_MS
      outcome = 'timeout'
    }

    const verdictsPm = proposals > 0 && totalMs > 0
      ? (proposals / (totalMs / 60_000))
      : 0

    results.scan_pipeline = {
      scan_id,
      proposals_found:          proposals,
      time_to_first_verdict_ms: firstVerdictMs,
      total_scan_time_ms:       totalMs,
      verdicts_per_min:         verdictsPm,
      outcome,
    }
  })

  // ── Save & print ─────────────────────────────────────────────────────────

  test.afterAll(() => {
    printTable(results)
    saveResults(results)
  })
})
