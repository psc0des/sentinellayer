/**
 * LiveLogPanel.jsx — slide-out panel showing real-time SSE scan progress.
 *
 * Supports two modes:
 *   Single agent  — pass scanId + agentType (individual scan buttons)
 *   All agents    — pass scanEntries=[{scanId,agentType},…] (Run All Agents)
 *
 * In all-agents mode the three SSE streams are merged into one chronological
 * log and each line is tagged with a coloured agent badge.
 *
 * Props:
 *   scanId       — UUID from POST /api/scan/* (single-agent mode)
 *   agentType    — "cost" | "monitoring" | "deploy"  (single-agent mode)
 *   scanEntries  — [{scanId, agentType}, …]           (all-agents mode)
 *   onClose      — callback to close the panel
 *   isOpen       — bool, controls visibility
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { BASE } from '../api'

// ── Constants ───────────────────────────────────────────────────────────────

const AGENT_LABELS = {
  cost:       'Cost Agent',
  monitoring: 'Monitoring Agent',
  deploy:     'Deploy Agent',
  all:        'All Agents',
}

const AGENT_BADGE_STYLE = {
  cost:       'bg-yellow-900/60 text-yellow-300',
  monitoring: 'bg-blue-900/60  text-blue-300',
  deploy:     'bg-orange-900/60 text-orange-300',
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function eventStyle(event) {
  switch (event.event) {
    case 'scan_started':  return { icon: '🚀', colour: 'text-slate-400' }
    case 'discovery':     return { icon: '🔍', colour: 'text-blue-400' }
    case 'analysis':      return { icon: '🧠', colour: 'text-purple-400' }
    case 'reasoning':     return { icon: '🤔', colour: 'text-purple-300' }
    case 'proposal':      return { icon: '📋', colour: 'text-orange-400' }
    case 'evaluation':    return { icon: '⚖️', colour: 'text-yellow-400' }
    case 'execution':     return { icon: '⚙️', colour: 'text-cyan-400' }
    case 'persisted':     return { icon: '💾', colour: 'text-slate-500' }
    case 'scan_complete': return { icon: '✔️', colour: 'text-green-300' }
    case 'scan_error':    return { icon: '❌', colour: 'text-red-400' }
    // Backward compat
    case 'agent_returned': return { icon: '🔍', colour: 'text-blue-400' }
    case 'evaluating':     return { icon: '⚖️', colour: 'text-yellow-400' }
    case 'verdict': {
      const d = event.decision?.toLowerCase()
      if (d === 'approved')  return { icon: '✅', colour: 'text-green-400' }
      if (d === 'escalated') return { icon: '⚠️', colour: 'text-orange-400' }
      if (d === 'denied')    return { icon: '🚫', colour: 'text-red-400' }
      return { icon: '⚖️', colour: 'text-yellow-400' }
    }
    default: return { icon: '•', colour: 'text-slate-400' }
  }
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return '' }
}

// ── Log line ─────────────────────────────────────────────────────────────────

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

// ── Main panel ───────────────────────────────────────────────────────────────

export default function LiveLogPanel({ scanId, agentType, scanEntries, onClose, isOpen }) {
  // Normalise: always work with an array of {scanId, agentType} entries.
  const isMulti   = !!scanEntries?.length
  const entries   = isMulti ? scanEntries : (scanId ? [{ scanId, agentType }] : [])

  const [events,  setEvents]  = useState([])
  const [doneSet, setDoneSet] = useState(new Set())  // scanIds that have finished
  const esMapRef              = useRef({})            // scanId → EventSource
  const bottomRef             = useRef(null)

  const done = entries.length > 0 && doneSet.size >= entries.length

  // Open one EventSource per entry; merge events into one list.
  useEffect(() => {
    if (!isOpen || !entries.length) return

    setEvents([])
    setDoneSet(new Set())

    // Close any leftover connections from a previous render
    Object.values(esMapRef.current).forEach(es => es.close())
    esMapRef.current = {}

    entries.forEach(({ scanId: sid, agentType: atype }) => {
      const es = new EventSource(`${BASE}/scan/${sid}/stream`)
      esMapRef.current[sid] = es

      es.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data)
          // Tag every event with which agent it came from
          setEvents(prev => [...prev, { ...event, _agentType: atype }])
          if (event.event === 'scan_complete' || event.event === 'scan_error') {
            setDoneSet(prev => new Set([...prev, sid]))
            es.close()
            delete esMapRef.current[sid]
          }
        } catch { /* ignore malformed events */ }
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
  }, [isOpen, scanId, JSON.stringify(scanEntries)])

  // Auto-scroll to bottom on new events
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events])

  const handleClose = useCallback(() => {
    Object.values(esMapRef.current).forEach(es => es.close())
    esMapRef.current = {}
    onClose()
  }, [onClose])

  if (!isOpen || !entries.length) return null

  const title = isMulti ? 'All Agents — Live Scan Log' : `${AGENT_LABELS[agentType] ?? agentType} — Live Scan Log`
  const subtitle = isMulti
    ? entries.map(e => e.scanId.slice(0, 6)).join(' · ')
    : entries[0]?.scanId.slice(0, 8) + '…'

  // How many agents have finished (for the status line in multi mode)
  const doneCount = doneSet.size

  // Portal to document.body: bypasses any CSS stacking context from parent
  // elements (backdropFilter, overflow, transform) that would otherwise
  // confine a position:fixed child to a local containing block.
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
            {!done && (
              <span className="flex items-center gap-1.5 text-xs text-yellow-400 font-mono">
                <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />
                {isMulti ? `${doneCount}/${entries.length} done` : 'Scanning…'}
              </span>
            )}
            {done && (
              <span className="text-xs text-green-400 font-mono">Complete</span>
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

        {/* Agent legend (multi mode only) */}
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
              Connecting to scan stream…
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
            {!done && ' · streaming…'}
          </p>
        </div>
      </div>
    </>,
    document.body
  )
}
