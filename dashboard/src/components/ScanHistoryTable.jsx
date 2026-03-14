/**
 * ScanHistoryTable.jsx — Cosmos-backed scan history with "View Log" action.
 *
 * Uses fetchScanHistory() for real data. Filters out "tracker" fallback records
 * (no scan_id, no real data). Each row has a "View Log" button that opens
 * ScanLogViewer in historical mode.
 */

import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchScanHistory } from '../api'
import {
  RefreshCw, CheckCircle, AlertTriangle, ChevronRight,
  ScrollText, Filter,
} from 'lucide-react'
import TableSkeleton from './magicui/TableSkeleton'

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
  { value: '',         label: 'All Statuses' },
  { value: 'complete', label: 'Complete' },
  { value: 'running',  label: 'Running' },
  { value: 'error',    label: 'Error' },
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

function normalizeAgentType(scan) {
  // API returns agent_type or source, normalize for filtering
  return scan.agent_type || scan.source || ''
}

// ── Component ───────────────────────────────────────────────────────────────

export default function ScanHistoryTable({ onViewLog }) {
  const navigate = useNavigate()

  const [scans, setScans]           = useState([])
  const [loading, setLoading]       = useState(true)
  const [agentFilter, setAgentFilter]   = useState('')
  const [statusFilter, setStatusFilter] = useState('')

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
                      {scan.status === 'error' ? (
                        <span className="text-xs text-red-400 flex items-center gap-1" title={scan.scan_error ?? 'Error'}>
                          <AlertTriangle className="w-3 h-3" /> Error
                        </span>
                      ) : isClean ? (
                        <span className="text-xs text-green-400 flex items-center gap-1">
                          <CheckCircle className="w-3 h-3" /> Clean
                        </span>
                      ) : scan.status === 'complete' ? (
                        <span className="text-xs text-blue-400 flex items-center gap-1">
                          <CheckCircle className="w-3 h-3" /> Complete
                        </span>
                      ) : scan.status === 'running' ? (
                        <span className="text-xs text-yellow-400 flex items-center gap-1.5">
                          <span className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse" />
                          Running
                        </span>
                      ) : (
                        <span className="text-xs text-slate-500">{scan.status ?? '\u2014'}</span>
                      )}
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
                      {scanDuration(scan.started_at, scan.completed_at) ?? '\u2014'}
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

                    {/* View Log action */}
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
