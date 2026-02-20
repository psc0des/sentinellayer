/**
 * EvaluationDetail — expanded panel shown when a table row is clicked.
 *
 * Shows:
 *  - Large verdict badge and SRI composite score
 *  - Horizontal bar chart of the four SRI dimensions (Recharts)
 *  - Action metadata (resource, type, agent, timestamp)
 *  - Policy violations (if any)
 *  - Human-readable verdict reason
 */

import React from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, Cell, ResponsiveContainer,
} from 'recharts'

// ── Helpers ───────────────────────────────────────────────────────────────

function barColor(score) {
  if (score <= 25) return '#22c55e'
  if (score <= 60) return '#eab308'
  return '#ef4444'
}

const VERDICT_STYLES = {
  approved: {
    border: 'border-green-500/30',
    bg:     'bg-green-500/5',
    text:   'text-green-400',
    badge:  'bg-green-500/20 text-green-400 border border-green-500/40',
  },
  escalated: {
    border: 'border-yellow-500/30',
    bg:     'bg-yellow-500/5',
    text:   'text-yellow-400',
    badge:  'bg-yellow-500/20 text-yellow-400 border border-yellow-500/40',
  },
  denied: {
    border: 'border-red-500/30',
    bg:     'bg-red-500/5',
    text:   'text-red-400',
    badge:  'bg-red-500/20 text-red-400 border border-red-500/40',
  },
}

function sriTextColor(score) {
  if (score <= 25) return 'text-green-400'
  if (score <= 60) return 'text-yellow-400'
  return 'text-red-400'
}

function DetailRow({ label, value }) {
  return (
    <div className="flex gap-3 text-sm">
      <dt className="text-slate-500 w-20 shrink-0">{label}</dt>
      <dd className="text-slate-300 font-mono text-xs leading-5 break-all">{value}</dd>
    </div>
  )
}

// Custom Recharts tooltip
function CustomTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const { name, score, weight } = payload[0].payload
  return (
    <div className="bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-xs shadow-lg">
      <p className="font-semibold text-slate-200 mb-1">{name}</p>
      <p className="text-slate-400">Score: <span className="font-bold" style={{ color: barColor(score) }}>{score.toFixed(1)} / 100</span></p>
      <p className="text-slate-400">Weight: {weight}</p>
    </div>
  )
}

// ── Component ─────────────────────────────────────────────────────────────

export default function EvaluationDetail({ evaluation, onClose }) {
  const {
    action_id,
    decision,
    sri_composite,
    sri_breakdown,
    resource_id,
    resource_type,
    action_type,
    agent_id,
    timestamp,
    verdict_reason,
    violations = [],
  } = evaluation

  const vs = VERDICT_STYLES[decision] ?? VERDICT_STYLES.escalated

  // Data for Recharts — four SRI dimensions
  const chartData = [
    { name: 'Infrastructure', score: sri_breakdown?.infrastructure ?? 0, weight: '30%' },
    { name: 'Policy',         score: sri_breakdown?.policy ?? 0,         weight: '25%' },
    { name: 'Historical',     score: sri_breakdown?.historical ?? 0,     weight: '25%' },
    { name: 'Cost',           score: sri_breakdown?.cost ?? 0,           weight: '20%' },
  ]

  const shortResource = resource_id?.split('/').filter(Boolean).pop() ?? resource_id

  return (
    <div className={`rounded-xl border ${vs.border} ${vs.bg} p-6 transition-all`}>

      {/* ── Header ── */}
      <div className="flex items-start justify-between mb-6">
        <div className="flex items-center gap-4 flex-wrap">
          {/* Verdict badge */}
          <span className={`px-4 py-1.5 rounded-full text-sm font-bold uppercase tracking-widest border ${vs.badge}`}>
            {decision}
          </span>

          {/* SRI composite */}
          <span className={`text-2xl font-bold tabular-nums ${sriTextColor(sri_composite)}`}>
            SRI™ {sri_composite?.toFixed(1)}
          </span>

          {/* Action ID */}
          <span className="text-xs text-slate-500 font-mono hidden lg:block">
            {action_id}
          </span>
        </div>

        {/* Close button */}
        <button
          onClick={onClose}
          className="text-slate-500 hover:text-slate-200 transition-colors text-xl leading-none ml-4 shrink-0"
          aria-label="Close detail panel"
        >
          ✕
        </button>
      </div>

      {/* ── Body: two-column grid ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">

        {/* Left — SRI breakdown bar chart */}
        <div>
          <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
            SRI Breakdown
          </h3>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart
              data={chartData}
              layout="vertical"
              margin={{ left: 4, right: 24, top: 2, bottom: 2 }}
            >
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#1e293b"
                horizontal={false}
              />
              <XAxis
                type="number"
                domain={[0, 100]}
                tick={{ fill: '#64748b', fontSize: 10 }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                type="category"
                dataKey="name"
                tick={{ fill: '#94a3b8', fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={88}
              />
              <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
              <Bar dataKey="score" radius={[0, 4, 4, 0]} maxBarSize={20}>
                {chartData.map(entry => (
                  <Cell key={entry.name} fill={barColor(entry.score)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>

          {/* Dimension weight legend */}
          <div className="mt-2 flex gap-4 flex-wrap">
            {chartData.map(d => (
              <div key={d.name} className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full" style={{ background: barColor(d.score) }} />
                <span className="text-xs text-slate-500">{d.name} <span className="text-slate-600">({d.weight})</span></span>
              </div>
            ))}
          </div>
        </div>

        {/* Right — action details + violations + reason */}
        <div className="space-y-5">

          {/* Action details */}
          <div>
            <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-3">
              Action Details
            </h3>
            <dl className="space-y-2">
              <DetailRow label="Resource"  value={shortResource} />
              <DetailRow label="Type"      value={resource_type ?? '—'} />
              <DetailRow label="Action"    value={action_type?.replace(/_/g, ' ') ?? '—'} />
              <DetailRow label="Agent"     value={agent_id ?? '—'} />
              <DetailRow label="Time"      value={timestamp ? new Date(timestamp).toLocaleString() : '—'} />
            </dl>
          </div>

          {/* Policy violations */}
          {violations.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-2">
                Policy Violations
              </h3>
              <div className="flex flex-wrap gap-2">
                {violations.map(v => (
                  <span
                    key={v}
                    className="px-2.5 py-1 rounded-full text-xs font-semibold bg-red-500/15 text-red-400 border border-red-500/30"
                  >
                    {v}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Verdict reason */}
          <div>
            <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-2">
              Reason
            </h3>
            <p className="text-xs text-slate-400 leading-relaxed">
              {verdict_reason ?? '—'}
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
