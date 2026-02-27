/**
 * ConnectedAgents.jsx — grid of cards for every A2A agent connected to SentinelLayer.
 *
 * Each card shows:
 *   - Agent name + a green pulsing dot (online) or grey dot (offline)
 *     "Online" means the agent was last seen less than 5 minutes ago.
 *   - Total actions proposed (large number)
 *   - Mini coloured bar chart: green = approved, yellow = escalated, red = denied
 *   - Last seen timestamp
 *
 * This component is purely presentational — it receives data as props and does
 * no fetching itself.  The parent (App.jsx) fetches and auto-refreshes the data.
 */

import React from 'react'

// ── Helpers ────────────────────────────────────────────────────────────────

/** Returns true if the ISO timestamp is within the last `minutes` minutes. */
function isOnline(lastSeen, minutes = 5) {
  if (!lastSeen) return false
  return Date.now() - new Date(lastSeen).getTime() < minutes * 60_000
}

/** Formats an ISO timestamp to a short human-readable string. */
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

// ── AgentCard ──────────────────────────────────────────────────────────────

/**
 * One agent card.
 *
 * Props:
 *   agent — object from GET /api/agents with fields:
 *     name, last_seen, total_actions_proposed,
 *     approval_count, denial_count, escalation_count
 */
function AgentCard({ agent }) {
  const online    = isOnline(agent.last_seen)
  const total     = agent.total_actions_proposed ?? 0
  const approved  = agent.approval_count ?? 0
  const denied    = agent.denial_count ?? 0
  const escalated = agent.escalation_count ?? 0

  // Percentage of the bar each segment occupies
  const pct = (n) => (total > 0 ? (n / total) * 100 : 0)

  return (
    <div className="bg-slate-800 rounded-xl border border-slate-700 p-4 flex flex-col gap-3">

      {/* ── Name + online/offline status dot ── */}
      <div className="flex items-center gap-2">
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${
            online ? 'bg-green-500 animate-pulse' : 'bg-slate-600'
          }`}
          title={online ? 'Online (seen < 5 min ago)' : 'Offline'}
        />
        <span
          className="text-sm font-medium text-slate-200 truncate"
          title={agent.name}
        >
          {agent.name}
        </span>
      </div>

      {/* ── Total actions count ── */}
      <div>
        <span className="text-2xl font-bold text-white tabular-nums">{total}</span>
        <span className="text-xs text-slate-500 ml-1.5">actions proposed</span>
      </div>

      {/* ── Mini coloured bar chart ── */}
      <div className="space-y-1.5">
        {/* The bar itself */}
        <div className="flex h-2 rounded-full overflow-hidden bg-slate-700">
          {total === 0 ? (
            // No data yet — show a plain grey bar
            <div className="w-full bg-slate-600 rounded-full" />
          ) : (
            <>
              {approved  > 0 && <div className="bg-green-500"  style={{ width: `${pct(approved)}%` }}  />}
              {escalated > 0 && <div className="bg-yellow-500" style={{ width: `${pct(escalated)}%` }} />}
              {denied    > 0 && <div className="bg-red-500"    style={{ width: `${pct(denied)}%` }}    />}
            </>
          )}
        </div>

        {/* Counts below the bar */}
        <div className="flex justify-between text-xs">
          <span className="text-green-400">{approved} appr.</span>
          <span className="text-yellow-400">{escalated} esc.</span>
          <span className="text-red-400">{denied} denied</span>
        </div>
      </div>

      {/* ── Last seen ── */}
      <p className="text-xs text-slate-500 mt-auto">
        Last seen:{' '}
        <span className="text-slate-400">{formatTime(agent.last_seen)}</span>
      </p>
    </div>
  )
}

// ── ConnectedAgents (section) ──────────────────────────────────────────────

/**
 * The full "Connected Agents" section.
 *
 * Props:
 *   agents — array of agent objects from GET /api/agents
 */
export default function ConnectedAgents({ agents }) {
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
        /* Empty state */
        <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 text-center">
          <p className="text-sm text-slate-500">
            No A2A agents connected yet.
          </p>
          <p className="text-xs text-slate-600 mt-1">
            Run{' '}
            <code className="text-slate-400 bg-slate-900 px-1.5 py-0.5 rounded">
              python demo_a2a.py
            </code>{' '}
            to connect agents.
          </p>
        </div>
      ) : (
        /* Responsive card grid: 1 col → 2 col → 3 col */
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {agents.map((agent) => (
            <AgentCard key={agent.name} agent={agent} />
          ))}
        </div>
      )}
    </div>
  )
}
