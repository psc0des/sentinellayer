/**
 * ConnectedAgents.jsx — grid of cards for every A2A agent connected to RuriSkry.
 *
 * Each card shows:
 *   - Agent name + a green pulsing dot (online) or grey dot (offline)
 *   - Total actions proposed (large number)
 *   - Mini coloured bar chart: green = approved, yellow = escalated, red = denied
 *   - Last seen timestamp
 *   - ⋮ 3-dot menu with: Start Scan, Stop Scan, Last Run Results,
 *       History, View Live Log, Agent Details
 *
 * This component owns the scan state for menu-triggered scans (separate from
 * the AgentControls panel which manages its own independent scan sessions).
 */

import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelScan,
  fetchAgentHistory,
  fetchAgentLastRun,
  fetchScanStatus,
  triggerScan,
} from '../api'
import LiveLogPanel from './LiveLogPanel'
import { Play, Square, ClipboardList, BarChart2, ScrollText, Info } from 'lucide-react'
import GlowCard from './magicui/GlowCard'
import VerdictBadge from './magicui/VerdictBadge'

// ── Constants ──────────────────────────────────────────────────────────────

/** Map A2A agent name → scan endpoint type */
const AGENT_TYPE = {
  'cost-optimization-agent': 'cost',
  'monitoring-agent': 'monitoring',
  'deploy-agent': 'deploy',
}

// ── Helpers ────────────────────────────────────────────────────────────────

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
  } catch {
    return iso
  }
}

// verdictBadge class helper kept for inline uses that haven't migrated yet
function verdictBadge(decision) {
  const d = decision?.toLowerCase()
  if (d === 'approved') return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
  if (d === 'escalated') return 'bg-amber-500/15 text-amber-300 border-amber-500/30'
  if (d === 'denied') return 'bg-rose-500/15 text-rose-300 border-rose-500/30'
  return 'bg-slate-700 text-slate-400 border-slate-600'
}

// ── 3-dot dropdown menu ────────────────────────────────────────────────────

function DropdownMenu({ scanning, hasScanId, onAction }) {
  const items = [
    { key: 'scan',    Icon: Play,          label: 'Start Scan',       disabled: scanning },
    { key: 'cancel',  Icon: Square,        label: 'Stop Scan',        disabled: !scanning },
    { key: 'results', Icon: ClipboardList, label: 'Last Run Results', disabled: false },
    { key: 'history', Icon: BarChart2,     label: 'History',          disabled: false },
    { key: 'log',     Icon: ScrollText,    label: 'View Live Log',    disabled: !hasScanId },
    { key: 'details', Icon: Info,          label: 'Agent Details',    disabled: false },
  ]

  return (
    <div
      className="absolute top-8 right-0 z-50 bg-slate-900 border border-slate-700 rounded-lg shadow-2xl py-1 w-48"
      onClick={e => e.stopPropagation()}
    >
      {items.map(({ key, Icon, label, disabled }) => (
        <button
          key={key}
          onClick={() => !disabled && onAction(key)}
          disabled={disabled}
          className={`
            flex items-center gap-2 w-full px-3 py-2 text-xs text-left transition-colors
            ${disabled
              ? 'text-slate-600 cursor-not-allowed'
              : 'text-slate-300 hover:bg-slate-800 hover:text-slate-100 cursor-pointer'
            }
          `}
        >
          <Icon className="w-3.5 h-3.5 shrink-0" />
          <span>{label}</span>
        </button>
      ))}
    </div>
  )
}

// ── Reusable Modal wrapper ─────────────────────────────────────────────────

