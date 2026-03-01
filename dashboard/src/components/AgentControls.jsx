/**
 * AgentControls.jsx — panel for manually triggering operational agent scans.
 *
 * Shows four buttons (Cost, SRE, Deploy, Run All) with a resource-group input.
 * Each button:
 *   1. Calls POST /api/scan/{type} → receives a scan_id immediately
 *   2. Polls GET /api/scan/{scan_id}/status every 2 s until status !== "running"
 *   3. Calls onScanComplete() so the parent re-fetches evaluations
 *
 * Buttons are disabled while any scan of that type is running.
 */

import React, { useState, useRef, useCallback } from 'react'
import { triggerScan, triggerAllScans, fetchScanStatus } from '../api'

// ── Helpers ────────────────────────────────────────────────────────────────

const AGENT_LABELS = {
  cost:       'Cost Scan',
  monitoring: 'SRE Scan',
  deploy:     'Deploy Scan',
}

const AGENT_DESCRIPTIONS = {
  cost:       'Find idle or over-provisioned resources',
  monitoring: 'Check health metrics + anomaly alerts',
  deploy:     'Audit NSG rules and config drift',
}

const AGENT_ICONS = {
  cost:       '💰',
  monitoring: '📡',
  deploy:     '🔒',
}

// Status badge colours
function statusColour(status) {
  if (status === 'complete') return 'text-green-400'
  if (status === 'error')    return 'text-red-400'
  if (status === 'running')  return 'text-yellow-400'
  return 'text-slate-500'
}

// ── Spinner ────────────────────────────────────────────────────────────────

function Spinner() {
  return (
    <span
      className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin"
      aria-hidden="true"
    />
  )
}

// ── Single agent button ────────────────────────────────────────────────────

function AgentButton({ type, scanning, lastStatus, onTrigger }) {
  const isRunning = scanning[type]

  return (
    <button
      onClick={() => onTrigger(type)}
      disabled={isRunning}
      className={`
        flex flex-col items-start gap-1 p-4 rounded-xl border transition-all text-left w-full
        ${isRunning
          ? 'border-yellow-500/40 bg-yellow-500/5 cursor-not-allowed opacity-80'
          : 'border-slate-600 bg-slate-700/50 hover:bg-slate-700 hover:border-slate-500 cursor-pointer'
        }
      `}
    >
      {/* Top row: icon + label + spinner */}
      <div className="flex items-center gap-2 w-full">
        <span className="text-xl" aria-hidden="true">{AGENT_ICONS[type]}</span>
        <span className="flex-1 text-sm font-semibold text-slate-200">
          {AGENT_LABELS[type]}
        </span>
        {isRunning && <Spinner />}
      </div>

      {/* Description */}
      <p className="text-xs text-slate-500 leading-snug">{AGENT_DESCRIPTIONS[type]}</p>

      {/* Status line */}
      {lastStatus[type] && (
        <p className={`text-xs font-mono mt-0.5 ${statusColour(lastStatus[type].status)}`}>
          {lastStatus[type].status === 'running' && 'Scanning…'}
          {lastStatus[type].status === 'complete' && (
            `Done · ${lastStatus[type].evaluations_count ?? 0} verdict(s)`
          )}
          {lastStatus[type].status === 'error' && `Error: ${lastStatus[type].error}`}
        </p>
      )}
    </button>
  )
}

// ── Main component ─────────────────────────────────────────────────────────

/**
 * @param {{ onScanComplete: () => void }} props
 *   onScanComplete — called when any scan finishes so the parent can re-fetch
 *   evaluation data and update the dashboard.
 */
