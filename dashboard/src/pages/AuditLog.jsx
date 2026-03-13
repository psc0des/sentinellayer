/**
 * AuditLog.jsx — operational audit log of every scan run.
 *
 * Shows one row per scan execution (not per governance decision).
 * Each row tells you: which agent ran, when, how long it took,
 * how many resources it examined, and the outcome breakdown.
 *
 * Click a row → drilldown panel shows every resource the agent
 * examined in that scan, with its governance verdict per proposal.
 *
 * The Decisions tab already shows governance verdicts in detail —
 * this tab is for auditing the scans themselves.
 */

import React, { useState, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useOutletContext } from 'react-router-dom'
import {
  Download, Search, X, ChevronRight, Clock, CheckCircle,
  AlertTriangle, XCircle, Activity, Copy, Check,
} from 'lucide-react'
import VerdictBadge from '../components/magicui/VerdictBadge'

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function formatDuration(startIso, endIso) {
  if (!startIso || !endIso) return null
  const ms = new Date(endIso) - new Date(startIso)
  if (isNaN(ms) || ms < 0) return null
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rem = s % 60
  return rem > 0 ? `${m}m ${rem}s` : `${m}m`
}

function shortResource(id) {
  return id?.split('/').filter(Boolean).pop() ?? id ?? '—'
}

function agentLabel(agentType) {
  const map = { cost: 'Cost', monitoring: 'Monitoring', deploy: 'Deploy' }
  return map[agentType] ?? agentType ?? '—'
}

function agentColor(agentType) {
  const map = { cost: 'text-amber-400', monitoring: 'text-blue-400', deploy: 'text-emerald-400' }
  return map[agentType] ?? 'text-slate-400'
}

function statusIcon(status) {
  if (status === 'complete')   return <CheckCircle  className="w-3.5 h-3.5 text-emerald-400" />
  if (status === 'error')      return <XCircle      className="w-3.5 h-3.5 text-rose-400" />
  if (status === 'cancelled')  return <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
  return <Activity className="w-3.5 h-3.5 text-blue-400 animate-pulse" />
}

function statusLabel(status) {
  const map = { complete: 'Complete', error: 'Error', cancelled: 'Cancelled', running: 'Running' }
  return map[status] ?? status ?? '—'
}

function statusColor(status) {
  if (status === 'complete')  return 'text-emerald-400'
  if (status === 'error')     return 'text-rose-400'
  if (status === 'cancelled') return 'text-amber-400'
  return 'text-blue-400'
}

// ── Export helpers ────────────────────────────────────────────────────────────

function exportCSV(rows) {
  const cols = ['scan_id', 'agent_type', 'status', 'started_at', 'completed_at', 'proposals_count', 'evaluations_count']
  const header = cols.join(',')
  const lines  = rows.map(r => cols.map(c => JSON.stringify(r[c] ?? '')).join(','))
  const blob   = new Blob([header + '\n' + lines.join('\n')], { type: 'text/csv' })
  downloadBlob(blob, 'ruriskry-scans.csv')
}

function exportJSON(rows) {
  const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' })
  downloadBlob(blob, 'ruriskry-scans.json')
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a   = document.createElement('a')
  a.href    = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// ── CopyButton ────────────────────────────────────────────────────────────────

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={() => navigator.clipboard.writeText(text).then(() => {
        setCopied(true); setTimeout(() => setCopied(false), 1500)
      })}
      className="ml-1 text-slate-500 hover:text-slate-300 transition-colors flex-shrink-0"
    >
      {copied
        ? <Check className="w-3.5 h-3.5 text-emerald-400" />
        : <Copy  className="w-3.5 h-3.5" />}
    </button>
  )
}

// ── SRI colour helper ─────────────────────────────────────────────────────────

function sriColor(score) {
  if (score <= 25) return 'text-emerald-400'
  if (score <= 60) return 'text-amber-400'
  return 'text-rose-400'
}

// ── Scan drilldown panel ──────────────────────────────────────────────────────

