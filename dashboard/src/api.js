/**
 * api.js — thin wrapper around the RuriSkry FastAPI backend.
 *
 * All functions return parsed JSON or throw an Error with a human-readable
 * message that the UI can display.
 *
 * Auth: apiFetch() injects Authorization: Bearer <token> on every call.
 * The token is stored in localStorage under the key 'ruriskry_token'.
 * EventSource (SSE) calls keep using raw fetch/EventSource — they are GET
 * requests and the middleware does not gate GET endpoints.
 */

export const BASE = import.meta.env.VITE_API_URL + '/api'

// ── Auth token helpers ────────────────────────────────────────────────────

export function getToken() {
  return localStorage.getItem('ruriskry_token')
}

export function setToken(token) {
  localStorage.setItem('ruriskry_token', token)
}

export function clearToken() {
  localStorage.removeItem('ruriskry_token')
}

/**
 * Thin fetch wrapper that injects the session token as Authorization header.
 * On 401 it clears the stored token so the next React render shows the login
 * screen — it does NOT redirect (React Router handles that).
 */
async function apiFetch(url, options = {}) {
  const token = getToken()
  const headers = { ...(options.headers || {}) }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const res = await fetch(url, { ...options, headers })
  if (res.status === 401) {
    clearToken()
  }
  return res
}

// ── Auth API ──────────────────────────────────────────────────────────────

/**
 * Check whether the admin account has been created yet.
 * @returns {{ setup_required: boolean }}
 */
export async function authStatus() {
  const res = await fetch(`${BASE}/auth/status`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to check auth status`)
  return res.json()
}

/**
 * Create the initial admin account (first-time setup).
 * @param {string} username
 * @param {string} password — minimum 8 characters
 * @returns {{ token: string, username: string }}
 */
export async function authSetup(username, password) {
  const res = await fetch(`${BASE}/auth/setup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail ?? `Setup failed (${res.status})`)
  }
  return res.json()
}

/**
 * Log in with username + password.
 * @param {string} username
 * @param {string} password
 * @returns {{ token: string, username: string }}
 */
export async function authLogin(username, password) {
  const res = await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail ?? `Login failed (${res.status})`)
  }
  return res.json()
}

/**
 * Validate the stored session token and return the username.
 * Throws (401) if the session has expired or the token is invalid.
 * @returns {{ username: string }}
 */
export async function authMe() {
  const res = await apiFetch(`${BASE}/auth/me`)
  if (!res.ok) throw new Error('Session expired')
  return res.json()
}

/**
 * Log out — revoke the session token on the server.
 */
export async function authLogout() {
  await apiFetch(`${BASE}/auth/logout`, { method: 'POST' }).catch(() => {})
  clearToken()
}

// ── Governance data ────────────────────────────────────────────────────────

/** Fetch recent evaluations, optionally filtered by resource ID substring. */
export async function fetchEvaluations(limit = 20, resourceId = null) {
  const params = new URLSearchParams({ limit })
  if (resourceId) params.append('resource_id', resourceId)
  const res = await apiFetch(`${BASE}/evaluations?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch evaluations`)
  return res.json()
}

/** Fetch one evaluation by its action_id. */
export async function fetchEvaluation(id) {
  const res = await apiFetch(`${BASE}/evaluations/${id}`)
  if (!res.ok) throw new Error(`Evaluation "${id}" not found`)
  return res.json()
}

/** Fetch aggregate metrics across all evaluations. */
export async function fetchMetrics() {
  const res = await apiFetch(`${BASE}/metrics`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch metrics`)
  return res.json()
}

/** Fetch the risk profile for a specific resource. */
export async function fetchResourceRisk(resourceId) {
  const res = await apiFetch(`${BASE}/resources/${encodeURIComponent(resourceId)}/risk`)
  if (!res.ok) throw new Error(`No risk data found for "${resourceId}"`)
  return res.json()
}

/** Fetch all connected A2A agents with their governance stats. */
export async function fetchAgents() {
  const res = await apiFetch(`${BASE}/agents`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch agents`)
  return res.json()
}

