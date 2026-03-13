/**
 * Scans.jsx — scan trigger controls + scan history table.
 *
 * Moved AgentControls here from the main overview.
 * Shows last-run data per agent in a structured table.
 */

import React, { useEffect, useState } from 'react'
import { useOutletContext, useNavigate } from 'react-router-dom'
import AgentControls from '../components/AgentControls'
import { fetchAgentLastRun } from '../api'
import { CheckCircle, RefreshCw, ChevronRight, AlertTriangle } from 'lucide-react'
import TableSkeleton from '../components/magicui/TableSkeleton'

// ── Helpers ────────────────────────────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '—'
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

const AGENT_NAMES  = ['cost-optimization-agent', 'monitoring-agent', 'deploy-agent']
const AGENT_LABELS = {
  'cost-optimization-agent': 'Cost',
  'monitoring-agent':        'Monitoring',
  'deploy-agent':            'Deploy',
}
// agent_type values returned by the API: "deploy", "monitoring", "cost", "cost-optimization"
const AGENT_TYPE_LABELS = {
  'deploy':            'Deploy',
  'monitoring':        'Monitoring',
  'cost':              'Cost',
  'cost-optimization': 'Cost',
}

// Map scan agent_type → the agent_id value stored in decision records
const AGENT_TYPE_TO_ID = {
  'monitoring':        'monitoring-agent',
  'cost':              'cost-optimization-agent',
  'cost-optimization': 'cost-optimization-agent',
  'deploy':            'deploy-agent',
}

// ── Component ─────────────────────────────────────────────────────────────

export default function Scans() {
  const { fetchAll } = useOutletContext()
  const navigate     = useNavigate()

  const [recentScans, setRecentScans] = useState([])
  const [scansLoading, setScansLoading] = useState(true)

  function loadScans() {
    setScansLoading(true)
    Promise.all(AGENT_NAMES.map(name => fetchAgentLastRun(name).catch(() => null)))
      .then(results => {
        setRecentScans(
          results
            .filter(r => r && r.status !== 'no_data')
            .sort((a, b) => (b.started_at ?? '').localeCompare(a.started_at ?? ''))
        )
        setScansLoading(false)
      })
  }

  useEffect(() => { loadScans() }, [])

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">

      <div>
        <h1 className="text-xl font-bold text-white">Scans</h1>
        <p className="text-sm text-slate-500 mt-1">
          Trigger and monitor operational agent scans
        </p>
      </div>

      {/* ── Scan controls ── */}
      <AgentControls
        onScanComplete={() => { fetchAll(); loadScans() }}
        onViewVerdicts={() => navigate('/decisions')}
      />

      {/* ── Scan history ── */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-slate-300">Scan history</h2>
          <button
            onClick={loadScans}
            className="text-slate-500 hover:text-slate-300 transition-colors"
            title="Refresh"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>

        {scansLoading ? (
          <table className="w-full">
            <TableSkeleton rows={3} cols={9} />
          </table>
        ) : recentScans.length === 0 ? (
          <div className="text-center py-8 text-slate-500 text-sm">
            No scan history. Run a scan above to populate this table.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Scan ID</th>
                  <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Agent</th>
                  <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Started</th>
                  <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Completed</th>
                  <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Duration</th>
                  <th className="text-right pb-2.5 pr-4 text-xs font-semibold text-slate-500">Proposals</th>
                  <th className="text-right pb-2.5 pr-4 text-xs font-semibold text-slate-500">Verdicts</th>
                  <th className="text-left pb-2.5 pr-4 text-xs font-semibold text-slate-500">Status</th>
                  <th className="pb-2.5 w-8" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {recentScans.map((scan, i) => {
                  const verdictCount = scan.evaluations_count ?? 0
                  const isClean = scan.status === 'complete' && verdictCount === 0
                  return (
                    <tr key={i} className="hover:bg-slate-800/40 transition-colors group">
                      <td className="py-3 pr-4 font-mono text-xs text-slate-500">
                        {scan.scan_id ? `${scan.scan_id.slice(0, 8)}…` : '—'}
                      </td>
                      <td className="py-3 pr-4">
                        <span className="text-blue-400 font-mono text-xs">
                          {AGENT_TYPE_LABELS[scan.agent_type] ?? AGENT_LABELS[scan.source] ?? scan.agent_type ?? scan.source}
                        </span>
                      </td>
                      <td className="py-3 pr-4 text-xs text-slate-400">{formatTime(scan.started_at)}</td>
                      <td className="py-3 pr-4 text-xs text-slate-400">{formatTime(scan.completed_at)}</td>
                      <td className="py-3 pr-4 text-xs text-slate-400 tabular-nums">
                        {scanDuration(scan.started_at, scan.completed_at) ?? '—'}
                      </td>
                      <td className="py-3 pr-4 text-right text-xs text-slate-400 tabular-nums">
                        {scan.proposals_count ?? 0}
                      </td>
                      <td className="py-3 pr-4 text-right">
                        {verdictCount > 0 ? (
                          <button
                            onClick={() => {
                              const agentId = AGENT_TYPE_TO_ID[scan.agent_type]
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
                      <td className="py-3 pr-4">
                        {scan.status === 'error' ? (
                          <span className="text-xs text-red-400 flex items-center gap-1" title={scan.scan_error ?? 'Agent framework error'}>
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
                          <span className="text-xs text-slate-500">{scan.status ?? '—'}</span>
                        )}
                      </td>
                      <td className="py-3">
                        {verdictCount > 0 && (
                          <button
                            onClick={() => {
                              const agentId = AGENT_TYPE_TO_ID[scan.agent_type]
                              navigate(agentId ? `/decisions?agent=${agentId}` : '/decisions')
                            }}
                            className="opacity-0 group-hover:opacity-100 transition-opacity text-slate-500 hover:text-slate-300"
                          >
                            <ChevronRight className="w-4 h-4" />
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
    </div>
  )
}
