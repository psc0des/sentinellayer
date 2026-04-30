/**
 * ScanHistoryTable.jsx — Cosmos-backed scan history with "View Log" action.
 *
 * Uses fetchScanHistory() for real data. Filters out "tracker" fallback records
 * (no scan_id, no real data). Each row has a "View Log" button that opens
 * ScanLogViewer in historical mode.
 */

import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchScanHistory, cancelScan } from '../api'
import {
  RefreshCw, CheckCircle, AlertTriangle, ChevronRight,
  ScrollText, Filter, XCircle, Square,
} from 'lucide-react'
import TableSkeleton from './magicui/TableSkeleton'

// ── localStorage helpers (mirrors useScanManager) ───────────────────────────

const LS_STOPPING_KEY = 'ruriskry-stopping-scans'

function getStoppingIds() {
  try {
    return new Set(JSON.parse(localStorage.getItem(LS_STOPPING_KEY) ?? '[]'))
  } catch { return new Set() }
}

function addStoppingId(scanId) {
  if (!scanId) return
  try {
    const s = getStoppingIds()
    s.add(scanId)
    localStorage.setItem(LS_STOPPING_KEY, JSON.stringify([...s]))
  } catch { /* ignore */ }
}

// ── Constants ───────────────────────────────────────────────────────────────

const AGENT_TYPE_LABELS = {
  deploy:            'Deploy',
  monitoring:        'Monitoring',
  cost:              'Cost',
  'cost-optimization': 'Cost',
}

const AGENT_TYPE_TO_ID = {
  monitoring:        'monitoring-agent',
  cost:              'cost-optimization-agent',
  'cost-optimization': 'cost-optimization-agent',
  deploy:            'deploy-agent',
}

const AGENT_FILTER_OPTIONS = [
  { value: '',           label: 'All Agents' },
  { value: 'deploy',     label: 'Deploy' },
  { value: 'monitoring', label: 'Monitoring' },
  { value: 'cost',       label: 'Cost' },
]

const STATUS_FILTER_OPTIONS = [
  { value: '',          label: 'All Statuses' },
  { value: 'complete',  label: 'Complete' },
  { value: 'running',   label: 'Running' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'error',     label: 'Error' },
]