/**
 * Trigger a single-agent background scan.
 * @param {'cost'|'monitoring'|'deploy'} type - which agent to run
 * @param {string|null} resourceGroup - optional Azure resource group to scope the scan
 * @param {string|null} subscriptionId - optional override for the configured subscription
 * @param {'existing'|'refresh'|'skip'} inventoryMode - how to handle inventory
 * @returns {{ status: string, scan_id: string, agent_type: string }}
 */
export async function triggerScan(type, resourceGroup = null, subscriptionId = null, inventoryMode = 'existing') {
  const body = {}
  if (resourceGroup) body.resource_group = resourceGroup
  if (subscriptionId) body.subscription_id = subscriptionId
  body.inventory_mode = inventoryMode
  const res = await apiFetch(`${BASE}/scan/${type}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to start ${type} scan`)
  return res.json()
}

/**
 * Trigger all three agent scans simultaneously.
 * @param {string|null} resourceGroup - optional Azure resource group
 * @param {string|null} subscriptionId - optional override for the configured subscription
 * @param {'existing'|'refresh'|'skip'} inventoryMode - how to handle inventory
 * @returns {{ status: string, scan_ids: string[] }}
 */
export async function triggerAllScans(resourceGroup = null, subscriptionId = null, inventoryMode = 'existing') {
  const body = {}
  if (resourceGroup) body.resource_group = resourceGroup
  if (subscriptionId) body.subscription_id = subscriptionId
  body.inventory_mode = inventoryMode
  const res = await apiFetch(`${BASE}/scan/all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to start all scans`)
  return res.json()
}

// ---------------------------------------------------------------------------
// Inventory API
// ---------------------------------------------------------------------------

/**
 * Trigger a background inventory refresh.
 * @param {string|null} subscriptionId - Azure subscription to query
 * @returns {{ status: string, refresh_id: string }}
 */
export async function refreshInventory(subscriptionId = null) {
  const res = await apiFetch(`${BASE}/inventory/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscription_id: subscriptionId }),
  })
  if (!res.ok) throw new Error(`Inventory refresh failed: ${res.status}`)
  return res.json()
}

/**
 * Poll the status of a background inventory refresh.
 * @param {string} refreshId - UUID returned by refreshInventory
 * @returns {{ status: string, resource_count?: number, error?: string }}
 */
export async function fetchRefreshStatus(refreshId) {
  const res = await apiFetch(`${BASE}/inventory/refresh/${refreshId}`)
  if (!res.ok) throw new Error(`Refresh status failed: ${res.status}`)
  return res.json()
}

/**
 * Fetch the latest inventory snapshot.
 * @param {string|null} subscriptionId
 * @param {boolean} summaryOnly - if true, omit resources array
 * @returns {object|null} inventory document, or null if not found
 */
export async function fetchInventory(subscriptionId = null, summaryOnly = false) {
  const params = new URLSearchParams()
  if (subscriptionId) params.set('subscription_id', subscriptionId)
  if (summaryOnly) params.set('summary_only', 'true')
  const res = await apiFetch(`${BASE}/inventory?${params}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`Fetch inventory failed: ${res.status}`)
  return res.json()
}

/**
 * Fetch lightweight inventory status (no resource list).
 * @param {string|null} subscriptionId
 * @returns {{ exists, refreshed_at, resource_count, type_summary, age_hours, stale }}
 */
export async function fetchInventoryStatus(subscriptionId = null) {
  const params = new URLSearchParams()
  if (subscriptionId) params.set('subscription_id', subscriptionId)
  const res = await apiFetch(`${BASE}/inventory/status?${params}`)
  if (!res.ok) throw new Error(`Inventory status failed: ${res.status}`)
  return res.json()
}

/**
 * Poll the status of a running scan.
 * @param {string} scanId - UUID returned by triggerScan / triggerAllScans
 * @returns {{ scan_id: string, status: string, evaluations: object[], ... }}
 */
export async function fetchScanStatus(scanId) {
  const res = await apiFetch(`${BASE}/scan/${scanId}/status`)
  if (!res.ok) throw new Error(`API error ${res.status}: scan "${scanId}" not found`)
  return res.json()
}

