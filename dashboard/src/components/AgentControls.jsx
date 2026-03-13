/**
 * AgentControls.jsx — panel for manually triggering operational agent scans.
 *
 * Shows four buttons (Cost, SRE, Deploy, Run All) with a resource-group input.
 * Each button:
 *   1. Calls POST /api/scan/{type} → receives a scan_id immediately
 *   2. Opens a LiveLogPanel that streams real-time SSE events for that scan
 *   3. Polls GET /api/scan/{scan_id}/status every 2 s until status !== "running"
 *   4. Calls onScanComplete() so the parent re-fetches evaluations
 *
 * Bug 1 fix: the "Run All" button now only shows "Running all agents…" when ALL
 * three agents are scanning simultaneously (i.e. via Run All).  A single-agent
 * scan correctly shows "Scan in progress…" on the Run All button instead.
 */

import React, { useState, useRef, useCallback } from 'react'
import { triggerScan, triggerAllScans, fetchScanStatus } from '../api'
import LiveLogPanel from './LiveLogPanel'
import { DollarSign, Activity, Shield, Zap, ClipboardList } from 'lucide-react'

// ── Helpers ────────────────────────────────────────────────────────────────

const AGENT_LABELS = {
  cost:       'Cost Scan',
  monitoring: 'Monitoring Scan',
  deploy:     'Deploy Scan',
}

const AGENT_DESCRIPTIONS = {
  cost:       'Find idle or over-provisioned resources',
  monitoring: 'Check health metrics + anomaly alerts',
  deploy:     'Audit NSG rules and config drift',
}