// ── Helpers ─────────────────────────────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '\u2014'
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function scanDuration(started, completed) {
  if (!started || !completed) return null
  const s = Math.round((new Date(completed) - new Date(started)) / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

function elapsedSince(started) {
  if (!started) return null
  const s = Math.round((Date.now() - new Date(started).getTime()) / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

function normalizeAgentType(scan) {
  // API returns agent_type or source, normalize for filtering
  return scan.agent_type || scan.source || ''
}

// ── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Map a scan row's agent_type back to the useScanManager state key.
 * Needed so we can cross-reference local "stopping" state with the table row.
 */
function toScanStateKey(agentType) {
  if (!agentType) return null
  if (agentType === 'cost' || agentType === 'cost-optimization') return 'cost'
  if (agentType === 'deploy') return 'deploy'
  if (agentType === 'monitoring') return 'monitoring'
  return null
}

// ── Component ───────────────────────────────────────────────────────────────

export default function ScanHistoryTable({ onViewLog, scanState = {}, refreshKey = 0 }) {
  const navigate = useNavigate()

  const [scans, setScans]           = useState([])
  const [loading, setLoading]       = useState(true)
  const [agentFilter, setAgentFilter]   = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  // Track scan IDs cancelled directly from this table (for immediate "Cancelling" display)
  const [tableCancelling, setTableCancelling] = useState(new Set())
  // Tick every second to refresh elapsed time for running scans
  const [, setTick] = useState(0)
  const tickRef = useRef(null)

  useEffect(() => {
    const hasRunning = scans.some(s => s.status === 'running')
    if (hasRunning && !tickRef.current) {
      tickRef.current = setInterval(() => setTick(t => t + 1), 1000)
    } else if (!hasRunning && tickRef.current) {
      clearInterval(tickRef.current)
      tickRef.current = null
    }
    return () => {}
  }, [scans])

  useEffect(() => () => { if (tickRef.current) clearInterval(tickRef.current) }, [])

  const loadScans = useCallback(() => {
    setLoading(true)
    fetchScanHistory(200)
      .then(data => {
        const records = (data.scans ?? data ?? [])
          // Filter out tracker fallback records (no real scan data)
          .filter(s => s.source !== 'tracker' && s.scan_id)
        setScans(records)
      })
      .catch(() => setScans([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { loadScans() }, [loadScans])

  // Re-fetch whenever the parent bumps refreshKey (scan completes or cancel requested)
  useEffect(() => { if (refreshKey > 0) loadScans() }, [refreshKey, loadScans])

  // Apply filters
  const filtered = scans.filter(scan => {
    if (agentFilter) {
      const type = normalizeAgentType(scan)
      // "cost" filter should match both "cost" and "cost-optimization"
      if (agentFilter === 'cost') {
        if (type !== 'cost' && type !== 'cost-optimization') return false
      } else if (type !== agentFilter) {
        return false
      }
    }
    if (statusFilter && scan.status !== statusFilter) return false
    return true
  })

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 p-5">
      {/* Header + filters */}
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <h2 className="text-sm font-semibold text-slate-300">Scan History</h2>

        <div className="flex items-center gap-2">
          {/* Agent filter */}
          <div className="relative">
            <select
              value={agentFilter}
              onChange={e => setAgentFilter(e.target.value)}
              className="appearance-none bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 pr-7 text-xs text-slate-300 focus:outline-none focus:border-blue-500 cursor-pointer"
            >
              {AGENT_FILTER_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
            <Filter className="w-3 h-3 text-slate-500 absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none" />
          </div>

          {/* Status filter */}
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            className="appearance-none bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-blue-500 cursor-pointer"
          >
            {STATUS_FILTER_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>

          {/* Refresh */}
          <button
            onClick={loadScans}
            className="text-slate-500 hover:text-slate-300 transition-colors p-1"
            title="Refresh scan history"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <table className="w-full">
          <TableSkeleton rows={5} cols={7} />
        </table>
      ) : filtered.length === 0 ? (
        <div className="text-center py-8 text-slate-500 text-sm">
          {scans.length === 0
            ? 'No scan history yet. Trigger a scan above to populate this table.'
            : 'No scans match the current filters.'}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Agent</th>
                <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Status</th>
                <th className="text-right pb-2.5 pr-4 text-xs font-semibold text-slate-500">Proposals</th>
                <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Started</th>
                <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Duration</th>
                <th className="text-right pb-2.5 pr-4 text-xs font-semibold text-slate-500">Verdicts</th>
                <th className="pb-2.5 w-20 text-xs font-semibold text-slate-500 text-center">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {filtered.map((scan, i) => {
                const verdictCount = scan.evaluations_count ?? 0
                const isClean = scan.status === 'complete' && verdictCount === 0
                const agentType = normalizeAgentType(scan)

                return (
                  <tr key={scan.scan_id || i} className="hover:bg-slate-800/40 transition-colors group">
                    {/* Agent */}
                    <td className="py-3 pr-4">
                      <span className="text-blue-400 font-mono text-xs">
                        {AGENT_TYPE_LABELS[agentType] ?? agentType}
                      </span>
                    </td>

                    {/* Status */}
                    <td className="py-3 pr-4">
                      {(() => {
                        // Three sources for "this scan is being cancelled":
                        //   1. useScanManager local state is 'stopping' (live, same session)
                        //   2. localStorage stopping set (survives page refresh)
                        //   3. tableCancelling — user clicked Stop directly from this table
                        const stateKey = toScanStateKey(agentType)
                        const localEntry = stateKey ? scanState[stateKey] : null
                        const stoppingIds = getStoppingIds()
                        const isCancelling =
                          scan.status === 'running' && (
                            localEntry?.status === 'stopping' ||
                            stoppingIds.has(scan.scan_id) ||
                            tableCancelling.has(scan.scan_id)
                          )

                        // Detect phantom/stalled scans: backend reported 'running' but
                        // the local state has no active scan for this agent (backend
                        // restart orphan). Flag after 20 minutes.
                        const isStale =
                          scan.status === 'running' &&
                          !isCancelling &&
                          !localEntry?.scanId &&
                          scan.started_at &&
                          Date.now() - new Date(scan.started_at).getTime() > 20 * 60 * 1000

                        if (isCancelling) {
                          return (
                            <span className="text-xs text-orange-400 flex items-center gap-1.5">
                              <span className="w-2 h-2 rounded-full bg-orange-400 animate-pulse" />
                              Cancelling…
                            </span>
                          )
                        }
                        if (isStale) {
                          return (
                            <span className="text-xs text-slate-500 flex items-center gap-1" title="Scan may have been interrupted (backend restarted)">
                              <AlertTriangle className="w-3 h-3 text-amber-600" /> Stalled
                            </span>
                          )
                        }
                        if (scan.status === 'error') {
                          return (
                            <span className="text-xs text-red-400 flex items-center gap-1" title={scan.scan_error ?? 'Error'}>
                              <AlertTriangle className="w-3 h-3" /> Error
                            </span>
                          )
                        }
                        if (isClean) {
                          return (
                            <span className="text-xs text-green-400 flex items-center gap-1">
                              <CheckCircle className="w-3 h-3" /> Clean
                            </span>
                          )
                        }
                        if (scan.status === 'complete') {
                          return (
                            <span className="text-xs text-blue-400 flex items-center gap-1">
                              <CheckCircle className="w-3 h-3" /> Complete
                            </span>
                          )
                        }
                        if (scan.status === 'running') {
                          return (
                            <span className="text-xs text-yellow-400 flex items-center gap-1.5">
                              <span className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse" />
                              Running
                            </span>
                          )
                        }
                        if (scan.status === 'cancelled') {
                          return (
                            <span className="text-xs text-slate-400 flex items-center gap-1">
                              <XCircle className="w-3 h-3" /> Cancelled
                            </span>
                          )
                        }
                        return <span className="text-xs text-slate-500">{scan.status ?? '\u2014'}</span>
                      })()}
                    </td>

                    {/* Proposals */}
                    <td className="py-3 pr-4 text-right text-xs text-slate-400 tabular-nums">
                      {scan.proposals_count ?? 0}
                    </td>

                    {/* Started */}
                    <td className="py-3 pr-4 text-xs text-slate-400">
                      {formatTime(scan.started_at)}
                    </td>

                    {/* Duration */}
                    <td className="py-3 pr-4 text-xs text-slate-400 tabular-nums">
                      {scan.status === 'running'
                        ? <span className="text-yellow-400">{elapsedSince(scan.started_at) ?? '\u2026'}</span>
                        : (scanDuration(scan.started_at, scan.completed_at) ?? '\u2014')}
                    </td>

                    {/* Verdicts */}
                    <td className="py-3 pr-4 text-right">
                      {verdictCount > 0 ? (
                        <button
                          onClick={() => {
                            const agentId = AGENT_TYPE_TO_ID[agentType]
                            navigate(agentId ? `/decisions?agent=${agentId}` : '/decisions')
                          }}
                          className="text-xs font-medium text-yellow-400 tabular-nums hover:text-yellow-300 underline decoration-dotted"
                        >
                          {verdictCount} verdict{verdictCount !== 1 ? 's' : ''}
                        </button>
                      ) : (
                        <span className="text-xs text-slate-600">0</span>
                      )}
                    </td>

                    {/* Actions: View Log (completed) or Stop (running) */}
                    <td className="py-3 text-center">
                      {scan.scan_id && scan.status !== 'running' && (
                        <button
                          onClick={() => onViewLog(scan.scan_id, agentType)}
                          className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-300 transition-colors"
                          title="View scan log"
                        >
                          <ScrollText className="w-3 h-3" />
                          <span>View Log</span>
                        </button>
                      )}
                      {scan.scan_id && scan.status === 'running' &&
                       !tableCancelling.has(scan.scan_id) && (
                        <button
                          onClick={async () => {
                            // Immediately show "Cancelling" in this row
                            setTableCancelling(prev => new Set([...prev, scan.scan_id]))
                            addStoppingId(scan.scan_id)
                            try { await cancelScan(scan.scan_id) } catch { /* already done */ }
                            // Refresh the table after backend processes cancel
                            setTimeout(loadScans, 3000)
                          }}
                          className="inline-flex items-center gap-1 text-xs text-red-500 hover:text-red-300 transition-colors"
                          title="Stop this scan"
                        >
                          <Square className="w-3 h-3" />
                          <span>Stop</span>
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
