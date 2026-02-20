/**
 * MetricsBar — a row of four summary stat cards at the top of the dashboard.
 *
 * Displays: total evaluations, approval rate, denial rate, average SRI score.
 */

import React from 'react'

function sriColor(avg) {
  if (avg == null) return 'text-slate-400'
  if (avg <= 25)   return 'text-green-400'
  if (avg <= 60)   return 'text-yellow-400'
  return 'text-red-400'
}

function StatCard({ label, value, sub, valueClass }) {
  return (
    <div className="bg-slate-800 rounded-xl border border-slate-700 p-5 flex flex-col gap-1">
      <p className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
        {label}
      </p>
      <p className={`text-3xl font-bold tabular-nums ${valueClass}`}>{value}</p>
      {sub && <p className="text-xs text-slate-500">{sub}</p>}
    </div>
  )
}

export default function MetricsBar({ metrics }) {
  const {
    total_evaluations,
    decisions,
    decision_percentages,
    sri_composite,
  } = metrics

  const avgSRI = sri_composite?.avg

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        label="Total Evaluations"
        value={total_evaluations}
        sub="governance decisions"
        valueClass="text-blue-400"
      />
      <StatCard
        label="Approval Rate"
        value={`${decision_percentages?.approved ?? 0}%`}
        sub={`${decisions?.approved ?? 0} approved · ${decisions?.escalated ?? 0} escalated`}
        valueClass="text-green-400"
      />
      <StatCard
        label="Denial Rate"
        value={`${decision_percentages?.denied ?? 0}%`}
        sub={`${decisions?.denied ?? 0} blocked`}
        valueClass="text-red-400"
      />
      <StatCard
        label="Avg SRI Score"
        value={avgSRI != null ? avgSRI.toFixed(1) : '—'}
        sub={`max ${sri_composite?.max?.toFixed(1) ?? '—'} · min ${sri_composite?.min?.toFixed(1) ?? '—'}`}
        valueClass={sriColor(avgSRI)}
      />
    </div>
  )
}