/**
 * Open an SSE stream for real-time scan progress events.
 * Returns an EventSource — caller must attach onmessage and call .close() when done.
 * EventSource uses GET and does not require auth headers.
 * @param {string} scanId - UUID returned by triggerScan
 * @returns {EventSource}
 */
export function streamScanEvents(scanId) {
  return new EventSource(`${BASE}/scan/${scanId}/stream`)
}

/**
 * Request cancellation of a running scan.
 * @param {string} scanId - UUID to cancel
 */
export async function cancelScan(scanId) {
  const res = await apiFetch(`${BASE}/scan/${scanId}/cancel`, { method: 'PATCH' })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to cancel scan "${scanId}"`)
  return res.json()
}

/**
 * Fetch the most recent scan results for an agent.
 * @param {string} agentName - e.g. "cost-optimization-agent"
 */
export async function fetchAgentLastRun(agentName) {
  const res = await apiFetch(`${BASE}/agents/${encodeURIComponent(agentName)}/last-run`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch last run for "${agentName}"`)
  return res.json()
}

/**
 * Fetch action history for one agent.
 * @param {string} agentName - e.g. "cost-optimization-agent"
 * @param {number} limit - max records
 */
export async function fetchAgentHistory(agentName, limit = 20) {
  const res = await apiFetch(`${BASE}/agents/${encodeURIComponent(agentName)}/history?limit=${limit}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch history for "${agentName}"`)
  return res.json()
}

/**
 * Fetch the Slack notification configuration status.
 * @returns {{ slack_configured: boolean, slack_enabled: boolean }}
 */
export async function fetchNotificationStatus() {
  const res = await apiFetch(`${BASE}/notification-status`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch notification status`)
  return res.json()
}

/**
 * Send a test notification to the configured Slack webhook.
 * @returns {{ status: string, reason?: string }}
 */
export async function testSlackNotification() {
  const res = await apiFetch(`${BASE}/test-notification`, { method: 'POST' })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to send test notification`)
  return res.json()
}

/**
 * Fetch the decision explanation (with counterfactual analysis) for one evaluation.
 * @param {string} evaluationId - action_id UUID
 * @returns {Promise<object>} DecisionExplanation
 */
export async function fetchExplanation(evaluationId) {
  const res = await apiFetch(`${BASE}/evaluations/${encodeURIComponent(evaluationId)}/explanation`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch explanation`)
  return res.json()
}

/**
 * Fetch execution status for a governance verdict.
 * @param {string} actionId - action_id UUID from the governance verdict
 * @returns {{ status?: string, action_id: string, executions?: object[] }}
 */
export async function fetchExecutionStatus(actionId) {
  const res = await apiFetch(`${BASE}/execution/by-action/${encodeURIComponent(actionId)}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch execution status`)
  return res.json()
}

/**
 * Fetch a single execution record by its execution_id.
 * @param {string} executionId
 * @returns {Promise<object>} ExecutionRecord
 */
export async function fetchExecutionRecord(executionId) {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/record`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch execution record`)
  return res.json()
}

/**
 * Human approves an escalated verdict for execution.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} reviewedBy - name/email of the approver
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function approveExecution(executionId, reviewedBy = 'dashboard-user') {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed_by: reviewedBy }),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to approve execution`)
  return res.json()
}

/**
 * Human dismisses a verdict — no execution will happen.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} reviewedBy - name/email of the person dismissing
 * @param {string} reason - optional reason for dismissal
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function dismissExecution(executionId, reviewedBy = 'dashboard-user', reason = '') {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/dismiss`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed_by: reviewedBy, reason }),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to dismiss execution`)
  return res.json()
}

/**
 * Analyse Terraform files to locate the block managing the target resource
 * and propose a human-confirmable attribute:value change.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} iacRepo - GitHub repo (e.g. "owner/repo")
 * @param {string} [iacPath] - Terraform subdirectory within the repo
 * @returns {Promise<object>} Resolve result — see API docs
 */
