/**
 * api.js — thin wrapper around the RuriSkry FastAPI backend.
 *
 * All functions return parsed JSON or throw an Error with a human-readable
 * message that the UI can display.
 */

export const BASE = 'http://localhost:8000/api'

/** Fetch recent evaluations, optionally filtered by resource ID substring. */
export async function fetchEvaluations(limit = 20, resourceId = null) {
  const params = new URLSearchParams({ limit })
  if (resourceId) params.append('resource_id', resourceId)
  const res = await fetch(`${BASE}/evaluations?${params}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch evaluations`)
  return res.json()
}

/** Fetch one evaluation by its action_id. */
export async function fetchEvaluation(id) {
  const res = await fetch(`${BASE}/evaluations/${id}`)
  if (!res.ok) throw new Error(`Evaluation "${id}" not found`)
  return res.json()
}

/** Fetch aggregate metrics across all evaluations. */
export async function fetchMetrics() {
  const res = await fetch(`${BASE}/metrics`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch metrics`)
  return res.json()
}

/** Fetch the risk profile for a specific resource. */
export async function fetchResourceRisk(resourceId) {
  const res = await fetch(`${BASE}/resources/${encodeURIComponent(resourceId)}/risk`)
  if (!res.ok) throw new Error(`No risk data found for "${resourceId}"`)
  return res.json()
}

/** Fetch all connected A2A agents with their governance stats. */
export async function fetchAgents() {
  const res = await fetch(`${BASE}/agents`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch agents`)
  return res.json()
}

/**
 * Trigger a single-agent background scan.
 * @param {'cost'|'monitoring'|'deploy'} type - which agent to run
 * @param {string|null} resourceGroup - optional Azure resource group to scope the scan
 * @returns {{ status: string, scan_id: string, agent_type: string }}
 */