export default function AgentControls({ onScanComplete }) {
  const [resourceGroup, setResourceGroup] = useState('sentinel-prod-rg')

  // { cost: bool, monitoring: bool, deploy: bool }
  const [scanning,    setScanning]    = useState({ cost: false, monitoring: false, deploy: false })

  // { cost: statusObject|null, monitoring: ..., deploy: ... }
  const [lastStatus,  setLastStatus]  = useState({ cost: null, monitoring: null, deploy: null })

  // Store polling interval IDs so we can clear them when done.
  // useRef keeps these between renders without causing re-renders.
  const pollRefs = useRef({ cost: null, monitoring: null, deploy: null })

  /**
   * Start polling GET /api/scan/{scanId}/status every 2 s.
   * Stops automatically when status !== "running".
   */
  const startPolling = useCallback((scanId, agentType) => {
    // Clear any existing poll for this agent type
    if (pollRefs.current[agentType]) clearInterval(pollRefs.current[agentType])

    pollRefs.current[agentType] = setInterval(async () => {
      try {
        const result = await fetchScanStatus(scanId)
        setLastStatus(prev => ({ ...prev, [agentType]: result }))

        if (result.status !== 'running') {
          clearInterval(pollRefs.current[agentType])
          pollRefs.current[agentType] = null
          setScanning(prev => ({ ...prev, [agentType]: false }))
          onScanComplete()   // tell App.jsx to re-fetch evaluations
        }
      } catch {
        // Network hiccup — keep polling
      }
    }, 2_000)
  }, [onScanComplete])

  /**
   * Trigger one agent scan then begin polling for results.
   */
  const handleTrigger = useCallback(async (agentType) => {
    const rg = resourceGroup.trim() || null
    setScanning(prev => ({ ...prev, [agentType]: true }))
    setLastStatus(prev => ({ ...prev, [agentType]: { status: 'running' } }))
    try {
      const { scan_id } = await triggerScan(agentType, rg)
      startPolling(scan_id, agentType)
    } catch (err) {
      setScanning(prev => ({ ...prev, [agentType]: false }))
      setLastStatus(prev => ({ ...prev, [agentType]: { status: 'error', error: err.message } }))
    }
  }, [resourceGroup, startPolling])

  /**
   * Trigger all three scans simultaneously.
   * Each scan gets its own polling loop.
   */
  const handleTriggerAll = useCallback(async () => {
    const rg = resourceGroup.trim() || null
    setScanning({ cost: true, monitoring: true, deploy: true })
    setLastStatus({
      cost:       { status: 'running' },
      monitoring: { status: 'running' },
      deploy:     { status: 'running' },
    })
    try {
      const { scan_ids } = await triggerAllScans(rg)
      const types = ['cost', 'monitoring', 'deploy']
      scan_ids.forEach((scanId, i) => startPolling(scanId, types[i]))
    } catch (err) {
      setScanning({ cost: false, monitoring: false, deploy: false })
      const errStatus = { status: 'error', error: err.message }
      setLastStatus({ cost: errStatus, monitoring: errStatus, deploy: errStatus })
    }
  }, [resourceGroup, startPolling])

  const anyScanning = Object.values(scanning).some(Boolean)

  return (
    <section className="bg-slate-800 rounded-xl border border-slate-700 p-5">
      {/* ── Panel header ── */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
            Agent Controls
          </h2>
          <p className="text-xs text-slate-600 mt-0.5">
            Trigger ops agent scans directly from the dashboard
          </p>
        </div>

        {/* Global scanning badge */}
        {anyScanning && (
          <div className="flex items-center gap-1.5 text-xs text-yellow-400 font-mono">
            <Spinner />
            scanning…
          </div>
        )}
      </div>

      {/* ── Resource group input ── */}
      <div className="mb-4">
        <label className="block text-xs text-slate-500 mb-1" htmlFor="rg-input">
          Resource Group
        </label>
        <input
          id="rg-input"
          type="text"
          value={resourceGroup}
          onChange={e => setResourceGroup(e.target.value)}
          placeholder="sentinel-prod-rg"
          className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors font-mono"
        />
      </div>

      {/* ── Individual agent buttons ── */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
        {['cost', 'monitoring', 'deploy'].map(type => (
          <AgentButton
            key={type}
            type={type}
            scanning={scanning}
            lastStatus={lastStatus}
            onTrigger={handleTrigger}
          />
        ))}
      </div>

      {/* ── Run All button ── */}
      <button
        onClick={handleTriggerAll}
        disabled={anyScanning}
        className={`
          w-full py-2.5 rounded-xl text-sm font-semibold transition-all border
          ${anyScanning
            ? 'border-slate-600 bg-slate-700/40 text-slate-500 cursor-not-allowed'
            : 'border-blue-500/60 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 hover:text-blue-200 cursor-pointer'
          }
        `}
      >
        {anyScanning ? (
          <span className="flex items-center justify-center gap-2">
            <Spinner /> Running all agents…
          </span>
        ) : (
          '⚡ Run All Agents'
        )}
      </button>
    </section>
  )
}
