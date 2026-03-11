/**
 * AuditLog.jsx — chronological, filterable audit log of all governance events.
 *
 * Features:
 *   - Filter by verdict, agent, date range, and free-text resource search
 *   - Export to CSV or JSON
 *   - Click any row → slide-in details panel showing every resource/field
 *     the LLM examined: full reason, violations, SRI breakdown, verdict rationale
 *   - Shows all evaluations from /api/evaluations
 */

import React, { useState, useMemo } from 'react'
import { useOutletContext } from 'react-router-dom'
import { Download, Search, Filter, X, ChevronRight, Copy, Check } from 'lucide-react'
import VerdictBadge from '../components/magicui/VerdictBadge'

// ── Helpers ────────────────────────────────────────────────────────────────

function sriColor(score) {
  if (score <= 25) return 'text-emerald-400'
  if (score <= 60) return 'text-amber-400'
  return 'text-rose-400'
}

function sriBg(score) {
  if (score <= 25) return 'bg-emerald-500'
  if (score <= 60) return 'bg-amber-500'
  return 'bg-rose-500'
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function shortResource(id) {
  return id?.split('/').filter(Boolean).pop() ?? id ?? '—'
}

// ── Export helpers ─────────────────────────────────────────────────────────

function exportCSV(rows) {
  const cols = ['action_id', 'timestamp', 'agent_id', 'action_type', 'resource_id', 'sri_composite', 'decision']
  const header = cols.join(',')
  const lines  = rows.map(r =>
    cols.map(c => JSON.stringify(r[c] ?? '')).join(',')
  )
  const blob = new Blob([header + '\n' + lines.join('\n')], { type: 'text/csv' })
  download(blob, 'ruriskry-audit.csv')
}

function exportJSON(rows) {
  const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' })
  download(blob, 'ruriskry-audit.json')
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a   = document.createElement('a')
  a.href    = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// ── CopyButton ─────────────────────────────────────────────────────────────

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  function handleCopy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <button onClick={handleCopy} className="ml-1.5 text-slate-500 hover:text-slate-300 transition-colors flex-shrink-0">
      {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  )
}

// ── SRI Bar ────────────────────────────────────────────────────────────────

function SriBar({ label, value }) {
  const pct = Math.min(100, Math.max(0, value ?? 0))
  return (
    <div>
      <div className="flex justify-between items-center mb-1">
        <span className="text-xs text-slate-400 capitalize">{label}</span>
        <span className={`text-xs font-bold tabular-nums ${sriColor(pct)}`}>{pct.toFixed(1)}</span>
      </div>
      <div className="h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all ${sriBg(pct)}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

// ── Details Panel ──────────────────────────────────────────────────────────

function DetailsPanel({ ev, onClose }) {
  if (!ev) return null

  const breakdown = ev.sri_breakdown ?? {}
  const violations = ev.violations ?? []
  const tirageTier = ev.triage_tier
  const triageMode = ev.triage_mode

  const tierLabel = tirageTier === 1 ? 'Tier 1 — Deterministic (fast-path)' :
                    tirageTier === 2 ? 'Tier 2 — Targeted LLM review' :
                    tirageTier === 3 ? 'Tier 3 — Full LLM governance' :
                    tirageTier != null ? `Tier ${tirageTier}` : null

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/40 z-40"
        onClick={onClose}
      />

      {/* Slide-in panel */}
      <div className="fixed right-0 top-0 bottom-0 w-full max-w-lg bg-slate-950 border-l border-slate-800 z-50 overflow-y-auto shadow-2xl flex flex-col">

        {/* Header */}
        <div className="sticky top-0 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-start justify-between gap-3 z-10">
          <div className="min-w-0">
            <p className="text-xs text-slate-500 uppercase tracking-wide font-semibold mb-0.5">
              {ev.agent_id?.replace(/-agent$/, '') ?? '—'} · {ev.action_type?.replace(/_/g, ' ') ?? '—'}
            </p>
            <h2 className="text-sm font-bold text-white truncate" title={ev.resource_id}>
              {shortResource(ev.resource_id)}
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">{formatTime(ev.timestamp)}</p>
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200 transition-colors flex-shrink-0 mt-0.5">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 px-5 py-4 space-y-5">

          {/* Verdict */}
          <div className="flex items-center gap-3">
            <VerdictBadge verdict={ev.decision} size="lg" />
            <span className={`text-xs font-bold tabular-nums ${sriColor(ev.sri_composite ?? 0)}`}>
              SRI {(ev.sri_composite ?? 0).toFixed(1)}
            </span>
            {tirageTier != null && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700">
                {tierLabel ?? `Tier ${tirageTier}`}
              </span>
            )}
          </div>

          {/* Full resource ID */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Resource Examined</h3>
            <div className="bg-slate-900 rounded-lg p-3 border border-slate-800">
              <div className="flex items-start gap-1">
                <span className="text-xs font-mono text-slate-300 break-all leading-relaxed">{ev.resource_id ?? '—'}</span>
                {ev.resource_id && <CopyButton text={ev.resource_id} />}
              </div>
              {ev.resource_type && (
                <p className="text-xs text-slate-500 mt-1">{ev.resource_type}</p>
              )}
            </div>
          </section>

          {/* What the LLM found — action reason */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">What the Agent Found</h3>
            <div className="bg-slate-900 rounded-lg p-3 border border-slate-800">
              <p className="text-xs text-slate-300 leading-relaxed whitespace-pre-wrap">
                {ev.action_reason ?? '—'}
              </p>
            </div>
          </section>

          {/* SRI Breakdown */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">Risk Score Breakdown (SRI™)</h3>
            <div className="bg-slate-900 rounded-lg p-3 border border-slate-800 space-y-3">
              <SriBar label="Infrastructure" value={breakdown.infrastructure} />
              <SriBar label="Policy"         value={breakdown.policy} />
              <SriBar label="Historical"     value={breakdown.historical} />
              <SriBar label="Financial"      value={breakdown.cost} />
              <div className="pt-1 border-t border-slate-800 flex justify-between">
                <span className="text-xs text-slate-500 font-semibold">Composite</span>
                <span className={`text-xs font-bold tabular-nums ${sriColor(ev.sri_composite ?? 0)}`}>
                  {(ev.sri_composite ?? 0).toFixed(1)} / 100
                </span>
              </div>
            </div>
          </section>

          {/* Policy violations */}
          {violations.length > 0 && (
            <section>
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">
                Policy Violations ({violations.length})
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {violations.map(v => (
                  <span key={v} className="text-xs font-mono px-2 py-0.5 rounded bg-rose-500/10 border border-rose-500/30 text-rose-400">
                    {v}
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Governance verdict rationale */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Governance Rationale</h3>
            <div className="bg-slate-900 rounded-lg p-3 border border-slate-800">
              <p className="text-xs text-slate-300 leading-relaxed">
                {ev.verdict_reason ?? '—'}
              </p>
            </div>
          </section>

          {/* Triage context */}
          {(tirageTier != null || triageMode) && (
            <section>
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Triage Context</h3>
              <div className="bg-slate-900 rounded-lg p-3 border border-slate-800 space-y-1.5">
                {tirageTier != null && (
                  <div className="flex justify-between">
                    <span className="text-xs text-slate-500">Tier</span>
                    <span className="text-xs text-slate-300">{tierLabel ?? tirageTier}</span>
                  </div>
                )}
                {triageMode && (
                  <div className="flex justify-between">
                    <span className="text-xs text-slate-500">Mode</span>
                    <span className="text-xs text-slate-300 capitalize">{triageMode}</span>
                  </div>
                )}
              </div>
            </section>
          )}

          {/* Audit IDs */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Audit Reference</h3>
            <div className="bg-slate-900 rounded-lg p-3 border border-slate-800 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500 flex-shrink-0">Action ID</span>
                <div className="flex items-center min-w-0">
                  <span className="text-xs font-mono text-slate-400 truncate">{ev.action_id}</span>
                  <CopyButton text={ev.action_id} />
                </div>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500 flex-shrink-0">Agent</span>
                <span className="text-xs font-mono text-blue-400">{ev.agent_id ?? '—'}</span>
              </div>
            </div>
          </section>

        </div>
      </div>
    </>
  )
}

// ── Component ─────────────────────────────────────────────────────────────

export default function AuditLog() {
  const { evaluations } = useOutletContext()

  const [searchText,    setSearchText]    = useState('')
  const [filterVerdict, setFilterVerdict] = useState('all')
  const [filterAgent,   setFilterAgent]   = useState('all')
  const [dateFrom,      setDateFrom]      = useState('')
  const [dateTo,        setDateTo]        = useState('')
  const [selectedEv,    setSelectedEv]    = useState(null)

  // Unique agent names for filter dropdown
  const agentOptions = useMemo(() => {
    const names = new Set(evaluations.map(e => e.agent_id).filter(Boolean))
    return ['all', ...Array.from(names).sort()]
  }, [evaluations])

  // Apply all filters
  const filtered = useMemo(() => {
    return evaluations.filter(ev => {
      if (filterVerdict !== 'all' && ev.decision?.toLowerCase() !== filterVerdict) return false
      if (filterAgent   !== 'all' && ev.agent_id !== filterAgent)                  return false
      if (searchText && !ev.resource_id?.toLowerCase().includes(searchText.toLowerCase()) &&
                        !ev.action_type?.toLowerCase().includes(searchText.toLowerCase())) return false
      if (dateFrom && ev.timestamp < dateFrom) return false
      if (dateTo) {
        const endOfDay = dateTo + 'T23:59:59Z'
        if (ev.timestamp > endOfDay) return false
      }
      return true
    })
  }, [evaluations, filterVerdict, filterAgent, searchText, dateFrom, dateTo])

  const hasActiveFilters = filterVerdict !== 'all' || filterAgent !== 'all' || searchText || dateFrom || dateTo

  function clearFilters() {
    setSearchText('')
    setFilterVerdict('all')
    setFilterAgent('all')
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
            {filtered.length} of {evaluations.length} events · click any row to inspect
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

        {/* Resource / action search */}
        <div className="flex-1 min-w-40">
          <label className="block text-xs text-slate-500 mb-1">Search</label>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500" />
            <input
              type="text"
              value={searchText}
              onChange={e => setSearchText(e.target.value)}
              placeholder="Resource or action…"
              className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
            />
          </div>
        </div>

        {/* Verdict filter */}
        <div className="min-w-28">
          <label className="block text-xs text-slate-500 mb-1">Verdict</label>
          <select
            value={filterVerdict}
            onChange={e => setFilterVerdict(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          >
            <option value="all">All verdicts</option>
            <option value="approved">Approved</option>
            <option value="escalated">Escalated</option>
            <option value="denied">Denied</option>
          </select>
        </div>

        {/* Agent filter */}
        <div className="min-w-36">
          <label className="block text-xs text-slate-500 mb-1">Agent</label>
          <select
            value={filterAgent}
            onChange={e => setFilterAgent(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          >
            {agentOptions.map(a => (
              <option key={a} value={a}>
                {a === 'all' ? 'All agents' : a.replace(/-agent$/, '')}
              </option>
            ))}
          </select>
        </div>

        {/* Date range */}
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
            <Filter className="w-8 h-8 mx-auto mb-2 opacity-40" />
            <p className="text-sm font-medium">No events match your filters</p>
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
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Timestamp</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Agent</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Resource</th>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500">Action</th>
                  <th className="text-right px-4 py-3 text-xs font-semibold text-slate-500">SRI</th>
                  <th className="text-center px-4 py-3 text-xs font-semibold text-slate-500">Verdict</th>
                  <th className="px-4 py-3 w-6" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {filtered.map(ev => (
                  <tr
                    key={ev.action_id}
                    onClick={() => setSelectedEv(ev)}
                    className={`cursor-pointer transition-colors hover:bg-slate-800/60 ${
                      selectedEv?.action_id === ev.action_id ? 'bg-slate-800/80 ring-1 ring-inset ring-blue-500/30' : ''
                    }`}
                  >
                    <td className="px-4 py-2.5 text-xs text-slate-500 tabular-nums whitespace-nowrap">
                      {formatTime(ev.timestamp)}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-blue-400 font-mono whitespace-nowrap">
                      {ev.agent_id?.replace(/-agent$/, '') ?? '—'}
                    </td>
                    <td className="px-4 py-2.5 text-xs font-mono text-slate-300 max-w-48 truncate" title={ev.resource_id}>
                      {shortResource(ev.resource_id)}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-slate-400 whitespace-nowrap">
                      {ev.action_type?.replace(/_/g, ' ') ?? '—'}
                    </td>
                    <td className={`px-4 py-2.5 text-right text-xs font-bold tabular-nums ${sriColor(ev.sri_composite ?? 0)}`}>
                      {(ev.sri_composite ?? 0).toFixed(1)}
                    </td>
                    <td className="px-4 py-2.5 text-center">
                      <VerdictBadge verdict={ev.decision} />
                    </td>
                    <td className="px-3 py-2.5 text-slate-700">
                      <ChevronRight className="w-3.5 h-3.5" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Details panel ── */}
      {selectedEv && (
        <DetailsPanel ev={selectedEv} onClose={() => setSelectedEv(null)} />
      )}
    </div>
  )
}
