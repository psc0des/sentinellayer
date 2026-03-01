/**
 * api.js — thin wrapper around the SentinelLayer FastAPI backend.
 *
 * All functions return parsed JSON or throw an Error with a human-readable
 * message that the UI can display.
 */

const BASE = 'http://localhost:8000/api'

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
