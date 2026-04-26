/**
 * confirmation-modal.spec.js — Phase 34F coverage
 *
 * Verifies the A2 Validator → ConfirmationModal → executor handshake that
 * replaced the old `window.confirm` in PlaybookPanel.
 *
 * Strategy: drive the live built UI but route-mock every backend call so the
 * test is hermetic (no Cosmos, no LLM, no Foundry). Targets the local Vite
 * dev server by default; can be pointed at any deployment via PLAYWRIGHT_BASE_URL.
 *
 * Run:
 *   npx playwright test tests/confirmation-modal.spec.js --config playwright.config.js
 */

import { test, expect } from '@playwright/test'

// ── Test fixtures ────────────────────────────────────────────────────────────
const ACTION_ID = 'a3f2c1d0-1111-2222-3333-444455556666'

const PLAYBOOK = {
  action_id: ACTION_ID,
  template_id: 'restart_vm',
  az_command: 'az vm restart --resource-group rg-prod --name web-vm-01',
  executable_args: ['vm', 'restart', '--resource-group', 'rg-prod', '--name', 'web-vm-01'],
  expected_outcome: 'Restarts the VM. Brief downtime (~30s) during the reboot.',
  rollback_command: null,
  risk_level: 'low',
  estimated_duration_seconds: 60,
  requires_downtime: true,
}

const VERDICT = {
  evaluation_id: ACTION_ID,
  action_id: ACTION_ID,
  resource_id: '/subscriptions/test/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/web-vm-01',
  resource_type: 'Microsoft.Compute/virtualMachines',
  action_type: 'restart_vm',
  decision: 'ESCALATED',
  triage_tier: 3,
  triage_mode: 'workflow',
  sri_score: 62,
  reason: 'Tier 3 escalation — restart requires human approval.',
  confidence: 0.81,
  timestamp: new Date().toISOString(),
  agent_evaluations: [],
  proposed_action: {
    action_id: ACTION_ID,
    action_type: 'restart_vm',
    resource_id: '/subscriptions/test/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/web-vm-01',
    description: 'Restart unhealthy VM',
  },
}

const VALIDATOR_BRIEF_OK = {
  brief_id: 'brief-abc123',
  validator_status: 'ok',
  summary: 'Restarts the VM. Cause is non-destructive — VM disks and network attachments are preserved.',
  caveats: [
    'Brief downtime (~30s) while the VM reboots.',
    'In-memory state on the VM is lost.',
  ],
  risk_level: 'low',
  raw_text: 'Restarts the VM. Cause is non-destructive — VM disks and network attachments are preserved.',
}

const VALIDATOR_BRIEF_UNAVAILABLE = {
  brief_id: null,
  validator_status: 'unavailable',
  summary: '',
  caveats: [],
  risk_level: 'medium',
  raw_text: '⚠ Validator unavailable — review the command carefully before running.',
}

const EXECUTION_RECORD = {
  execution_id: 'exec-xyz789',
  action_id: ACTION_ID,
  mode: 'live',
  status: 'success',
  exit_code: 0,
  stdout: '{"status":"Succeeded"}',
  stderr: '',
  started_at: new Date().toISOString(),
  completed_at: new Date().toISOString(),
}

// ── Mocking helpers ──────────────────────────────────────────────────────────

/**
 * Bypass the React login flow by writing a session token to localStorage and
 * mocking /api/auth/me to validate it. Must be called before page.goto().
 */
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

/**
 * Stub every API call AppShell makes on initial load. We supply just enough
 * data so the shell renders without errors and the Decisions page lands on
 * our verdict.
 */
