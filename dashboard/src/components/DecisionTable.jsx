/**
 * DecisionTable — scrollable table of recent governance decisions.
 *
 * Columns: Resource, Action Type, SRI Composite, Verdict badge, Timestamp.
 * Clicking a row selects it (highlights it and opens EvaluationDetail).
 * Clicking the same row again deselects it.
 */

import React from 'react'

// ── Helpers ───────────────────────────────────────────────────────────────

function sriColor(score) {
  if (score <= 25) return 'text-green-400'
  if (score <= 60) return 'text-yellow-400'
  return 'text-red-400'
}

function VerdictBadge({ verdict }) {
  const styles = {
    approved: 'bg-green-500/15 text-green-400 border border-green-500/30',
    escalated: 'bg-yellow-500/15 text-yellow-400 border border-yellow-500/30',
    denied:    'bg-red-500/15 text-red-400 border border-red-500/30',
  }
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-bold uppercase tracking-wide ${styles[verdict] ?? styles.escalated}`}>
      {verdict}
    </span>
  )
}

function shortResource(resourceId) {
  // Return the last segment of an Azure resource path
  return resourceId?.split('/').filter(Boolean).pop() ?? resourceId
}

function formatTime(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

// ── Component ─────────────────────────────────────────────────────────────

export default function DecisionTable({ evaluations, selected, onSelect }) {
  if (!evaluations.length) {
    return (
      <div className="flex flex-col items-center justify-center h-48 text-slate-500 gap-2">
        <span className="text-3xl">📭</span>
        <p className="text-sm">No evaluations yet.</p>
        <p className="text-xs">Run the FastAPI server and generate some decisions.</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Table header */}
      <div className="px-5 py-4 border-b border-slate-700">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
          Recent Decisions
        </h2>
      </div>

      {/* Scrollable table body */}
      <div className="overflow-auto flex-1 max-h-[380px]">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-slate-800 z-10">
            <tr className="border-b border-slate-700">
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-400 uppercase tracking-wider">Resource</th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-400 uppercase tracking-wider hidden sm:table-cell">Action</th>
              <th className="text-right px-5 py-3 text-xs font-semibold text-slate-400 uppercase tracking-wider">SRI</th>
              <th className="text-center px-5 py-3 text-xs font-semibold text-slate-400 uppercase tracking-wider">Verdict</th>
              <th className="text-right px-5 py-3 text-xs font-semibold text-slate-400 uppercase tracking-wider hidden md:table-cell">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700/40">
            {evaluations.map(ev => {
              const isSelected = selected?.action_id === ev.action_id
              return (
                <tr
                  key={ev.action_id}
                  onClick={() => onSelect(isSelected ? null : ev)}
                  className={`cursor-pointer transition-colors duration-100 ${
                    isSelected
                      ? 'bg-blue-500/10 border-l-2 border-l-blue-500'
                      : 'hover:bg-slate-700/40'
                  }`}
                >
                  {/* Resource */}
                  <td className="px-5 py-3">
                    <span className="font-mono text-xs text-slate-200">
                      {shortResource(ev.resource_id)}
                    </span>
                  </td>

                  {/* Action type */}
                  <td className="px-5 py-3 text-slate-400 hidden sm:table-cell">
                    {ev.action_type?.replace(/_/g, ' ')}
                  </td>

                  {/* SRI score */}
                  <td className={`px-5 py-3 text-right font-bold tabular-nums ${sriColor(ev.sri_composite)}`}>
                    {ev.sri_composite?.toFixed(1)}
                  </td>

                  {/* Verdict badge */}
                  <td className="px-5 py-3 text-center">
                    <VerdictBadge verdict={ev.decision} />
                  </td>

                  {/* Timestamp */}
                  <td className="px-5 py-3 text-right text-xs text-slate-500 tabular-nums hidden md:table-cell">
                    {formatTime(ev.timestamp)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