export async function triggerScan(type, resourceGroup = null) {
  const body = resourceGroup ? { resource_group: resourceGroup } : {}
  const res = await fetch(`${BASE}/scan/${type}`, {
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
 * @returns {{ status: string, scan_ids: string[] }}
 */
export async function triggerAllScans(resourceGroup = null) {
  const body = resourceGroup ? { resource_group: resourceGroup } : {}
  const res = await fetch(`${BASE}/scan/all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to start all scans`)
  return res.json()
}

/**
 * Poll the status of a running scan.
 * @param {string} scanId - UUID returned by triggerScan / triggerAllScans
 * @returns {{ scan_id: string, status: string, evaluations: object[], ... }}
 */
export async function fetchScanStatus(scanId) {
  const res = await fetch(`${BASE}/scan/${scanId}/status`)
  if (!res.ok) throw new Error(`API error ${res.status}: scan "${scanId}" not found`)
  return res.json()
}

/**
 * Open an SSE stream for real-time scan progress events.
 * Returns an EventSource — caller must attach onmessage and call .close() when done.
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
  const res = await fetch(`${BASE}/scan/${scanId}/cancel`, { method: 'PATCH' })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to cancel scan "${scanId}"`)
  return res.json()
}

/**
 * Fetch the most recent scan results for an agent.
 * @param {string} agentName - e.g. "cost-optimization-agent"
 * @returns {{
 *   source: string,
 *   scan_id: string|null,
 *   status: string,
 *   started_at: string|null,
 *   completed_at: string|null,
 *   proposed_actions: object[],
 *   proposals_count: number,
 *   evaluations: object[],
 *   evaluations_count: number,
 *   totals: { approved: number, escalated: number, denied: number }
 * }}
 */
export async function fetchAgentLastRun(agentName) {
  const res = await fetch(`${BASE}/agents/${encodeURIComponent(agentName)}/last-run`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch last run for "${agentName}"`)
  return res.json()
}

/**
 * Fetch action history for one agent.
 * @param {string} agentName - e.g. "cost-optimization-agent"
 * @param {number} limit - max records
 */
export async function fetchAgentHistory(agentName, limit = 20) {
  const res = await fetch(`${BASE}/agents/${encodeURIComponent(agentName)}/history?limit=${limit}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch history for "${agentName}"`)
  return res.json()
}

/**
 * Fetch the Teams notification configuration status.
 * @returns {{ teams_configured: boolean, teams_enabled: boolean }}
 */
export async function fetchNotificationStatus() {
  const res = await fetch(`${BASE}/notification-status`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch notification status`)
  return res.json()
}

/**
 * Send a test notification to the configured Teams webhook.
 * @returns {{ status: string, reason?: string }}
 */
export async function testTeamsNotification() {
  const res = await fetch(`${BASE}/test-notification`, { method: 'POST' })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to send test notification`)
  return res.json()
}

/**
 * Fetch the decision explanation (with counterfactual analysis) for one evaluation.
 * @param {string} evaluationId - action_id UUID
 * @returns {Promise<object>} DecisionExplanation
 */
export async function fetchExplanation(evaluationId) {
  const res = await fetch(`${BASE}/evaluations/${encodeURIComponent(evaluationId)}/explanation`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch explanation`)
  return res.json()
}

/**
 * Fetch execution status for a governance verdict.
 * @param {string} actionId - action_id UUID from the governance verdict
 * @returns {{ status?: string, action_id: string, executions?: object[] }}
 */
export async function fetchExecutionStatus(actionId) {
  const res = await fetch(`${BASE}/execution/by-action/${encodeURIComponent(actionId)}`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch execution status`)
  return res.json()
}

/**
 * Human approves an escalated verdict for execution.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} reviewedBy - name/email of the approver
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function approveExecution(executionId, reviewedBy = 'dashboard-user') {
  const res = await fetch(`${BASE}/execution/${encodeURIComponent(executionId)}/approve`, {
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
  const res = await fetch(`${BASE}/execution/${encodeURIComponent(executionId)}/dismiss`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed_by: reviewedBy, reason }),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to dismiss execution`)
  return res.json()
}

/**
 * Create a Terraform PR from a manual_required execution record.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @param {string} reviewedBy - name/email of the person creating the PR
 * @returns {Promise<object>} Updated ExecutionRecord
 */
export async function createPRFromManual(executionId, reviewedBy = 'dashboard-user') {
  const res = await fetch(`${BASE}/execution/${encodeURIComponent(executionId)}/create-pr`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewed_by: reviewedBy }),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: failed to create PR`)
  return res.json()
}

/**
 * Preview the az CLI commands that would fix a manual_required issue.
 * @param {string} executionId - UUID of the ExecutionRecord
 * @returns {{ execution_id: string, action_type: string, resource_id: string, commands: string[], warning: string }}
 */
export async function fetchAgentFixPreview(executionId) {
  const res = await fetch(`${BASE}/execution/${encodeURIComponent(executionId)}/agent-fix-preview`)
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
  const res = await fetch(`${BASE}/execution/${encodeURIComponent(executionId)}/agent-fix-execute`, {
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
  const res = await fetch(`${BASE}/execution/${encodeURIComponent(executionId)}/terraform`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch Terraform stub`)
  return res.json()
}

/**
 * Fetch all execution records currently awaiting human review.
 * @returns {{ pending_reviews: object[] }}
 */
export async function fetchPendingReviews() {
  const res = await fetch(`${BASE}/execution/pending-reviews`)
  if (!res.ok) throw new Error(`API error ${res.status}: failed to fetch pending reviews`)
  return res.json()
}

/**
 * ⚠ Dev/test only — wipe all local JSON data and reset in-memory state.
 * Deletes data/decisions/, data/executions/, data/scans/ JSON files.
 * Cosmos DB data is never touched.
 * @returns {{ status: string, deleted: object, total: number }}
 */
export async function adminReset() {
  const res = await fetch(`${BASE}/admin/reset`, { method: 'POST' })
  if (!res.ok) throw new Error(`API error ${res.status}: reset failed`)
  return res.json()
}