export async function resolveTfChange(executionId, iacRepo, iacPath = '') {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/resolve-tf-change`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      iac_repo: iacRepo,
      ...(iacPath ? { iac_path: iacPath } : {}),
    }),
  })
  if (!res.ok) {
    let detail = `API error ${res.status}: failed to analyse Terraform files`
    try { const body = await res.json(); if (body?.detail) detail = body.detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * Create a Terraform PR from a manual_required execution record.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} reviewedBy - name/email of the person creating the PR
 * @param {string} [iacRepo] - override detected repo (e.g. "owner/repo")
 * @param {string} [iacPath] - override detected Terraform path
 * @param {object|null} [confirmedChange] - human-confirmed change from resolve step
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function createPRFromManual(executionId, reviewedBy = 'dashboard-user', iacRepo = '', iacPath = '', confirmedChange = null) {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/create-pr`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reviewed_by: reviewedBy,
      ...(iacRepo ? { iac_repo: iacRepo } : {}),
      ...(iacPath ? { iac_path: iacPath } : {}),
      ...(confirmedChange ? { confirmed_change: confirmedChange } : {}),
    }),
  })
  if (!res.ok) {
    let detail = `API error ${res.status}: failed to create PR`
    try { const body = await res.json(); if (body?.detail) detail = body.detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * List GitHub repos accessible via the configured GITHUB_TOKEN.
 * Used by the Terraform PR overlay repo dropdown.
 * @returns {Promise<{ repos: string[] }>}
 */
export async function fetchGithubRepos() {
  const res = await apiFetch(`${BASE}/github/repos`)
  if (!res.ok) {
    let detail = `API error ${res.status}: failed to list GitHub repos`
    try { const body = await res.json(); if (body?.detail) detail = body.detail } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * Generate the LLM-driven execution plan for a manual_required issue.
 * @param {string} executionId - UUID of the ExecutionRecord
 */
export async function fetchAgentFixPreview(executionId) {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/agent-fix-preview`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch agent fix preview`)
  return res.json()
}

/**
 * Execute the az CLI fix commands for a manual_required record.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} reviewedBy - name/email of the person executing
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function executeAgentFix(executionId, reviewedBy = 'dashboard-user') {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/agent-fix-execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed_by: reviewedBy }),
  })
  if (!res.ok) {
    let detail = 'failed to execute agent fix'
    try { const b = await res.json(); if (b.detail) detail = b.detail } catch (_) {}
    throw new Error(`API error ${res.status}: ${detail}`)
  }
  return res.json()
}

/**
 * Fetch the Terraform HCL stub for a manual_required execution record.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @returns {{ execution_id: string, hcl: string }}
 */
export async function fetchTerraformStub(executionId) {
  const res = await apiFetch(`${BASE}/execution/${encodeURIComponent(executionId)}/terraform`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch Terraform stub`)
  return res.json()
}

/**
 * Fetch all execution records currently awaiting human review.
 * @returns {{ pending_reviews: object[] }}
 */
export async function fetchPendingReviews() {
  const res = await apiFetch(`${BASE}/execution/pending-reviews`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch pending reviews`)
  return res.json()
}

/**
 * Fetch all scan run records (newest-first) — operational audit log.
 * @param {number} limit - max records to return (1–500)
 */
export async function fetchScanHistory(limit = 100) {
  const res = await apiFetch(`${BASE}/scan-history?limit=${limit}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch scan history`)
  return res.json()
}

/**
 * Fetch all alert records newest-first.
 * @param {number} limit - max records to return (1–500)
 * @returns {{ count: number, alerts: object[] }}
 */
export async function fetchAlerts(limit = 100) {
  const res = await apiFetch(`${BASE}/alerts?limit=${limit}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch alerts`)
  return res.json()
}

/**
 * Fetch count of currently firing/investigating alerts.
 * @returns {{ active_count: number }}
 */
export async function fetchActiveAlertCount() {
  const res = await apiFetch(`${BASE}/alerts/active-count`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch active alert count`)
  return res.json()
}

/**
 * Fetch the full record for one alert (used for live-log polling).
 * @param {string} alertId
 * @returns {object} full alert record
 */
export async function fetchAlertStatus(alertId) {
  const res = await apiFetch(`${BASE}/alerts/${alertId}/status`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

/**
 * Manually trigger investigation for a pending alert.
 * @param {string} alertId - UUID of the pending alert
 * @returns {{ status: string, alert_id: string }}
 */
export async function investigateAlert(alertId) {
  const res = await apiFetch(`${BASE}/alerts/${alertId}/investigate`, { method: 'POST' })
  if (!res.ok) throw new Error(`API error ${res.status}: ${(await res.json().catch(() => ({}))).detail ?? 'failed to investigate alert'}`)
  return res.json()
}

/**
 * Open an SSE stream for real-time alert investigation progress.
 * Returns an EventSource — caller must attach onmessage and call .close() when done.
 * EventSource uses GET and does not require auth headers.
 * @param {string} alertId - UUID returned by trigger_alert
 * @returns {EventSource}
 */
export function streamAlertEvents(alertId) {
  return new EventSource(`${BASE}/alerts/${alertId}/stream`)
}

/**
 * ⚠ Dev/test only — wipe all local JSON data and reset in-memory state.
 * @returns {{ status: string, deleted: object, total: number }}
 */
export async function adminReset() {
  const res = await apiFetch(`${BASE}/admin/reset`, { method: 'POST' })
  if (!res.ok) throw new Error(`API error ${res.status}: reset failed`)
  return res.json()
}

/**
 * Fetch safe system configuration — mode, timeouts, feature flags.
 * @returns {{ mode: string, llm_timeout: number, llm_concurrency_limit: number,
 *             execution_gateway_enabled: boolean, use_live_topology: boolean, version: string }}
 */
export async function fetchConfig() {
  const res = await apiFetch(`${BASE}/config`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch config`)
  return res.json()
}

/**
 * Roll back a previously applied agent fix.
 * Only valid when status === 'applied'.
 * @param {string} executionId
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function rollbackAgentFix(executionId, reviewedBy = 'dashboard-user') {
  const res = await apiFetch(`${BASE}/execution/${executionId}/rollback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed_by: reviewedBy }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(`API error ${res.status}: ${body.detail ?? 'rollback failed'}`)
  }
  return res.json()
}

/**
 * Mark a human-required ApprovalCondition as satisfied.
 * @param {string} executionId
 * @param {number} conditionIndex
 * @param {string} satisfiedBy
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function satisfyCondition(executionId, conditionIndex, satisfiedBy) {
  const res = await apiFetch(`${BASE}/execution/${executionId}/condition/${conditionIndex}/satisfy`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ satisfied_by: satisfiedBy }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(`API error ${res.status}: ${body.detail ?? 'satisfy condition failed'}`)
  }
  return res.json()
}

/**
 * Force an immediate auto-check of one condition.
 * @param {string} executionId
 * @param {number} conditionIndex
 * @returns {Promise<{satisfied: boolean, record: object}>}
 */
export async function checkConditionNow(executionId, conditionIndex) {
  const res = await apiFetch(`${BASE}/execution/${executionId}/condition/${conditionIndex}/check`, {
    method: 'POST',
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(`API error ${res.status}: ${body.detail ?? 'condition check failed'}`)
  }
  return res.json()
}

/**
 * Admin force-execute: bypass unmet conditions with justification.
 * @param {string} executionId
 * @param {string} adminUser
 * @param {string} justification
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function forceExecuteConditional(executionId, adminUser, justification) {
  const res = await apiFetch(`${BASE}/execution/${executionId}/force-execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ admin_user: adminUser, justification }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(`API error ${res.status}: ${body.detail ?? 'force-execute failed'}`)
  }
  return res.json()
}

/**
 * Fetch the Tier 3 remediation playbook for a governance decision.
 * Returns null if no template exists (404) — caller renders a "not available" state.
 * @param {string} decisionId - action_id UUID from the governance verdict
 * @returns {Promise<object|null>} Playbook object or null
 */
export async function fetchPlaybook(decisionId) {
  const res = await apiFetch(`${BASE}/decisions/${encodeURIComponent(decisionId)}/playbook`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch playbook`)
  return res.json()
}