async function stubAppShellApis(page) {
  const json = (body, status = 200) => route => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  })

  // Playwright routes match in REVERSE registration order — register the
  // catch-all FIRST so specific routes registered after it take precedence.
  await page.route(/\/api\//, route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({}),
  }))

  await page.route(/\/api\/evaluations(\?|$)/, json({ evaluations: [VERDICT], total: 1 }))
  await page.route(/\/api\/evaluations\/[^/]+$/, json(VERDICT))
  await page.route('**/api/metrics', json({
    total_evaluations: 1, approved: 0, escalated: 1, denied: 0, sri_score_avg: 62,
  }))
  await page.route('**/api/agents', json({ agents: [] }))
  // Decisions.jsx auto-opens the drilldown when ?exec=<id> matches a pending
  // review whose execution_id equals <id> AND verdict_snapshot is populated.
  await page.route('**/api/execution/pending-reviews', json({
    pending_reviews: [{
      execution_id: ACTION_ID,
      action_id: ACTION_ID,
      status: 'awaiting_review',
      verdict_snapshot: VERDICT,
    }],
  }))
  await page.route('**/api/scan-history*', json({ scans: [] }))
  await page.route('**/api/alerts*', json({ alerts: [] }))
  await page.route('**/api/inventory/status*', json({ status: 'unknown' }))
  await page.route('**/api/notifications/status', json({ slack_configured: false }))
  await page.route('**/api/config', json({ mode: 'mock', use_workflows: true }))
  // Drilldown helpers — execution status fetches return "no record yet"
  await page.route(/\/api\/execution\/by-action\/.*/, json({ status: 'no_record' }))
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe('ConfirmationModal — Phase 34F validator + execution gate', () => {
  test.beforeEach(async ({ page }) => {
    await bypassAuth(page)
    await stubAppShellApis(page)
  })

  test('renders loading → brief, enables Confirm, and posts validator_brief_id on execute', async ({ page }) => {
    let validateCalls = 0
    let executeBody = null

    // Validator: delay 250ms so we can observe the loading state
    await page.route(/\/api\/decisions\/[^/]+\/validate$/, async route => {
      validateCalls++
      await new Promise(r => setTimeout(r, 250))
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(VALIDATOR_BRIEF_OK) })
    })
    await page.route(/\/api\/decisions\/[^/]+\/playbook$/, route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAYBOOK) }),
    )
    await page.route(/\/api\/decisions\/[^/]+\/playbook\/execute$/, async route => {
      executeBody = JSON.parse(route.request().postData() || '{}')
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EXECUTION_RECORD) })
    })

    await page.goto(`/decisions?exec=${ACTION_ID}`)

    // Open modal in dry-run mode
    const dryRunBtn = page.getByRole('button', { name: /Run as dry-run/i })
    await expect(dryRunBtn).toBeVisible({ timeout: 15_000 })
    await dryRunBtn.click()

    // Loading state — Confirm disabled, "Reviewing…" visible
    const confirmBtn = page.getByRole('button', { name: /Reviewing|Confirm/ })
    await expect(confirmBtn).toBeDisabled()
    await expect(page.getByText(/Validator is reviewing the command/i)).toBeVisible()

    // After validator resolves: brief renders, Confirm enables
    await expect(page.getByText(VALIDATOR_BRIEF_OK.summary)).toBeVisible({ timeout: 5000 })
    // Caveats list — use the exact bullet text so we don't collide with the
    // summary paragraph (which also contains the substring "brief downtime").
    await expect(page.getByText('In-memory state on the VM is lost.')).toBeVisible()
    // "reviewed by A2 Validator" text only appears in the modal — confirms the
    // modal is rendered with validator_status=ok (where the risk badge lives).
    await expect(page.getByText(/reviewed by A2 Validator/i)).toBeVisible()
    const confirmDryRun = page.getByRole('button', { name: /Confirm dry-run/i })
    await expect(confirmDryRun).toBeEnabled()

    // Execute and verify the brief was forwarded for audit linkage
    await confirmDryRun.click()
    await expect.poll(() => executeBody, { timeout: 5000 }).not.toBeNull()
    expect(executeBody.mode).toBe('dry_run')
    expect(executeBody.validator_brief_id).toBe(VALIDATOR_BRIEF_OK.brief_id)
    expect(executeBody.validator_brief_summary).toBe(VALIDATOR_BRIEF_OK.summary)
    expect(executeBody.validator_brief_caveats).toEqual(VALIDATOR_BRIEF_OK.caveats)
    expect(validateCalls).toBe(1)
  })

  test('Cancel closes the modal without calling the executor', async ({ page }) => {
    let executeCalled = false

    await page.route(/\/api\/decisions\/[^/]+\/validate$/, route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(VALIDATOR_BRIEF_OK) }),
    )
    await page.route(/\/api\/decisions\/[^/]+\/playbook$/, route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAYBOOK) }),
    )
    await page.route(/\/api\/decisions\/[^/]+\/playbook\/execute$/, route => {
      executeCalled = true
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EXECUTION_RECORD) })
    })

    await page.goto(`/decisions?exec=${ACTION_ID}`)
    await expect(page.getByRole('button', { name: /Run live/i })).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: /Run live/i }).click()

    // Wait until brief is resolved so Cancel is unambiguous
    await expect(page.getByText(VALIDATOR_BRIEF_OK.summary)).toBeVisible({ timeout: 5000 })
    await page.getByRole('button', { name: /^Cancel$/ }).click()

    // Modal gone, no execute call ever made
    await expect(page.getByText(VALIDATOR_BRIEF_OK.summary)).toBeHidden()
    await page.waitForTimeout(300)
    expect(executeCalled).toBe(false)
  })

  test('validator unavailable: amber fallback rendered, buttons still enabled', async ({ page }) => {
    await page.route(/\/api\/decisions\/[^/]+\/validate$/, route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(VALIDATOR_BRIEF_UNAVAILABLE) }),
    )
    await page.route(/\/api\/decisions\/[^/]+\/playbook$/, route =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAYBOOK) }),
    )

    await page.goto(`/decisions?exec=${ACTION_ID}`)
    await expect(page.getByRole('button', { name: /Run live/i })).toBeVisible({ timeout: 15_000 })
    await page.getByRole('button', { name: /Run live/i }).click()

    // Amber warning visible, Confirm enabled despite no real brief
    await expect(page.getByText(/Validator unavailable/i)).toBeVisible({ timeout: 5000 })
    const confirmLive = page.getByRole('button', { name: /Confirm and run live/i })
    await expect(confirmLive).toBeEnabled()
  })
})
