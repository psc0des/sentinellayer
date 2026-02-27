/**
 * LiveActivityFeed.jsx — a scrollable real-time feed of governance evaluations.
 *
 * Each row shows:
 *   agent → action type → resource → SRI score → verdict badge → "X min ago"
 *
 * The feed is sorted newest-first (the API already returns it this way).
 * Auto-refresh happens in the parent (App.jsx) via setInterval every 5 seconds —
 * this component just re-renders when it receives updated props.
 *
 * Data comes from GET /api/evaluations (same endpoint as DecisionTable) so
 * no extra network calls are needed here.
 */

import React from 'react'

// ── Helpers ────────────────────────────────────────────────────────────────

/** Tailwind classes for each verdict type. */
const VERDICT_STYLES = {
  approved:  { text: 'text-green-400',  pill: 'bg-green-500/10 border-green-500/30' },
  escalated: { text: 'text-yellow-400', pill: 'bg-yellow-500/10 border-yellow-500/30' },
  denied:    { text: 'text-red-400',    pill: 'bg-red-500/10 border-red-500/30' },
}

/** Map an SRI composite score to a Tailwind text-colour class. */
function sriColor(score) {
  if (score <= 25) return 'text-green-400'
  if (score <= 60) return 'text-yellow-400'
  return 'text-red-400'
}

/** Return the last path segment of an Azure resource ID (or the whole string). */
function shortResource(id) {
  if (!id) return '—'
  return id.split('/').filter(Boolean).pop() ?? id
}

/**
 * Return a human-friendly relative time string.
 * Examples: "just now", "45s ago", "3m ago", "2h ago"
 */
function relativeTime(iso) {
  if (!iso) return ''
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 10)  return 'just now'
  if (s < 60)  return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60)  return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

// ── Sub-components ─────────────────────────────────────────────────────────

/** Small coloured pill showing the decision verdict. */
function VerdictBadge({ verdict }) {
  const v = (verdict ?? 'unknown').toLowerCase()
  const s = VERDICT_STYLES[v] ?? { text: 'text-slate-400', pill: 'bg-slate-700/50 border-slate-600' }
  return (
    <span
      className={`shrink-0 text-xs font-bold px-2 py-0.5 rounded-full border ${s.text} ${s.pill}`}
    >
      {v.toUpperCase()}
    </span>
  )
}

/**
 * One row in the feed.
 *
 * Props:
 *   ev — one evaluation record with fields:
 *     action_id, agent_id, action_type, resource_id, sri_composite,
 *     decision, timestamp
 */
function FeedRow({ ev }) {
  // Shorten "cost-optimization-agent" → "cost-optimization" to save space
  const agentLabel = (ev.agent_id ?? 'unknown').replace(/-agent$/, '')

  return (
    <div className="flex items-center gap-2 py-2.5 border-b border-slate-700/40 last:border-0 min-w-0 text-xs">

      {/* Agent name — blue, monospace, fixed width */}
      <span
        className="text-blue-400 font-mono shrink-0 w-28 truncate"
        title={ev.agent_id}
      >
        {agentLabel}
      </span>

      <span className="text-slate-600 shrink-0">→</span>

      {/* Action type — e.g. "delete resource" */}
      <span
        className="text-slate-400 shrink-0 w-24 truncate"
        title={ev.action_type}
      >
        {ev.action_type?.replace(/_/g, ' ') ?? '—'}
      </span>

      <span className="text-slate-600 shrink-0">→</span>

      {/* Resource — fills remaining space, truncates with tooltip */}
      <span
        className="text-slate-300 font-mono flex-1 truncate min-w-0"
        title={ev.resource_id}
      >
        {shortResource(ev.resource_id)}
      </span>

      {/* SRI composite score — colour-coded */}
      <span className={`font-bold shrink-0 w-10 text-right tabular-nums ${sriColor(ev.sri_composite ?? 0)}`}>
        {(ev.sri_composite ?? 0).toFixed(1)}
      </span>

      {/* Verdict badge */}
      <VerdictBadge verdict={ev.decision} />

      {/* Relative time — hidden on small screens to save space */}
      <span className="text-slate-600 shrink-0 hidden sm:block w-16 text-right">
        {relativeTime(ev.timestamp)}
      </span>
    </div>
  )
}

// ── LiveActivityFeed (section) ─────────────────────────────────────────────

/**
 * The full "Live Activity Feed" section.
 *
 * Props:
 *   evaluations — array of evaluation records (newest-first) from the parent.
 *                 Same data as DecisionTable; we show the newest 50 here.
 */
export default function LiveActivityFeed({ evaluations }) {
  const items = (evaluations ?? []).slice(0, 50)

  return (
    <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">

      {/* ── Header ── */}
      <div className="flex items-center gap-2 mb-4">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
          Live Activity Feed
        </h2>
        {/* Pulsing green dot to signal live updates */}
        <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
        <span className="text-xs text-slate-600 ml-auto">
          {items.length} events · auto-refresh 5s
        </span>
      </div>

      {items.length === 0 ? (
        /* Empty state */
        <div className="text-center py-8">
          <p className="text-sm text-slate-500">No activity yet.</p>
          <p className="text-xs text-slate-600 mt-1">
            Run{' '}
            <code className="text-slate-400 bg-slate-900 px-1.5 py-0.5 rounded">
              python demo_a2a.py
            </code>{' '}
            to generate events.
          </p>
        </div>
      ) : (
        <>
          {/* Column header row */}
          <div className="flex items-center gap-2 pb-2 border-b border-slate-700 text-xs text-slate-600 mb-1">
            <span className="w-28 shrink-0">Agent</span>
            <span className="w-5  shrink-0" />
            <span className="w-24 shrink-0">Action</span>
            <span className="w-5  shrink-0" />
            <span className="flex-1">Resource</span>
            <span className="w-10 shrink-0 text-right">SRI</span>
            <span className="w-16 shrink-0 ml-2">Verdict</span>
            <span className="w-16 shrink-0 text-right hidden sm:block">When</span>
          </div>

          {/* Scrollable feed — capped at 360 px so it never dominates the page */}
          <div className="overflow-y-auto max-h-72 pr-1">
            {items.map((ev) => (
              <FeedRow key={ev.action_id ?? ev.timestamp} ev={ev} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
