/**
 * AgentCardGrid.jsx — agent cards with inline scan controls + details menu.
 *
 * Each card has:
 *   - Primary: "Run Scan" (idle) / "Stop" (running)
 *   - Secondary: "Live Log" (running) / "Last Run" (idle, historical viewer)
 *   - ··· menu: Last Run Results (modal), History (modal), Agent Details (modal)
 */

import React, { useState, useEffect, useCallback } from 'react'
import {
  DollarSign, Activity, Shield, Zap, Play, Square,
  ScrollText, Clock, MoreHorizontal, ClipboardList,
  BarChart2, Info, X, AlertTriangle,
} from 'lucide-react'
import GlowCard from './magicui/GlowCard'
import VerdictBadge from './magicui/VerdictBadge'
import {
  fetchAgentLastRun,
  fetchAgentHistory,
} from '../api'

// ── Constants ───────────────────────────────────────────────────────────────

const AGENT_META = {
  cost:       { label: 'Cost Agent',       desc: 'Find idle or over-provisioned resources', Icon: DollarSign },
  monitoring: { label: 'Monitoring Agent', desc: 'Check health metrics + anomaly alerts',   Icon: Activity },
  deploy:     { label: 'Deploy Agent',     desc: 'Audit NSG rules and config drift',        Icon: Shield },
}

