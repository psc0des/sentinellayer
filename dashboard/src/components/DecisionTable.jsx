/**
 * DecisionTable — enhanced governance decision table.
 *
 * Features:
 *   - Column sorting (resource, SRI, verdict, time)
 *   - Filters: verdict, agent, free-text resource/action search
 *   - Pagination (10 / 25 / 50 per page)
 *   - Export to CSV or JSON
 */

import React, { useState, useMemo } from 'react'
import { ArrowUpDown, ArrowUp, ArrowDown, Search, Download, X } from 'lucide-react'
import VerdictBadge from './magicui/VerdictBadge'

// ── Helpers ────────────────────────────────────────────────────────────────

function sriColor(score) {
  if (score <= 25) return 'text-emerald-400'
  if (score <= 60) return 'text-amber-400'
  return 'text-rose-400'
}

function shortResource(id) {
  return id?.split('/').filter(Boolean).pop() ?? id ?? '—'
}

function formatTime(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

// Export helpers
function exportCSV(rows) {
  const cols = ['action_id', 'timestamp', 'agent_id', 'action_type', 'resource_id', 'sri_composite', 'decision']
  const header = cols.join(',')
  const lines  = rows.map(r => cols.map(c => JSON.stringify(r[c] ?? '')).join(','))
  const blob   = new Blob([header + '\n' + lines.join('\n')], { type: 'text/csv' })
  downloadBlob(blob, 'decisions.csv')
}

function exportJSON(rows) {
  const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' })
  downloadBlob(blob, 'decisions.json')
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a   = Object.assign(document.createElement('a'), { href: url, download: filename })
  a.click()
  URL.revokeObjectURL(url)
}

// ── Sort indicator icon ────────────────────────────────────────────────────

function SortIcon({ field, sortField, sortDir }) {
  if (sortField !== field) return <ArrowUpDown className="w-3 h-3 opacity-30" />
  return sortDir === 'asc'
    ? <ArrowUp className="w-3 h-3 text-blue-400" />
    : <ArrowDown className="w-3 h-3 text-blue-400" />
}

// ── Agent display helpers ──────────────────────────────────────────────────

const AGENT_DISPLAY = {
  'monitoring-agent':        { label: 'Monitoring',    color: 'text-blue-400',   bg: 'bg-blue-500/10',   border: 'border-blue-500/25' },
  'cost-optimization-agent': { label: 'Cost',   color: 'text-amber-400',  bg: 'bg-amber-500/10',  border: 'border-amber-500/25' },
  'deploy-agent':            { label: 'Deploy', color: 'text-purple-400', bg: 'bg-purple-500/10', border: 'border-purple-500/25' },
}

function AgentBadge({ agentId }) {
  const cfg = AGENT_DISPLAY[agentId]
  if (!cfg) return <span className="text-xs text-slate-500">{agentId?.replace(/-agent$/, '') ?? '—'}</span>
  return (
    <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border ${cfg.color} ${cfg.bg} ${cfg.border}`}>
      {cfg.label}
    </span>
  )
}

// ── Component ─────────────────────────────────────────────────────────────

export default function DecisionTable({ evaluations, onSelect, onRefresh, initialAgent = 'all' }) {
  const [sortField,   setSortField]   = useState('timestamp')
  const [sortDir,     setSortDir]     = useState('desc')
  const [filterVerdict, setFilter]    = useState('all')
  const [filterAgent, setFilterAgent] = useState(initialAgent)
  const [searchText,  setSearch]      = useState('')
  const [pageSize,    setPageSize]    = useState(25)
  const [page,        setPage]        = useState(1)

  // Unique agents for dropdown
  const agentOptions = useMemo(() => {
    const names = new Set(evaluations.map(e => e.agent_id).filter(Boolean))
    return ['all', ...Array.from(names).sort()]
  }, [evaluations])

  function toggleSort(field) {
    if (sortField === field) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDir(field === 'timestamp' ? 'desc' : 'asc')
    }
    setPage(1)
  }

  // Filter + sort
  const filtered = useMemo(() => {
    let rows = evaluations.filter(ev => {
      if (filterVerdict !== 'all' && ev.decision?.toLowerCase() !== filterVerdict) return false
      if (filterAgent   !== 'all' && ev.agent_id !== filterAgent)                  return false
      if (searchText) {
        const q = searchText.toLowerCase()
        if (!ev.resource_id?.toLowerCase().includes(q) &&
            !ev.action_type?.toLowerCase().includes(q)) return false
      }
      return true
    })

    rows.sort((a, b) => {
      let av, bv
      if (sortField === 'sri')     { av = a.sri_composite ?? 0; bv = b.sri_composite ?? 0 }
      else if (sortField === 'verdict') { av = a.decision ?? ''; bv = b.decision ?? '' }
      else                         { av = a.timestamp ?? ''; bv = b.timestamp ?? '' }

      if (av < bv) return sortDir === 'asc' ? -1 : 1
      if (av > bv) return sortDir === 'asc' ?  1 : -1
      return 0
    })

    return rows
  }, [evaluations, filterVerdict, filterAgent, searchText, sortField, sortDir])

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage   = Math.min(page, totalPages)
  const pageRows   = filtered.slice((safePage - 1) * pageSize, safePage * pageSize)

  const hasFilters = filterVerdict !== 'all' || filterAgent !== 'all' || searchText

  function clearFilters() {
    setFilter('all')
    setFilterAgent('all')
    setSearch('')
    setPage(1)
  }

  function ThCol({ field, children, className = '' }) {
    return (
      <th
        className={`px-4 py-3 text-xs font-semibold text-slate-500 cursor-pointer hover:text-slate-300 transition-colors select-none ${className}`}
        onClick={() => toggleSort(field)}
      >
        <div className={`flex items-center gap-1 ${className.includes('text-right') ? 'justify-end' : className.includes('text-center') ? 'justify-center' : ''}`}>
          {children}
          <SortIcon field={field} sortField={sortField} sortDir={sortDir} />
        </div>
      </th>
    )
  }

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 flex flex-col">

      {/* ── Toolbar ── */}
      <div className="px-4 pt-4 pb-3 border-b border-slate-800 flex flex-wrap gap-3 items-end">

        {/* Search */}
        <div className="flex-1 min-w-32">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
            <input
              type="text"
              value={searchText}
              onChange={e => { setSearch(e.target.value); setPage(1) }}
              placeholder="Resource or action…"
              className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
            />
          </div>
        </div>

        {/* Verdict filter */}
        <select
          value={filterVerdict}
          onChange={e => { setFilter(e.target.value); setPage(1) }}
          className="bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
        >
          <option value="all">All verdicts</option>
          <option value="approved">Approved</option>
          <option value="escalated">Escalated</option>
          <option value="denied">Denied</option>
        </select>

        {/* Agent filter */}
        <select
          value={filterAgent}
          onChange={e => { setFilterAgent(e.target.value); setPage(1) }}
          className="bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
        >
          {agentOptions.map(a => (
            <option key={a} value={a}>
              {a === 'all' ? 'All agents' : (AGENT_DISPLAY[a]?.label ?? a.replace(/-agent$/, ''))}
            </option>
          ))}
        </select>

        {hasFilters && (
          <button
            onClick={clearFilters}
            className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
          >
            <X className="w-3 h-3" /> Clear
          </button>
        )}

        <div className="flex items-center gap-2 ml-auto">
          {/* Export buttons */}
          <button
            onClick={() => exportCSV(filtered)}
            title="Export CSV"
            className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
          >
            <Download className="w-3.5 h-3.5" /> CSV
          </button>
          <button
            onClick={() => exportJSON(filtered)}
            title="Export JSON"
            className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border border-slate-700 text-slate-400 hover:text-slate-200 transition-colors"
          >
            <Download className="w-3.5 h-3.5" /> JSON
          </button>
          {/* Row count */}
          <span className="text-xs text-slate-600 whitespace-nowrap">
            {filtered.length} row{filtered.length !== 1 ? 's' : ''}
          </span>
        </div>
      </div>

      {/* ── Table ── */}
      {pageRows.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-slate-500 gap-2">
          <span className="text-3xl">📭</span>
          <p className="text-sm">{hasFilters ? 'No matching decisions.' : 'No evaluations yet.'}</p>
          {hasFilters && (
            <button onClick={clearFilters} className="text-xs text-blue-400 hover:text-blue-300">
              Clear filters
            </button>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto flex-1">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-900 z-10">
              <tr className="border-b border-slate-800">
                <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Resource</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 hidden sm:table-cell">Action</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 hidden md:table-cell">Agent</th>
                <ThCol field="sri"     className="text-right">SRI</ThCol>
                <ThCol field="verdict" className="text-center">Verdict</ThCol>
                <ThCol field="timestamp" className="text-right hidden md:table-cell">Time</ThCol>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {pageRows.map(ev => (
                <tr
                  key={ev.action_id}
                  onClick={() => onSelect?.(ev)}
                  className="cursor-pointer hover:bg-slate-800/50 transition-colors group"
                >
                  <td className="px-4 py-3">
                    <span className="font-mono text-xs text-slate-200 group-hover:text-white transition-colors">
                      {shortResource(ev.resource_id)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-400 text-xs hidden sm:table-cell">
                    {ev.action_type?.replace(/_/g, ' ')}
                  </td>
                  <td className="px-4 py-3 hidden md:table-cell">
                    <AgentBadge agentId={ev.agent_id} />
                  </td>
                  <td className={`px-4 py-3 text-right font-bold tabular-nums text-xs ${sriColor(ev.sri_composite ?? 0)}`}>
                    {(ev.sri_composite ?? 0).toFixed(1)}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <VerdictBadge verdict={ev.decision} />
                  </td>
                  <td className="px-4 py-3 text-right text-xs text-slate-500 tabular-nums hidden md:table-cell">
                    {formatTime(ev.timestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Pagination ── */}
      {filtered.length > 0 && (
        <div className="px-4 py-3 border-t border-slate-800 flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <span>Rows per page:</span>
            <select
              value={pageSize}
              onChange={e => { setPageSize(Number(e.target.value)); setPage(1) }}
              className="bg-slate-800 border border-slate-700 rounded px-2 py-0.5 text-slate-200 focus:outline-none"
            >
              <option value={10}>10</option>
              <option value={25}>25</option>
              <option value={50}>50</option>
            </select>
          </div>

          <div className="flex items-center gap-1 text-xs">
            <button
              onClick={() => setPage(1)}
              disabled={safePage === 1}
              className="px-2 py-1 rounded border border-slate-700 text-slate-400 hover:text-slate-200 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              «
            </button>
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={safePage === 1}
              className="px-2 py-1 rounded border border-slate-700 text-slate-400 hover:text-slate-200 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              ‹
            </button>
            <span className="px-3 text-slate-400">
              Page {safePage} of {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={safePage === totalPages}
              className="px-2 py-1 rounded border border-slate-700 text-slate-400 hover:text-slate-200 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              ›
            </button>
            <button
              onClick={() => setPage(totalPages)}
              disabled={safePage === totalPages}
              className="px-2 py-1 rounded border border-slate-700 text-slate-400 hover:text-slate-200 disabled:opacity-30 disabled:cursor-not-allowed"
            >
              »
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
