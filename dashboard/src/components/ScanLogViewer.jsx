/**
 * ScanLogViewer.jsx — dual-mode scan log viewer (live SSE + historical).
 *
 * Replaces LiveLogPanel.jsx. Portaled to document.body to bypass CSS stacking.
 *
 * Mode A — Live (running scans):
 *   SSE via EventSource, auto-scroll, real-time append, elapsed timer.
 *
 * Mode B — Historical (completed scans):
 *   Fetches scan status and reconstructs structured log from evaluations.
 *   Shows completion summary with verdict counts.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { BASE, fetchScanStatus } from '../api'
import VerdictBadge from './magicui/VerdictBadge'

// ── Constants ───────────────────────────────────────────────────────────────

const AGENT_LABELS = {
  cost:       'Cost Agent',
  monitoring: 'Monitoring Agent',
  deploy:     'Deploy Agent',
  all:        'All Agents',
}

const AGENT_BADGE_STYLE = {
  cost:       'bg-yellow-900/60 text-yellow-300',
  monitoring: 'bg-blue-900/60 text-blue-300',
  deploy:     'bg-orange-900/60 text-orange-300',
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function eventStyle(event) {
  switch (event.event) {
    case 'scan_started':   return { icon: '\u{1F680}', colour: 'text-slate-400' }
    case 'discovery':      return { icon: '\u{1F50D}', colour: 'text-blue-400' }
    case 'analysis':       return { icon: '\u{1F9E0}', colour: 'text-purple-400' }
    case 'reasoning':      return { icon: '\u{1F914}', colour: 'text-purple-300' }
    case 'proposal':       return { icon: '\u{1F4CB}', colour: 'text-orange-400' }
    case 'evaluation':     return { icon: '\u2696\uFE0F', colour: 'text-yellow-400' }
    case 'execution':      return { icon: '\u2699\uFE0F', colour: 'text-cyan-400' }
    case 'persisted':      return { icon: '\u{1F4BE}', colour: 'text-slate-500' }
    case 'scan_complete':  return { icon: '\u2714\uFE0F', colour: 'text-green-300' }
    case 'scan_error':     return { icon: '\u274C', colour: 'text-red-400' }
    case 'agent_returned': return { icon: '\u{1F50D}', colour: 'text-blue-400' }
    case 'evaluating':     return { icon: '\u2696\uFE0F', colour: 'text-yellow-400' }
    case 'verdict': {
      const d = event.decision?.toLowerCase()
      if (d === 'approved')  return { icon: '\u2705', colour: 'text-green-400' }
      if (d === 'escalated') return { icon: '\u26A0\uFE0F', colour: 'text-orange-400' }
      if (d === 'denied')    return { icon: '\u{1F6AB}', colour: 'text-red-400' }
      return { icon: '\u2696\uFE0F', colour: 'text-yellow-400' }
    }
    default: return { icon: '\u2022', colour: 'text-slate-400' }
  }
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return '' }
}

function formatDuration(startedAt) {
  if (!startedAt) return ''
  const secs = Math.round((Date.now() - new Date(startedAt).getTime()) / 1000)
  if (secs < 60) return `${secs}s`
  return `${Math.floor(secs / 60)}m ${secs % 60}s`
}

function resourceName(resourceId) {
  if (!resourceId) return '—'
  return resourceId.split('/').filter(Boolean).pop() ?? resourceId
}

// ── LogLine (live mode) ─────────────────────────────────────────────────────

function LogLine({ event, showAgent }) {
  const { icon, colour } = eventStyle(event)
  const msg = event.message || JSON.stringify(event)
  const badgeStyle = AGENT_BADGE_STYLE[event._agentType] || 'bg-slate-800 text-slate-400'

  return (
    <div className="flex items-start gap-2 py-1 border-b border-slate-800/60 last:border-0">
      <span className="text-slate-600 font-mono text-xs shrink-0 w-20 pt-0.5">
        {fmtTime(event.timestamp)}
      </span>
      <span className="shrink-0 text-sm">{icon}</span>
      {showAgent && event._agentType && (
        <span className={`shrink-0 text-xs font-mono px-1.5 py-0.5 rounded ${badgeStyle}`}>
          {AGENT_LABELS[event._agentType] ?? event._agentType}
        </span>
      )}
      <span className={`text-xs font-mono leading-relaxed ${colour} break-words min-w-0`}>
        {msg}
      </span>
    </div>
  )
}

// ── EvaluationRow (historical mode) ─────────────────────────────────────────

function EvaluationRow({ ev }) {
  return (
    <div className="bg-slate-800/60 rounded-lg p-3 border border-slate-700/50 flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-sm font-mono text-slate-200 truncate">
          {resourceName(ev.resource_id)}
        </p>
        <p className="text-xs text-slate-500 mt-0.5">
          {ev.action_type?.replace(/_/g, ' ')}
        </p>
        {(ev.reason || ev.verdict_reason || ev.action_reason) && (
          <p className="text-xs text-slate-600 mt-1 line-clamp-2">
            {ev.reason || ev.verdict_reason || ev.action_reason}
          </p>
        )}
        {ev.violations?.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {ev.violations.map((v, i) => (
              <span key={i} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-rose-900/30 text-rose-400 border border-rose-500/20">
                {v.policy_id || v.rule}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="shrink-0 text-right">
        <VerdictBadge verdict={ev.decision} />
        {ev.sri_composite != null && (
          <p className="text-xs text-slate-500 mt-1 font-mono">
            SRI {ev.sri_composite.toFixed(1)}
          </p>
        )}
      </div>
    </div>
  )
}

// ── LiveLogBody ─────────────────────────────────────────────────────────────

function LiveLogBody({ scanId, agentType, scanEntries }) {
  const isMulti = !!scanEntries?.length
  const entries = isMulti ? scanEntries : (scanId ? [{ scanId, agentType }] : [])

  const [events, setEvents]   = useState([])
  const [doneSet, setDoneSet] = useState(new Set())
  const esMapRef              = useRef({})
  const bottomRef             = useRef(null)

  const done = entries.length > 0 && doneSet.size >= entries.length

  useEffect(() => {
    if (!entries.length) return

    setEvents([])
    setDoneSet(new Set())
    Object.values(esMapRef.current).forEach(es => es.close())
    esMapRef.current = {}

    entries.forEach(({ scanId: sid, agentType: atype }) => {
      const es = new EventSource(`${BASE}/scan/${sid}/stream`)
      esMapRef.current[sid] = es

      es.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data)
          setEvents(prev => [...prev, { ...event, _agentType: atype }])
          if (event.event === 'scan_complete' || event.event === 'scan_error') {
            setDoneSet(prev => new Set([...prev, sid]))
            es.close()
            delete esMapRef.current[sid]
          }
        } catch { /* ignore malformed */ }
      }

      es.onerror = () => {
        setDoneSet(prev => new Set([...prev, sid]))
        es.close()
        delete esMapRef.current[sid]
      }
    })

    return () => {
      Object.values(esMapRef.current).forEach(es => es.close())
      esMapRef.current = {}
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanId, JSON.stringify(scanEntries)])

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events])

  // Cleanup SSE on unmount
  useEffect(() => {
    return () => {
      Object.values(esMapRef.current).forEach(es => es.close())
      esMapRef.current = {}
    }
  }, [])

  return (
    <>
      {/* Agent legend (multi mode) */}
      {isMulti && (
        <div className="flex gap-2 px-4 py-2 border-b border-slate-800 shrink-0">
          {entries.map(({ agentType: atype }) => (
            <span key={atype} className={`text-xs font-mono px-2 py-0.5 rounded ${AGENT_BADGE_STYLE[atype] ?? 'bg-slate-800 text-slate-400'}`}>
              {AGENT_LABELS[atype]}
            </span>
          ))}
        </div>
      )}

      {/* Log body */}
      <div className="flex-1 overflow-y-auto px-4 py-3 font-mono">
        {events.length === 0 && (
          <p className="text-slate-600 text-xs text-center py-8">
            Connecting to scan stream...
          </p>
        )}
        {events.map((ev, i) => (
          <LogLine key={i} event={ev} showAgent={isMulti} />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Footer */}
      <div className="px-4 py-3 border-t border-slate-700 shrink-0">
        <p className="text-xs text-slate-600">
          {events.length} event{events.length !== 1 ? 's' : ''}
          {!done && ' \u00B7 streaming\u2026'}
        </p>
      </div>
    </>
  )
}

// ── HistoricalLogBody ───────────────────────────────────────────────────────

function HistoricalLogBody({ scanId }) {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)

  useEffect(() => {
    if (!scanId) return
    setLoading(true)
    setError(null)
    fetchScanStatus(scanId)
      .then(setData)
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [scanId])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-slate-500 text-sm">Loading scan data...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-red-400 text-sm">{error}</p>
      </div>
    )
  }

  if (!data) return null

  const evaluations = data.evaluations ?? []
  const proposals   = data.proposals_count ?? data.proposed_actions?.length ?? 0
  const approved    = evaluations.filter(e => e.decision?.toLowerCase() === 'approved').length
  const denied      = evaluations.filter(e => e.decision?.toLowerCase() === 'denied').length
  const escalated   = evaluations.filter(e => e.decision?.toLowerCase() === 'escalated').length

  return (
    <>
      {/* Summary bar */}
      <div className="px-4 py-3 border-b border-slate-800 shrink-0">
        <div className="flex items-center gap-4 text-xs">
          <span className="text-slate-400">
            {proposals} proposal{proposals !== 1 ? 's' : ''}{' → '}{evaluations.length} verdict{evaluations.length !== 1 ? 's' : ''}
          </span>
          {evaluations.length > 0 && (
            <div className="flex items-center gap-3">
              <span className="text-emerald-400">{approved} approved</span>
              <span className="text-amber-400">{escalated} escalated</span>
              <span className="text-rose-400">{denied} denied</span>
            </div>
          )}
        </div>
      </div>

      {/* Evaluations list */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {evaluations.length === 0 ? (
          <div className="text-center py-8 space-y-2">
            <p className="text-sm font-medium text-green-400">Scan completed — no issues found</p>
            <p className="text-xs text-slate-500">
              {proposals} proposal{proposals !== 1 ? 's' : ''} checked, 0 issues
            </p>
          </div>
        ) : (
          evaluations.map((ev, i) => <EvaluationRow key={i} ev={ev} />)
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-3 border-t border-slate-700 shrink-0">
        <p className="text-xs text-slate-600">
          {'Status: '}{data.status}{' · '}{evaluations.length} evaluation{evaluations.length !== 1 ? 's' : ''}
        </p>
      </div>
    </>
  )
}

// ── Main Component ──────────────────────────────────────────────────────────

export default function ScanLogViewer({
  scanId,
  agentType,
  scanEntries,
  mode,        // 'live' | 'historical'
  isOpen,
  onClose,
  startedAt,
}) {
  const [elapsed, setElapsed] = useState('')

  // Elapsed timer for live mode
  useEffect(() => {
    if (!isOpen || mode !== 'live' || !startedAt) {
      setElapsed('')
      return
    }
    const tick = () => setElapsed(formatDuration(startedAt))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [isOpen, mode, startedAt])

  const handleClose = useCallback(() => {
    onClose()
  }, [onClose])

  if (!isOpen) return null

  const isMulti = !!scanEntries?.length
  const title = mode === 'historical'
    ? `${AGENT_LABELS[agentType] ?? agentType} \u2014 Scan Results`
    : isMulti
      ? 'All Agents \u2014 Live Scan Log'
      : `${AGENT_LABELS[agentType] ?? agentType} \u2014 Live Scan Log`

  const subtitle = isMulti
    ? scanEntries.map(e => e.scanId?.slice(0, 6)).join(' \u00B7 ')
    : scanId
      ? scanId.slice(0, 8) + '\u2026'
      : ''

  return createPortal(
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/40 z-30"
        onClick={handleClose}
        aria-hidden="true"
      />

      {/* Panel */}
      <div className="fixed top-0 right-0 h-full w-full max-w-lg bg-slate-900 border-l border-slate-700 shadow-2xl z-40 flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700 shrink-0">
          <div>
            <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
            <p className="text-xs text-slate-500 font-mono mt-0.5">{subtitle}</p>
          </div>
          <div className="flex items-center gap-3">
            {mode === 'live' && (
              <span className="flex items-center gap-1.5 text-xs text-yellow-400 font-mono">
                <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />
                {elapsed || 'Scanning\u2026'}
              </span>
            )}
            {mode === 'historical' && (
              <span className="text-xs text-slate-500 font-mono">Historical</span>
            )}
            <button
              onClick={handleClose}
              className="text-slate-500 hover:text-slate-200 transition-colors text-lg leading-none"
              title="Close log panel"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Body — delegates to mode-specific sub-component */}
        {mode === 'live' ? (
          <LiveLogBody scanId={scanId} agentType={agentType} scanEntries={scanEntries} />
        ) : (
          <HistoricalLogBody scanId={scanId} />
        )}
      </div>
    </>,
    document.body
  )
}