const AGENT_ICON_CMP = {
  cost:       DollarSign,
  monitoring: Activity,
  deploy:     Shield,
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

function AgentButton({ type, scanning, lastStatus, onTrigger, onViewResults }) {
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
        {(() => { const Icon = AGENT_ICON_CMP[type]; return Icon ? <Icon className="w-4 h-4 text-slate-400 shrink-0" /> : null })()}
        <span className="flex-1 text-sm font-semibold text-slate-200">
          {AGENT_LABELS[type]}
        </span>
        {isRunning && <Spinner />}
      </div>

      {/* Description */}
      <p className="text-xs text-slate-500 leading-snug">{AGENT_DESCRIPTIONS[type]}</p>

      {/* Status line */}
      {lastStatus[type] && (
        lastStatus[type].status === 'complete' ? (
          // Clickable only when there are verdicts to show
          (lastStatus[type].evaluations_count ?? 0) > 0 ? (
            <span
              className="text-xs font-mono mt-0.5 text-green-400 underline decoration-dotted cursor-pointer hover:text-green-300"
              title="Click to view verdict details"
              onClick={(e) => { e.stopPropagation(); onViewResults?.() }}
            >
              Done · {lastStatus[type].evaluations_count} verdict(s) →
            </span>
          ) : (
            <span className="text-xs font-mono mt-0.5 text-slate-500">
              Done · 0 verdicts (no issues found)
            </span>
          )
        ) : (
          <p className={`text-xs font-mono mt-0.5 ${statusColour(lastStatus[type].status)}`}>
            {lastStatus[type].status === 'running' && 'Scanning…'}
            {lastStatus[type].status === 'error' && (
              lastStatus[type].scan_error?.includes('429') || lastStatus[type].scan_error?.includes('Too Many')
                ? 'Rate limited — wait 60s and retry'
                : lastStatus[type].scan_error
                  ? `Error: ${lastStatus[type].scan_error.slice(0, 80)}…`
                  : lastStatus[type].error
                    ? `Error: ${lastStatus[type].error}`
                    : 'Agent framework error'
            )}
          </p>
        )
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
export default function AgentControls({ onScanComplete, onViewVerdicts }) {
  // Default empty → API sends null resource_group → agents scan whole subscription.
  const [resourceGroup, setResourceGroup] = useState('')

  // Per-agent scanning state: { cost: bool, monitoring: bool, deploy: bool }
  const [scanning,   setScanning]   = useState({ cost: false, monitoring: false, deploy: false })

  // Per-agent last status from poll: { cost: obj|null, monitoring: ..., deploy: ... }
  const [lastStatus, setLastStatus] = useState({ cost: null, monitoring: null, deploy: null })

  // Live log panel state: which scan_id + agent_type to show.
  const [liveLog, setLiveLog] = useState({ open: false, scanId: null, agentType: null, scanEntries: null })

  // Store polling interval IDs so we can clear them when done.
  const pollRefs = useRef({ cost: null, monitoring: null, deploy: null })

  /**
   * Start polling GET /api/scan/{scanId}/status every 2 s.
   * Stops automatically when status !== "running".
   */
  const startPolling = useCallback((scanId, agentType) => {
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
   * Trigger one agent scan, open live log, then begin polling for results.
   */
  const handleTrigger = useCallback(async (agentType) => {
    const rg = resourceGroup.trim() || null
    setScanning(prev => ({ ...prev, [agentType]: true }))
    setLastStatus(prev => ({ ...prev, [agentType]: { status: 'running' } }))
    try {
      const { scan_id } = await triggerScan(agentType, rg)
      // Open the live log panel for this scan
      setLiveLog({ open: true, scanId: scan_id, agentType })
      startPolling(scan_id, agentType)
    } catch (err) {
      setScanning(prev => ({ ...prev, [agentType]: false }))
      setLastStatus(prev => ({ ...prev, [agentType]: { status: 'error', error: err.message } }))
    }
  }, [resourceGroup, startPolling])

  /**
   * Trigger all three scans simultaneously.
   * Opens the live log for the cost scan (first one).
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
      // Open merged log panel showing all 3 agents' streams simultaneously
      if (scan_ids.length) {
        setLiveLog({
          open: true,
          scanId: null,
          agentType: 'all',
          scanEntries: scan_ids.map((id, i) => ({ scanId: id, agentType: types[i] })),
        })
      }
      scan_ids.forEach((scanId, i) => startPolling(scanId, types[i]))
    } catch (err) {
      setScanning({ cost: false, monitoring: false, deploy: false })
      const errStatus = { status: 'error', error: err.message }
      setLastStatus({ cost: errStatus, monitoring: errStatus, deploy: errStatus })
    }
  }, [resourceGroup, startPolling])

  const anyScanning = Object.values(scanning).some(Boolean)
  // BUG 1 FIX: only say "all agents" when every agent is scanning (Run All was clicked)
  const allScanning = Object.values(scanning).every(Boolean)

  return (
    <>
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
            Resource Group <span className="text-slate-600">(optional)</span>
          </label>
          <input
            id="rg-input"
            type="text"
            value={resourceGroup}
            onChange={e => setResourceGroup(e.target.value)}
            placeholder="e.g. ruriskry-prod-rg (leave empty to scan whole subscription)"
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
              onViewResults={() => {
                // Pick the first action_id from this scan's evaluations.
                // The parent looks it up in its evaluations[] and opens drilldown.
                const firstId = lastStatus[type]?.evaluations?.[0]?.action_id
                if (firstId) onViewVerdicts?.(firstId)
              }}
            />
          ))}
        </div>

        {/* ── View Log button — reopen a closed-but-still-active scan log ── */}
        {!liveLog.open && (liveLog.scanId || liveLog.scanEntries?.length) && (
          <button
            onClick={() => setLiveLog(prev => ({ ...prev, open: true }))}
            className="w-full mb-3 py-2 rounded-xl text-sm font-semibold border border-slate-500/60 bg-slate-700/30 hover:bg-slate-700/50 text-slate-300 hover:text-slate-100 cursor-pointer transition-all flex items-center justify-center gap-2"
          >
            <ClipboardList className="w-3.5 h-3.5" /> View Scan Log
          </button>
        )}

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
              <Spinner />
              {/* BUG 1 FIX: only "Running all agents" when all 3 are scanning */}
              {allScanning ? 'Running all agents…' : 'Scan in progress…'}
            </span>
          ) : (
            <span className="flex items-center justify-center gap-2">
              <Zap className="w-3.5 h-3.5" /> Run All Agents
            </span>
          )}
        </button>
      </section>

      {/* ── Live Log Panel (renders outside the card to cover the whole screen) ── */}
      <LiveLogPanel
        scanId={liveLog.scanId}
        agentType={liveLog.agentType}
        scanEntries={liveLog.scanEntries}
        isOpen={liveLog.open}
        onClose={() => setLiveLog(prev => ({ ...prev, open: false }))}
      />
    </>
  )
}