function Modal({ title, children, onClose }) {
  return (
    <>
      <div
        className="fixed inset-0 bg-black/50 z-30"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
        <div className="bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-700 shrink-0">
            <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
            <button
              onClick={onClose}
              className="text-slate-500 hover:text-slate-200 transition-colors text-lg leading-none"
            >
              ✕
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

// ── Panel: Last Run Results ────────────────────────────────────────────────

function LastRunPanel({ agentName, data, onClose }) {
  const evaluations   = data?.evaluations ?? []
  const proposedCount = data?.proposals_count ?? data?.proposed_actions?.length ?? 0
  const isCleanScan   = evaluations.length === 0 && data?.status === 'complete'

  return (
    <Modal title={`Last Run — ${agentName}`} onClose={onClose}>
      {!data ? (
        <p className="text-slate-500 text-sm text-center py-6">
          No results found. Run a scan first.
        </p>
      ) : isCleanScan ? (
        <div className="text-center py-6 space-y-2">
          <div className="text-3xl">✅</div>
          <p className="text-sm font-medium text-green-400">Scan completed — no issues found</p>
          <p className="text-xs text-slate-500 space-y-0.5">
            <span className="block">{formatTime(data.completed_at ?? data.started_at)}</span>
            {data.scan_id && (
              <span className="block font-mono text-slate-600">
                scan {data.scan_id.slice(0, 8)}…
              </span>
            )}
            <span className="block">{proposedCount} proposal(s) checked · 0 issues</span>
          </p>
        </div>
      ) : evaluations.length === 0 ? (
        <p className="text-slate-500 text-sm text-center py-6">
          No results found. Run a scan first.
        </p>
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-slate-500 mb-3 space-y-1">
            <span className="block">
              Last run: {formatTime(data?.completed_at ?? data?.started_at)}
            </span>
            <span className="block">
              {proposedCount} action(s) proposed · {evaluations.length} verdict(s)
            </span>
            {data?.scan_id && (
              <span className="ml-0 block font-mono text-slate-600">
                scan {data.scan_id.slice(0, 8)}…
              </span>
            )}
          </p>
          {evaluations.map((ev, i) => (
            <div
              key={i}
              className="bg-slate-800 rounded-lg p-3 border border-slate-700 flex items-start justify-between gap-3"
            >
              <div className="min-w-0">
                <p className="text-sm font-mono text-slate-200 truncate">
                  {(ev.resource_id ?? '').split('/').filter(Boolean).pop() ?? ev.resource_id}
                </p>
                <p className="text-xs text-slate-500 mt-0.5">
                  {ev.action_type?.replace(/_/g, ' ')}
                </p>
                <p className="text-xs text-slate-600 mt-1 line-clamp-2">
                  {ev.reason || ev.verdict_reason || ev.action_reason || 'No reasoning provided.'}
                </p>
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
          ))}
        </div>
      )}
    </Modal>
  )
}

// ── Panel: History ─────────────────────────────────────────────────────────

function HistoryPanel({ agentName, data, onClose }) {
  const history = data?.history ?? []

  return (
    <Modal title={`History — ${agentName}`} onClose={onClose}>
      {history.length === 0 ? (
        <p className="text-slate-500 text-sm text-center py-6">No history records found.</p>
      ) : (
        <div className="space-y-2">
          {history.map((rec, i) => (
            <div
              key={i}
              className="bg-slate-800 rounded-lg p-3 border border-slate-700 flex items-start justify-between gap-3"
            >
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

// ── Panel: Agent Details ───────────────────────────────────────────────────

function DetailsPanel({ agent, onClose }) {
  const rows = [
    { label: 'Name', value: agent.name },
    { label: 'Registered', value: formatTime(agent.registered_at) },
    { label: 'Last seen', value: formatTime(agent.last_seen) },
    { label: 'Total proposed', value: agent.total_actions_proposed ?? 0 },
    { label: 'Approved', value: agent.approval_count ?? 0 },
    { label: 'Escalated', value: agent.escalation_count ?? 0 },
    { label: 'Denied', value: agent.denial_count ?? 0 },
    { label: 'Agent card URL', value: agent.agent_card_url ?? '—' },
  ]

  return (
    <Modal title={`Agent Details — ${agent.name}`} onClose={onClose}>
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

// ── AgentCard ──────────────────────────────────────────────────────────────

function AgentCard({ agent, menuOpen, onMenuToggle, onMenuAction, scanning, hasScanId }) {
  const online = isOnline(agent.last_seen)
  const total = agent.total_actions_proposed ?? 0
  const approved = agent.approval_count ?? 0
  const denied = agent.denial_count ?? 0
  const escalated = agent.escalation_count ?? 0

  const pct = (n) => (total > 0 ? (n / total) * 100 : 0)

  return (
    <GlowCard
      color={scanning ? 'amber' : online ? 'green' : 'slate'}
      intensity={online ? 'medium' : 'low'}
      beam={online || scanning}
      beamDuration={scanning ? 1.5 : 4}
      className="p-4 flex flex-col gap-3 relative"
    >

      {/* ── Name + status dot + 3-dot menu ── */}
      <div className="flex items-center gap-2">
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${online ? 'bg-green-500 animate-pulse' : 'bg-slate-600'
            }`}
          title={online ? 'Online (seen < 5 min ago)' : 'Offline'}
        />
        <span
          className="text-sm font-medium text-slate-200 truncate flex-1"
          title={agent.name}
        >
          {agent.name}
        </span>

        {/* 3-dot menu button */}
        <div className="relative shrink-0">
          <button
            onClick={e => { e.stopPropagation(); onMenuToggle() }}
            className="w-7 h-7 flex items-center justify-center rounded text-slate-500 hover:text-slate-200 hover:bg-slate-700 transition-colors text-base"
            title="Agent actions"
            aria-label="Agent actions menu"
          >
            ⋮
          </button>
          {menuOpen && (
            <DropdownMenu
              scanning={scanning}
              hasScanId={hasScanId}
              onAction={onMenuAction}
            />
          )}
        </div>
      </div>

      {/* ── Total actions count ── */}
      <div>
        <span className="text-2xl font-bold text-white tabular-nums">{total}</span>
        <span className="text-xs text-slate-500 ml-1.5">actions proposed</span>
      </div>

      {/* ── Mini coloured bar chart ── */}
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

      {/* ── Last seen + scanning indicator ── */}
      <p className="text-xs text-slate-500 mt-auto">
        {scanning ? (
          <span className="text-amber-400 font-mono flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse inline-block" />
            Scanning…
          </span>
        ) : (
          <>
            Last seen:{' '}
            <span className="text-slate-400">{formatTime(agent.last_seen)}</span>
          </>
        )}
      </p>
    </GlowCard>
  )
}

// ── ConnectedAgents (section) ──────────────────────────────────────────────

export default function ConnectedAgents({ agents }) {
  const [menus, setMenus] = useState({})  // { agentName: bool }
  const [scanState, setScan] = useState({})  // { agentName: { scanning, scanId } }
  const [panels, setPanels] = useState({})  // { agentName: panel type string | null }
  const [panelData, setData] = useState({})  // { agentName: fetched data }

  const pollRefs = useRef({})

  // Close all menus when clicking anywhere outside
  useEffect(() => {
    const handleOutsideClick = () => setMenus({})
    document.addEventListener('click', handleOutsideClick)
    return () => document.removeEventListener('click', handleOutsideClick)
  }, [])

  const toggleMenu = useCallback((name) => {
    setMenus(prev => ({ ...prev, [name]: !prev[name] }))
  }, [])

  const closePanel = useCallback((name) => {
    setPanels(prev => ({ ...prev, [name]: null }))
  }, [])

  const startScanPolling = useCallback((scanId, agentName) => {
    if (pollRefs.current[agentName]) clearInterval(pollRefs.current[agentName])
    pollRefs.current[agentName] = setInterval(async () => {
      try {
        const result = await fetchScanStatus(scanId)
        if (result.status !== 'running') {
          clearInterval(pollRefs.current[agentName])
          pollRefs.current[agentName] = null
          setScan(prev => ({ ...prev, [agentName]: { scanning: false, scanId } }))
        }
      } catch { /* keep polling */ }
    }, 2_000)
  }, [])

  const handleMenuAction = useCallback(async (agentName, action) => {
    setMenus({})  // close menu immediately

    const agentType = AGENT_TYPE[agentName]

    switch (action) {
      case 'scan': {
        if (!agentType) return
        setScan(prev => ({ ...prev, [agentName]: { scanning: true, scanId: null } }))
        try {
          const { scan_id } = await triggerScan(agentType, null)
          setScan(prev => ({ ...prev, [agentName]: { scanning: true, scanId: scan_id } }))
          setPanels(prev => ({ ...prev, [agentName]: 'log' }))
          startScanPolling(scan_id, agentName)
        } catch {
          setScan(prev => ({ ...prev, [agentName]: { scanning: false, scanId: null } }))
        }
        break
      }

      case 'cancel': {
        const sid = scanState[agentName]?.scanId
        if (sid) {
          try { await cancelScan(sid) } catch { /* ignore */ }
          setScan(prev => ({ ...prev, [agentName]: { scanning: false, scanId: sid } }))
        }
        break
      }

      case 'results': {
        try {
          const data = await fetchAgentLastRun(agentName)
          setData(prev => ({ ...prev, [agentName]: data }))
          setPanels(prev => ({ ...prev, [agentName]: 'results' }))
        } catch { /* no data */ }
        break
      }

      case 'history': {
        try {
          const data = await fetchAgentHistory(agentName, 20)
          setData(prev => ({ ...prev, [agentName]: data }))
          setPanels(prev => ({ ...prev, [agentName]: 'history' }))
        } catch { /* not registered */ }
        break
      }

      case 'log': {
        const sid = scanState[agentName]?.scanId
        if (sid) {
          setPanels(prev => ({ ...prev, [agentName]: 'log' }))
          break
        }
        try {
          const data = await fetchAgentLastRun(agentName)
          if (data?.scan_id) {
            setData(prev => ({ ...prev, [agentName]: data }))
            setScan(prev => ({ ...prev, [agentName]: { scanning: false, scanId: data.scan_id } }))
            setPanels(prev => ({ ...prev, [agentName]: 'log' }))
          }
        } catch { /* no last run */ }
        break
      }

      case 'details': {
        setPanels(prev => ({ ...prev, [agentName]: 'details' }))
        break
      }
    }
  }, [scanState, startScanPolling])

  return (
    <div>
      {/* Section header */}
      <div className="flex items-center gap-3 mb-3">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
          Connected Agents
        </h2>
        {agents.length > 0 && (
          <span className="text-xs bg-blue-500/20 text-blue-400 border border-blue-500/30 px-2 py-0.5 rounded-full">
            {agents.length} registered
          </span>
        )}
      </div>

      {agents.length === 0 ? (
        <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 text-center">
          <p className="text-sm text-slate-500">No A2A agents connected yet.</p>
          <p className="text-xs text-slate-600 mt-1">
            Run{' '}
            <code className="text-slate-400 bg-slate-900 px-1.5 py-0.5 rounded">
              python demo_a2a.py
            </code>{' '}
            to connect agents.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {[...agents].sort((a, b) => a.name.localeCompare(b.name)).map((agent) => (
            <AgentCard
              key={agent.name}
              agent={agent}
              menuOpen={!!menus[agent.name]}
              onMenuToggle={() => toggleMenu(agent.name)}
              onMenuAction={(action) => handleMenuAction(agent.name, action)}
              scanning={!!scanState[agent.name]?.scanning}
              hasScanId={!!scanState[agent.name]?.scanId}
            />
          ))}
        </div>
      )}

      {/* ── Per-agent panels ── */}
      {agents.map((agent) => {
        const panel = panels[agent.name]
        const data = panelData[agent.name]
        const scanId = scanState[agent.name]?.scanId
        const agentType = AGENT_TYPE[agent.name]

        if (!panel) return null

        if (panel === 'log') {
          return (
            <LiveLogPanel
              key={`log-${agent.name}`}
              scanId={scanId}
              agentType={agentType}
              isOpen={true}
              onClose={() => closePanel(agent.name)}
            />
          )
        }
        if (panel === 'results') {
          return (
            <LastRunPanel
              key={`results-${agent.name}`}
              agentName={agent.name}
              data={data}
              onClose={() => closePanel(agent.name)}
            />
          )
        }
        if (panel === 'history') {
          return (
            <HistoryPanel
              key={`history-${agent.name}`}
              agentName={agent.name}
              data={data}
              onClose={() => closePanel(agent.name)}
            />
          )
        }
        if (panel === 'details') {
          return (
            <DetailsPanel
              key={`details-${agent.name}`}
              agent={agent}
              onClose={() => closePanel(agent.name)}
            />
          )
        }
        return null
      })}
    </div>
  )
}