function ScanPanel({ scan, onClose }) {
  if (!scan) return null

  const scannedResources = scan.scanned_resources ?? []
  const proposals        = scan.proposed_actions  ?? []
  const evaluations      = scan.evaluations       ?? []
  const totals           = scan.totals ?? {}
  const duration         = formatDuration(scan.started_at, scan.completed_at)

  // Build lookup: lowercase resource name/id → {proposal, evaluation}
  // Used to match scanned_resources entries against flagged proposals
  const flaggedByName = useMemo(() => {
    const map = {}
    proposals.forEach((p, idx) => {
      const rid    = p.target?.resource_id ?? p.resource_id ?? ''
      const name   = rid.split('/').filter(Boolean).pop() ?? rid
      const ev     = evaluations[idx] ?? null
      const entry  = { proposal: p, ev }
      map[rid.toLowerCase()]  = entry
      map[name.toLowerCase()] = entry
    })
    return map
  }, [proposals, evaluations])

  // The full list to display = scanned_resources (all) or fall back to proposals only
  const displayResources = useMemo(() => {
    if (scannedResources.length > 0) {
      return scannedResources.map(r => {
        const name = r.name ?? r.id?.split('/').filter(Boolean).pop() ?? ''
        const flagged = flaggedByName[r.id?.toLowerCase()] ?? flaggedByName[name.toLowerCase()] ?? null
        return { ...r, flagged }
      })
    }
    // Fallback: no snapshot — show proposals only, each marked as flagged
    return proposals.map((p, idx) => {
      const rid  = p.target?.resource_id ?? p.resource_id ?? ''
      const name = rid.split('/').filter(Boolean).pop() ?? rid
      return {
        id: rid, name, type: p.target?.resource_type ?? '', location: '',
        flagged: { proposal: p, ev: evaluations[idx] ?? null },
      }
    })
  }, [scannedResources, proposals, evaluations, flaggedByName])

  const flaggedCount = displayResources.filter(r => r.flagged).length
  const cleanCount   = displayResources.length - flaggedCount

  return createPortal(
    <>
      {/* Backdrop */}
      <div
        style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 9998 }}
        onClick={onClose}
      />

      {/* Slide-in panel */}
      <div
        style={{ position: 'fixed', right: 0, top: 0, bottom: 0, width: '100%', maxWidth: '580px', zIndex: 9999 }}
        className="bg-slate-950 border-l border-slate-800 overflow-y-auto shadow-2xl flex flex-col"
      >
        {/* Header */}
        <div className="sticky top-0 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-start justify-between gap-3 z-10">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-0.5">
              {statusIcon(scan.status)}
              <p className={`text-xs font-semibold uppercase tracking-wide ${statusColor(scan.status)}`}>
                {agentLabel(scan.agent_type)} Agent · {statusLabel(scan.status)}
              </p>
            </div>
            <p className="text-xs text-slate-500">{formatTime(scan.started_at)}</p>
            {duration && (
              <p className="text-xs text-slate-600 mt-0.5">
                <Clock className="w-3 h-3 inline mr-1 -mt-px" />
                Duration: {duration}
              </p>
            )}
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200 transition-colors flex-shrink-0 mt-0.5">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 px-5 py-4 space-y-5">

          {/* Outcome summary — now includes total scanned */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Outcome Summary</h3>
            <div className="grid grid-cols-4 gap-2">
              <div className="bg-slate-800/60 border border-slate-700 rounded-lg p-2.5 text-center">
                <p className="text-lg font-bold text-slate-200">{displayResources.length}</p>
                <p className="text-xs text-slate-500 mt-0.5">Scanned</p>
              </div>
              <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-2.5 text-center">
                <p className="text-lg font-bold text-emerald-400">{totals.approved ?? 0}</p>
                <p className="text-xs text-slate-500 mt-0.5">Approved</p>
              </div>
              <div className="bg-amber-500/10 border border-amber-500/20 rounded-lg p-2.5 text-center">
                <p className="text-lg font-bold text-amber-400">{totals.escalated ?? 0}</p>
                <p className="text-xs text-slate-500 mt-0.5">Escalated</p>
              </div>
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-lg p-2.5 text-center">
                <p className="text-lg font-bold text-rose-400">{totals.denied ?? 0}</p>
                <p className="text-xs text-slate-500 mt-0.5">Denied</p>
              </div>
            </div>
          </section>

          {/* Error message */}
          {scan.scan_error && (
            <section>
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Scan Error</h3>
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-lg p-3">
                <p className="text-xs text-rose-300 leading-relaxed">{scan.scan_error}</p>
              </div>
            </section>
          )}

          {/* All resources examined */}
          <section>
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Resources Examined ({displayResources.length})
              </h3>
              {displayResources.length > 0 && (
                <div className="flex items-center gap-2 text-xs">
                  {flaggedCount > 0 && <span className="text-amber-400">{flaggedCount} flagged</span>}
                  {cleanCount > 0   && <span className="text-emerald-500">{cleanCount} clean</span>}
                </div>
              )}
            </div>

            {displayResources.length === 0 ? (
              <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 text-center">
                <p className="text-xs text-slate-500">No resources recorded for this scan.</p>
              </div>
            ) : (
              <div className="space-y-1.5">
                {displayResources.map((resource, idx) => {
                  const isFlagged  = !!resource.flagged
                  const proposal   = resource.flagged?.proposal ?? null
                  const ev         = resource.flagged?.ev ?? null
                  const actionType = proposal?.action_type ?? proposal?.type ?? null
                  const reason     = proposal?.action_reason ?? proposal?.reason ?? null
                  const decision   = ev?.decision ?? null
                  const sri        = ev?.sri_composite ?? null
                  const violations = ev?.violations ?? []
                  const displayName = resource.name || shortResource(resource.id)
                  const resourceType = resource.type ?? proposal?.target?.resource_type ?? ''

                  return (
                    <div
                      key={idx}
                      className={`rounded-lg p-3 border ${
                        isFlagged
                          ? 'bg-slate-900 border-slate-700'
                          : 'bg-slate-900/40 border-slate-800/60'
                      }`}
                    >
                      {/* Resource row */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          {isFlagged ? (
                            <AlertTriangle className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />
                          ) : (
                            <CheckCircle className="w-3.5 h-3.5 text-emerald-500/60 flex-shrink-0" />
                          )}
                          <div className="min-w-0">
                            <p className={`text-xs font-mono truncate ${isFlagged ? 'text-slate-200' : 'text-slate-500'}`}
                               title={resource.id}>
                              {displayName}
                            </p>
                            {resourceType && (
                              <p className="text-xs text-slate-600 truncate">{resourceType.split('/').pop()}</p>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center gap-2 flex-shrink-0">
                          {sri != null && (
                            <span className={`text-xs font-bold tabular-nums ${sriColor(sri)}`}>
                              {sri.toFixed(1)}
                            </span>
                          )}
                          {decision
                            ? <VerdictBadge verdict={decision} />
                            : !isFlagged && <span className="text-xs text-emerald-600 font-medium">Clean</span>
                          }
                        </div>
                      </div>

                      {/* Flagged details: action type + finding + violations */}
                      {isFlagged && (
                        <div className="mt-2 pt-2 border-t border-slate-800 space-y-1.5">
                          {actionType && (
                            <p className="text-xs text-slate-500">{actionType.replace(/_/g, ' ')}</p>
                          )}
                          {reason && (
                            <p className="text-xs text-slate-400 leading-relaxed">{reason}</p>
                          )}
                          {violations.length > 0 && (
                            <div className="flex flex-wrap gap-1 pt-0.5">
                              {violations.map(v => (
                                <span key={v} className="text-xs font-mono px-1.5 py-0.5 rounded bg-rose-500/10 border border-rose-500/30 text-rose-400">
                                  {v}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          {/* Scan metadata */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Scan Reference</h3>
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-3 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500 flex-shrink-0">Scan ID</span>
                <div className="flex items-center min-w-0">
                  <span className="text-xs font-mono text-slate-400 truncate">{scan.scan_id}</span>
                  <CopyButton text={scan.scan_id} />
                </div>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500">Agent</span>
                <span className={`text-xs font-mono ${agentColor(scan.agent_type)}`}>
                  {scan.agent_type}-agent
                </span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500">Started</span>
                <span className="text-xs text-slate-400 tabular-nums">{formatTime(scan.started_at)}</span>
              </div>
              {scan.completed_at && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-slate-500">Completed</span>
                  <span className="text-xs text-slate-400 tabular-nums">{formatTime(scan.completed_at)}</span>
                </div>
              )}
              {duration && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-slate-500">Duration</span>
                  <span className="text-xs text-slate-400">{duration}</span>
                </div>
              )}
            </div>
          </section>

        </div>
      </div>
    </>,
    document.body
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function AuditLog() {
  const { scans = [] } = useOutletContext()

  const [searchText,  setSearchText]  = useState('')
  const [filterAgent, setFilterAgent] = useState('all')
  const [filterStatus,setFilterStatus]= useState('all')
  const [dateFrom,    setDateFrom]    = useState('')
  const [dateTo,      setDateTo]      = useState('')
  const [selectedScan,setSelectedScan]= useState(null)

  const agentOptions = useMemo(() => {
    const names = new Set(scans.map(s => s.agent_type).filter(Boolean))
    return ['all', ...Array.from(names).sort()]
  }, [scans])

  const filtered = useMemo(() => {
    return scans.filter(scan => {
      if (filterAgent  !== 'all' && scan.agent_type !== filterAgent)  return false
      if (filterStatus !== 'all' && scan.status     !== filterStatus) return false
      if (searchText) {
        const q = searchText.toLowerCase()
        const inAgent   = scan.agent_type?.includes(q)
        const inScanId  = scan.scan_id?.toLowerCase().includes(q)
        const inResources = (scan.proposed_actions ?? [])
          .some(p => (p.target?.resource_id ?? p.resource_id ?? '').toLowerCase().includes(q))
        if (!inAgent && !inScanId && !inResources) return false
      }
      if (dateFrom && scan.started_at < dateFrom) return false
      if (dateTo) {
        const end = dateTo + 'T23:59:59Z'
        if (scan.started_at > end) return false
      }
      return true
    })
  }, [scans, filterAgent, filterStatus, searchText, dateFrom, dateTo])

  const hasActiveFilters = filterAgent !== 'all' || filterStatus !== 'all' || searchText || dateFrom || dateTo

  function clearFilters() {
    setSearchText('')
    setFilterAgent('all')
    setFilterStatus('all')
    setDateFrom('')
    setDateTo('')
  }

  return (
    <div className="p-6 space-y-5 max-w-6xl mx-auto">

      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold text-white">Audit Log</h1>
          <p className="text-sm text-slate-500 mt-1">
            {filtered.length} of {scans.length} scan runs · click any row to inspect resources examined
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => exportCSV(filtered)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 hover:border-slate-600 transition-colors"
          >
            <Download className="w-3.5 h-3.5" /> CSV
          </button>
          <button
            onClick={() => exportJSON(filtered)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 hover:border-slate-600 transition-colors"
          >
            <Download className="w-3.5 h-3.5" /> JSON
          </button>
        </div>
      </div>

      {/* ── Filter bar ── */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-4 flex flex-wrap gap-3 items-end">

        <div className="flex-1 min-w-40">
          <label className="block text-xs text-slate-500 mb-1">Search</label>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
            <input
              type="text"
              value={searchText}
              onChange={e => setSearchText(e.target.value)}
              placeholder="Agent, scan ID, or resource…"
              className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
            />
          </div>
        </div>

        <div className="min-w-28">
          <label className="block text-xs text-slate-500 mb-1">Agent</label>
          <select
            value={filterAgent}
            onChange={e => setFilterAgent(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          >
            {agentOptions.map(a => (
              <option key={a} value={a}>{a === 'all' ? 'All agents' : agentLabel(a)}</option>
            ))}
          </select>
        </div>

        <div className="min-w-28">
          <label className="block text-xs text-slate-500 mb-1">Status</label>
          <select
            value={filterStatus}
            onChange={e => setFilterStatus(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          >
            <option value="all">All statuses</option>
            <option value="complete">Complete</option>
            <option value="error">Error</option>
            <option value="running">Running</option>
            <option value="cancelled">Cancelled</option>
          </select>
        </div>

        <div className="min-w-32">
          <label className="block text-xs text-slate-500 mb-1">From</label>
          <input
            type="date"
            value={dateFrom}
            onChange={e => setDateFrom(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          />
        </div>

        <div className="min-w-32">
          <label className="block text-xs text-slate-500 mb-1">To</label>
          <input
            type="date"
            value={dateTo}
            onChange={e => setDateTo(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          />
        </div>

        {hasActiveFilters && (
          <button
            onClick={clearFilters}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 transition-colors self-end"
          >
            <X className="w-3 h-3" /> Clear
          </button>
        )}
      </div>

      {/* ── Table ── */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
        {filtered.length === 0 ? (
          <div className="text-center py-16 text-slate-500">
            <Activity className="w-8 h-8 mx-auto mb-2 opacity-40" />
            <p className="text-sm font-medium">
              {scans.length === 0 ? 'No scans recorded yet — trigger a scan to see the audit log.' : 'No scans match your filters'}
            </p>
            {hasActiveFilters && (
              <button onClick={clearFilters} className="text-xs text-blue-400 hover:text-blue-300 mt-2">
                Clear filters
              </button>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-900 z-10">
                <tr className="border-b border-slate-800">
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Started</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Agent</th>
                  <th className="text-center px-4 py-3 text-xs font-semibold text-slate-500">Status</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-slate-500">Resources</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-slate-500 hidden sm:table-cell">Duration</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Outcome</th>
                  <th className="px-4 py-3 w-6" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {filtered.map(scan => {
                  const duration = formatDuration(scan.started_at, scan.completed_at)
                  const totals   = scan.totals ?? {}
                  const proposals = scan.proposals_count ?? (scan.proposed_actions?.length ?? 0)

                  return (
                    <tr
                      key={scan.scan_id}
                      onClick={() => setSelectedScan(scan)}
                      className={`cursor-pointer transition-colors hover:bg-slate-800/60 ${
                        selectedScan?.scan_id === scan.scan_id
                          ? 'bg-slate-800/80 ring-1 ring-inset ring-blue-500/30'
                          : ''
                      }`}
                    >
                      <td className="px-4 py-2.5 text-xs text-slate-500 tabular-nums whitespace-nowrap">
                        {formatTime(scan.started_at)}
                      </td>
                      <td className={`px-4 py-2.5 text-xs font-semibold whitespace-nowrap ${agentColor(scan.agent_type)}`}>
                        {agentLabel(scan.agent_type)}
                      </td>
                      <td className="px-4 py-2.5 text-center">
                        <div className="flex items-center justify-center gap-1.5">
                          {statusIcon(scan.status)}
                          <span className={`text-xs ${statusColor(scan.status)}`}>
                            {statusLabel(scan.status)}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-right text-xs text-slate-400 tabular-nums">
                        {proposals}
                      </td>
                      <td className="px-4 py-2.5 text-right text-xs text-slate-600 tabular-nums hidden sm:table-cell">
                        {duration ?? '—'}
                      </td>
                      <td className="px-4 py-2.5">
                        {(totals.approved ?? 0) + (totals.escalated ?? 0) + (totals.denied ?? 0) > 0 ? (
                          <div className="flex items-center gap-2 text-xs tabular-nums">
                            {(totals.approved  ?? 0) > 0 && <span className="text-emerald-400">{totals.approved}✓</span>}
                            {(totals.escalated ?? 0) > 0 && <span className="text-amber-400">{totals.escalated}⚠</span>}
                            {(totals.denied    ?? 0) > 0 && <span className="text-rose-400">{totals.denied}✗</span>}
                          </div>
                        ) : (
                          <span className="text-xs text-slate-600">—</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-slate-700">
                        <ChevronRight className="w-3.5 h-3.5" />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Scan drilldown panel ── */}
      {selectedScan && (
        <ScanPanel scan={selectedScan} onClose={() => setSelectedScan(null)} />
      )}
    </div>
  )
}