const AGENT_NAMES = {
  cost:       'cost-optimization-agent',
  monitoring: 'monitoring-agent',
  deploy:     'deploy-agent',
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function isOnline(lastSeen, minutes = 5) {
  if (!lastSeen) return false
  return Date.now() - new Date(lastSeen).getTime() < minutes * 60_000
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function useElapsed(startedAt) {
  const [elapsed, setElapsed] = useState('')
  useEffect(() => {
    if (!startedAt) { setElapsed(''); return }
    const tick = () => {
      const secs = Math.round((Date.now() - new Date(startedAt).getTime()) / 1000)
      setElapsed(secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [startedAt])
  return elapsed
}

function Spinner() {
  return (
    <span className="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin" aria-hidden="true" />
  )
}

// ── Modal wrapper ────────────────────────────────────────────────────────────

function Modal({ title, onClose, children }) {
  return (
    <>
      <div className="fixed inset-0 bg-black/50 z-30" onClick={onClose} aria-hidden="true" />
      <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
        <div className="bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-700 shrink-0">
            <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
            <button onClick={onClose} className="text-slate-500 hover:text-slate-200 transition-colors">
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto px-5 py-4">
            {children}
          </div>
        </div>
      </div>
    </>
  )
}

// ── Last Run Results modal ───────────────────────────────────────────────────

function LastRunModal({ agentName, onClose }) {
  const [data, setData]     = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchAgentLastRun(agentName)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [agentName])

  const evaluations   = data?.evaluations ?? []
  const proposedCount = data?.proposals_count ?? data?.proposed_actions?.length ?? 0
  const isClean       = evaluations.length === 0 && data?.status === 'complete'

  return (
    <Modal title={`Last Run — ${agentName}`} onClose={onClose}>
      {loading ? (
        <p className="text-slate-500 text-sm text-center py-6">Loading...</p>
      ) : !data || data.status === 'no_data' ? (
        <p className="text-slate-500 text-sm text-center py-6">No results found. Run a scan first.</p>
      ) : isClean ? (
        <div className="text-center py-6 space-y-2">
          <div className="text-3xl">✅</div>
          <p className="text-sm font-medium text-green-400">Scan completed — no issues found</p>
          <p className="text-xs text-slate-500">
            {formatTime(data.completed_at ?? data.started_at)}<br />
            {proposedCount} proposal(s) checked · 0 issues
          </p>
        </div>
      ) : evaluations.length === 0 ? (
        <p className="text-slate-500 text-sm text-center py-6">No results found. Run a scan first.</p>
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-slate-500 mb-3">
            Last run: {formatTime(data?.completed_at ?? data?.started_at)}<br />
            {proposedCount} action(s) proposed · {evaluations.length} verdict(s)
            {data?.scan_id && <span className="block font-mono text-slate-600 mt-0.5">scan {data.scan_id.slice(0, 8)}…</span>}
          </p>
          {evaluations.map((ev, i) => (
            <div key={i} className="bg-slate-800 rounded-lg p-3 border border-slate-700 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-mono text-slate-200 truncate">
                  {(ev.resource_id ?? '').split('/').filter(Boolean).pop() ?? ev.resource_id}
                </p>
                <p className="text-xs text-slate-500 mt-0.5">{ev.action_type?.replace(/_/g, ' ')}</p>
                <p className="text-xs text-slate-600 mt-1 line-clamp-2">
                  {ev.reason || ev.verdict_reason || ev.action_reason || 'No reasoning provided.'}
                </p>
              </div>
              <div className="shrink-0 text-right">
                <VerdictBadge verdict={ev.decision} />
                {ev.sri_composite != null && (
                  <p className="text-xs text-slate-500 mt-1 font-mono">SRI {ev.sri_composite.toFixed(1)}</p>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

// ── History modal ────────────────────────────────────────────────────────────

function HistoryModal({ agentName, onClose }) {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchAgentHistory(agentName, 30)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [agentName])

  const history = data?.history ?? []

  return (
    <Modal title={`History — ${agentName}`} onClose={onClose}>
      {loading ? (
        <p className="text-slate-500 text-sm text-center py-6">Loading...</p>
      ) : history.length === 0 ? (
        <p className="text-slate-500 text-sm text-center py-6">No history records found.</p>
      ) : (
        <div className="space-y-2">
          {history.map((rec, i) => (
            <div key={i} className="bg-slate-800 rounded-lg p-3 border border-slate-700 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-mono text-slate-200 truncate">
                  {(rec.resource_id ?? '').split('/').filter(Boolean).pop() ?? rec.resource_id}
                </p>
                <p className="text-xs text-slate-500 mt-0.5">
                  {rec.action_type?.replace(/_/g, ' ')} · {formatTime(rec.timestamp)}
                </p>
              </div>
              <VerdictBadge verdict={rec.decision} />
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

// ── Agent Details modal ──────────────────────────────────────────────────────

function DetailsModal({ agent, agentLabel, onClose }) {
  const rows = [
    { label: 'Name',           value: agent.name },
    { label: 'Registered',     value: formatTime(agent.registered_at) },
    { label: 'Last seen',      value: formatTime(agent.last_seen) },
    { label: 'Total proposed', value: agent.total_actions_proposed ?? 0 },
    { label: 'Approved',       value: agent.approval_count ?? 0 },
    { label: 'Escalated',      value: agent.escalation_count ?? 0 },
    { label: 'Denied',         value: agent.denial_count ?? 0 },
    { label: 'Agent card URL', value: agent.agent_card_url ?? '—' },
  ]
  return (
    <Modal title={`Agent Details — ${agentLabel}`} onClose={onClose}>
      <dl className="space-y-2">
        {rows.map(({ label, value }) => (
          <div key={label} className="flex justify-between gap-4 text-sm border-b border-slate-800 pb-2 last:border-0">
            <dt className="text-slate-500 shrink-0">{label}</dt>
            <dd className="text-slate-200 font-mono text-right truncate">{String(value)}</dd>
          </div>
        ))}
      </dl>
    </Modal>
  )
}

// ── ··· Dropdown menu ────────────────────────────────────────────────────────
// Note: no panel state here — lives in AgentCard so it survives menu close.

function CardMenu({ agentType, scan, onOpenPanel, onViewHistory, onClose }) {
  return (
    <div
      className="absolute top-8 right-0 z-50 bg-slate-900 border border-slate-700 rounded-lg shadow-2xl py-1 w-44"
      onClick={e => e.stopPropagation()}
    >
      <button
        onClick={() => { onOpenPanel('results'); onClose() }}
        className="flex items-center gap-2 w-full px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-slate-100 cursor-pointer transition-colors"
      >
        <ClipboardList className="w-3.5 h-3.5 shrink-0" /> Last Run Results
      </button>
      <button
        onClick={() => { onOpenPanel('history'); onClose() }}
        className="flex items-center gap-2 w-full px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-slate-100 cursor-pointer transition-colors"
      >
        <BarChart2 className="w-3.5 h-3.5 shrink-0" /> History
      </button>
      {scan.scanId && scan.status !== 'running' && (
        <button
          onClick={() => { onViewHistory(scan.scanId, agentType); onClose() }}
          className="flex items-center gap-2 w-full px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-slate-100 cursor-pointer transition-colors"
        >
          <ScrollText className="w-3.5 h-3.5 shrink-0" /> View Last Log
        </button>
      )}
      <button
        onClick={() => { onOpenPanel('details'); onClose() }}
        className="flex items-center gap-2 w-full px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-slate-100 cursor-pointer transition-colors"
      >
        <Info className="w-3.5 h-3.5 shrink-0" /> Agent Details
      </button>
    </div>
  )
}

// ── AgentCard ────────────────────────────────────────────────────────────────

function AgentCard({ agent, agentType, scan, onStart, onStop, onViewLive, onViewHistory }) {
  const meta      = AGENT_META[agentType]
  const [menuOpen, setMenuOpen] = useState(false)
  // panel state lives HERE so it survives the menu closing
  const [panel, setPanel] = useState(null)   // 'results' | 'history' | 'details' | null

  const agentName  = AGENT_NAMES[agentType]
  const agentLabel = meta?.label ?? agentType

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return
    const handler = () => setMenuOpen(false)
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [menuOpen])

  if (!meta) return null

  const online      = isOnline(agent.last_seen)
  const isRunning   = scan.status === 'running'
  const isStopping  = scan.status === 'stopping'
  const total     = agent.total_actions_proposed ?? 0
  const approved  = agent.approval_count ?? 0
  const denied    = agent.denial_count ?? 0
  const escalated = agent.escalation_count ?? 0
  const pct       = (n) => (total > 0 ? (n / total) * 100 : 0)
  const elapsed   = useElapsed((isRunning || isStopping) ? scan.startedAt : null)
  const { Icon }  = meta

  return (
    <>
    <GlowCard
      color={isStopping ? 'amber' : isRunning ? 'amber' : online ? 'green' : 'slate'}
      intensity={online || isRunning || isStopping ? 'medium' : 'low'}
      beam={online || isRunning || isStopping}
      beamDuration={isRunning || isStopping ? 1.5 : 4}
      className="p-4 flex flex-col gap-3"
    >
      {/* Name + icon + ··· menu */}
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full shrink-0 ${
          isStopping ? 'bg-orange-400 animate-pulse'
          : isRunning ? 'bg-amber-400 animate-pulse'
          : online ? 'bg-green-500 animate-pulse'
          : 'bg-slate-600'
        }`} />
        <Icon className="w-4 h-4 text-slate-400 shrink-0" />
        <span className="text-sm font-medium text-slate-200 truncate flex-1" title={agent.name}>
          {meta.label}
        </span>
        {/* ··· button */}
        <div className="relative shrink-0">
          <button
            onClick={e => { e.stopPropagation(); setMenuOpen(v => !v) }}
            className="w-7 h-7 flex items-center justify-center rounded text-slate-500 hover:text-slate-200 hover:bg-slate-700 transition-colors cursor-pointer"
            title="More options"
          >
            <MoreHorizontal className="w-4 h-4" />
          </button>
          {menuOpen && (
            <CardMenu
              agentType={agentType}
              scan={scan}
              onOpenPanel={setPanel}
              onViewHistory={onViewHistory}
              onClose={() => setMenuOpen(false)}
            />
          )}
        </div>
      </div>

      {/* Total actions + bar */}
      <div>
        <span className="text-2xl font-bold text-white tabular-nums">{total}</span>
        <span className="text-xs text-slate-500 ml-1.5">actions proposed</span>
      </div>
      <div className="space-y-1.5">
        <div className="flex h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(30,41,59,0.8)' }}>
          {total === 0 ? (
            <div className="w-full rounded-full" style={{ background: 'rgba(51,65,85,0.5)' }} />
          ) : (
            <>
              {approved  > 0 && <div className="bg-emerald-500/80" style={{ width: `${pct(approved)}%` }} />}
              {escalated > 0 && <div className="bg-amber-500/80"   style={{ width: `${pct(escalated)}%` }} />}
              {denied    > 0 && <div className="bg-rose-500/80"    style={{ width: `${pct(denied)}%` }} />}
            </>
          )}
        </div>
        <div className="flex justify-between text-[11px]">
          <span className="text-emerald-400/80">{approved} appr.</span>
          <span className="text-amber-400/80">{escalated} esc.</span>
          <span className="text-rose-400/80">{denied} denied</span>
        </div>
      </div>

      {/* Status line */}
      <p className="text-xs text-slate-500">
        {isStopping ? (
          <span className="text-orange-400 font-mono flex items-center gap-1.5">
            <Clock className="w-3 h-3" />
            Stopping… {elapsed}
          </span>
        ) : isRunning ? (
          <span className="text-amber-400 font-mono flex items-center gap-1.5">
            <Clock className="w-3 h-3" />
            Scanning… {elapsed}
          </span>
        ) : (
          <>Last seen: <span className="text-slate-400">{formatTime(agent.last_seen)}</span></>
        )}
      </p>

      {/* Action buttons */}
      <div className="flex gap-2 mt-auto">
        {isStopping ? (
          <div className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium border border-orange-500/30 bg-orange-500/10 text-orange-400 cursor-not-allowed opacity-75 select-none">
            <Spinner /> Stopping…
          </div>
        ) : isRunning ? (
          <>
            <button
              onClick={() => onStop(agentType)}
              className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium border border-red-500/30 bg-red-500/10 text-red-400 hover:bg-red-500/20 hover:text-red-300 transition-colors cursor-pointer"
            >
              <Square className="w-3 h-3" /> Stop
            </button>
            {scan.scanId && (
              <button
                onClick={() => onViewLive(agentType)}
                className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium border border-amber-500/30 bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 hover:text-amber-300 transition-colors cursor-pointer"
              >
                <ScrollText className="w-3 h-3" /> Live Log
              </button>
            )}
          </>
        ) : (
          <>
            <button
              onClick={() => onStart(agentType)}
              className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium border border-blue-500/30 bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 hover:text-blue-300 transition-colors cursor-pointer"
            >
              <Play className="w-3 h-3" /> Run Scan
            </button>
            {scan.scanId && (
              <button
                onClick={() => onViewHistory(scan.scanId, agentType)}
                className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-medium border border-slate-600 bg-slate-700/50 text-slate-300 hover:bg-slate-700 hover:text-slate-100 transition-colors cursor-pointer"
              >
                <ScrollText className="w-3 h-3" /> Last Run
              </button>
            )}
          </>
        )}
      </div>
    </GlowCard>

    {/* Modals — rendered outside GlowCard so panel state survives menu close */}
    {panel === 'results' && (
      <LastRunModal agentName={agentName} onClose={() => setPanel(null)} />
    )}
    {panel === 'history' && (
      <HistoryModal agentName={agentName} onClose={() => setPanel(null)} />
    )}
    {panel === 'details' && (
      <DetailsModal agent={agent} agentLabel={agentLabel} onClose={() => setPanel(null)} />
    )}
    </>
  )
}

// ── AgentCardGrid ────────────────────────────────────────────────────────────

// ── Scan Modal ────────────────────────────────────────────────────────────────

function ScanModal({ agentType, inventoryStatus, onStart, onClose }) {
  const hasInventory = inventoryStatus?.exists
  const [mode, setMode] = useState(hasInventory ? 'existing' : 'refresh')
  const stale = inventoryStatus?.stale
  const ageHours = inventoryStatus?.age_hours
  const resourceCount = inventoryStatus?.resource_count || 0
  const meta = AGENT_META[agentType] || {}

  function ageText() {
    if (!ageHours) return ''
    if (ageHours < 1) return `${Math.round(ageHours * 60)}m ago`
    return `${Math.round(ageHours)}h ago`
  }

  return (
    <>
      <div className="fixed inset-0 bg-black/50 z-30" onClick={onClose} aria-hidden="true" />
      <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
        <div className="bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl w-full max-w-md">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-700">
            <h3 className="text-sm font-semibold text-slate-200">
              Start Scan — {meta.label || agentType}
            </h3>
            <button onClick={onClose} className="text-slate-500 hover:text-slate-200 transition-colors text-lg leading-none">✕</button>
          </div>
          <div className="px-5 py-4 space-y-4">
            <div>
              <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Resource Inventory</div>
              <div className="rounded-lg overflow-hidden" style={{ border: '1px solid rgba(30,45,74,0.6)' }}>
                <label className={`flex items-start gap-3 p-3 cursor-pointer transition-colors ${!hasInventory ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-800/40'} ${mode === 'existing' ? 'bg-blue-500/06' : ''}`}
                  style={mode === 'existing' ? { background: 'rgba(59,130,246,0.05)' } : {}}>
                  <input type="radio" name={`inv-${agentType}`} value="existing" checked={mode === 'existing'}
                    disabled={!hasInventory} onChange={() => setMode('existing')} className="mt-0.5" />
                  <div>
                    <div className="text-sm font-medium text-slate-200">Use existing inventory</div>
                    {hasInventory
                      ? <div className="text-xs text-slate-500 mt-0.5">Last updated: {ageText()} · {resourceCount} resources{stale && <span className="ml-2 text-amber-400">⚠ stale</span>}</div>
                      : <div className="text-xs text-slate-600 mt-0.5">No inventory found</div>}
                  </div>
                </label>
                <div style={{ borderTop: '1px solid rgba(30,45,74,0.4)' }} />
                <label className={`flex items-start gap-3 p-3 cursor-pointer transition-colors hover:bg-slate-800/40 ${mode === 'refresh' ? 'bg-blue-500/06' : ''}`}
                  style={mode === 'refresh' ? { background: 'rgba(59,130,246,0.05)' } : {}}>
                  <input type="radio" name={`inv-${agentType}`} value="refresh" checked={mode === 'refresh'} onChange={() => setMode('refresh')} className="mt-0.5" />
                  <div>
                    <div className="text-sm font-medium text-slate-200">Refresh inventory first</div>
                    <div className="text-xs text-slate-500 mt-0.5">Fetches latest from Azure (~10–30s)</div>
                  </div>
                </label>
                <div style={{ borderTop: '1px solid rgba(30,45,74,0.4)' }} />
                <label className={`flex items-start gap-3 p-3 cursor-pointer transition-colors hover:bg-slate-800/40 ${mode === 'skip' ? 'bg-blue-500/06' : ''}`}
                  style={mode === 'skip' ? { background: 'rgba(59,130,246,0.05)' } : {}}>
                  <input type="radio" name={`inv-${agentType}`} value="skip" checked={mode === 'skip'} onChange={() => setMode('skip')} className="mt-0.5" />
                  <div>
                    <div className="text-sm font-medium text-slate-200">Skip inventory</div>
                    <div className="text-xs text-slate-500 mt-0.5">Agent discovers resources on its own
                      <span className="text-amber-500/70"> (may produce inconsistent results)</span>
                    </div>
                  </div>
                </label>
              </div>
            </div>
            {stale && mode === 'existing' && hasInventory && (
              <div className="flex items-center gap-2 px-3 py-2 rounded-lg text-xs"
                style={{ background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.2)', color: '#fcd34d' }}>
                <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                Inventory is {ageHours ? `${Math.round(ageHours)}h` : ''} old — consider refreshing for accurate results.
              </div>
            )}
            <div className="flex gap-3">
              <button onClick={onClose}
                className="flex-1 py-2 rounded-lg text-sm text-slate-400 border border-slate-700 hover:bg-slate-800 transition-colors">
                Cancel
              </button>
              <button onClick={() => onStart(mode)}
                className="flex-1 py-2 rounded-lg text-sm font-medium text-white transition-colors"
                style={{ background: 'rgba(59,130,246,0.8)', border: '1px solid rgba(59,130,246,0.5)' }}>
                Start Scan
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

export default function AgentCardGrid({
  agents,
  scanState,
  anyScanning,
  allScanning,
  onStartScan,
  onStartAll,
  onStopScan,
  onStopAll,
  onOpenLiveLog,
  onOpenHistoricalLog,
  resourceGroup,
  onResourceGroupChange,
  inventoryStatus,
  subscriptionId,
  onSubscriptionIdChange,
}) {
  const [modalAgentType, setModalAgentType] = useState(null)

  const agentMap = {}
  agents.forEach(a => { agentMap[a.name] = a })

  const agentTypes = ['cost', 'monitoring', 'deploy']
  const agentNameMap = {
    cost:       'cost-optimization-agent',
    monitoring: 'monitoring-agent',
    deploy:     'deploy-agent',
  }

  const handleStartWithModal = useCallback((agentType) => {
    setModalAgentType(agentType)
  }, [])

  const handleModalStart = useCallback((mode) => {
    const type = modalAgentType
    setModalAgentType(null)
    onStartScan(type, mode)
  }, [modalAgentType, onStartScan])

  return (
    <div className="space-y-4">
      {/* Section header */}
      <div className="flex items-center gap-3">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
          Connected Agents
        </h2>
        {agents.length > 0 && (
          <span className="text-xs bg-blue-500/20 text-blue-400 border border-blue-500/30 px-2 py-0.5 rounded-full">
            {agents.length} registered
          </span>
        )}
      </div>

      {/* Subscription ID input */}
      {onSubscriptionIdChange && (
        <div>
          <label className="block text-xs text-slate-500 mb-1" htmlFor="sub-input">
            Target Subscription <span className="text-slate-600">(defaults to configured subscription)</span>
          </label>
          <input
            id="sub-input"
            type="text"
            value={subscriptionId || ''}
            onChange={e => onSubscriptionIdChange(e.target.value)}
            placeholder="e.g. e7e0ed80-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors font-mono"
          />
        </div>
      )}

      {/* Resource group input */}
      <div>
        <label className="block text-xs text-slate-500 mb-1" htmlFor="rg-input">
          Resource Group <span className="text-slate-600">(optional)</span>
        </label>
        <input
          id="rg-input"
          type="text"
          value={resourceGroup}
          onChange={e => onResourceGroupChange(e.target.value)}
          placeholder="e.g. ruriskry-prod-rg (leave empty to scan whole subscription)"
          className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-blue-500 transition-colors font-mono"
        />
      </div>

      {/* Cards — always show all 3 regardless of registration status */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {agentTypes.map(type => {
          // Use registered agent data if available; fall back to a placeholder so
          // the card always renders. The placeholder shows "never run" state and
          // lets the user trigger a scan directly without having to run it first.
          const agent = agentMap[agentNameMap[type]] ?? {
            name: agentNameMap[type],
            last_seen: null,
            total_actions_proposed: 0,
            approval_count: 0,
            escalation_count: 0,
            denial_count: 0,
          }
          return (
            <AgentCard
              key={type}
              agent={agent}
              agentType={type}
              scan={scanState[type]}
              onStart={handleStartWithModal}
              onStop={onStopScan}
              onViewLive={onOpenLiveLog}
              onViewHistory={onOpenHistoricalLog}
            />
          )
        })}
      </div>

      {/* Run All / Stop All */}
      {anyScanning ? (
        <div className="flex gap-2">
          <div className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-semibold border border-slate-600 bg-slate-700/40 text-slate-400">
            <Spinner />
            {allScanning ? 'Running all agents…' : 'Scan in progress…'}
          </div>
          {/* Only show Stop All when there are actually running (not just stopping) scans */}
          {agentTypes.some(t => scanState[t]?.status === 'running') && (
            <button
              onClick={onStopAll}
              className="flex items-center gap-1.5 px-4 py-2.5 rounded-xl text-sm font-semibold border border-rose-500/50 bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 hover:text-rose-300 transition-all cursor-pointer"
              title="Stop all running scans"
            >
              <Square className="w-3.5 h-3.5" /> Stop All
            </button>
          )}
        </div>
      ) : (
        <button
          onClick={() => onStartAll('existing')}
          className="w-full py-2.5 rounded-xl text-sm font-semibold transition-all border border-blue-500/60 bg-blue-600/20 hover:bg-blue-600/30 text-blue-300 hover:text-blue-200 cursor-pointer"
        >
          <span className="flex items-center justify-center gap-2">
            <Zap className="w-3.5 h-3.5" /> Run All Agents
          </span>
        </button>
      )}

      {/* Scan modal */}
      {modalAgentType && (
        <ScanModal
          agentType={modalAgentType}
          inventoryStatus={inventoryStatus}
          onStart={handleModalStart}
          onClose={() => setModalAgentType(null)}
        />
      )}
    </div>
  )
}
