/**
 * OverrideHistoryPanel.jsx — Phase 35C
 *
 * Shows relevant past operator overrides for the current evaluation's action context.
 * Fetched from GET /api/overrides?action_type=X (filtered to same action type).
 *
 * Props:
 *   actionType    — action_type string from the evaluation (e.g. "restart_service")
 *   resourceType  — resource_type string from the evaluation (optional, for context)
 */

import React, { useEffect, useState } from 'react'
import { fetchOverrides } from '../api'

// ── Helpers ────────────────────────────────────────────────────────────────

function formatDate(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  } catch { return iso }
}

const OVERRIDE_LABELS = {
  force_execute:     { label: 'Force Executed',     color: 'text-rose-400',   bg: 'bg-rose-500/10',   border: 'border-rose-500/25' },
  dismiss_approved:  { label: 'Dismissed (Approved)', color: 'text-slate-400', bg: 'bg-slate-500/10',  border: 'border-slate-500/25' },
  dismiss_escalated: { label: 'Dismissed (Escalated)', color: 'text-amber-400', bg: 'bg-amber-500/10', border: 'border-amber-500/25' },
  satisfy_condition: { label: 'Condition Satisfied', color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/25' },
  reverse_denial:    { label: 'Denial Reversed',    color: 'text-purple-400',  bg: 'bg-purple-500/10', border: 'border-purple-500/25' },
}

const VERDICT_COLORS = {
  approved:    'text-green-400',
  approved_if: 'text-amber-300',
  escalated:   'text-yellow-400',
  denied:      'text-red-400',
}

function OverrideBadge({ type }) {
  const cfg = OVERRIDE_LABELS[type] ?? { label: type, color: 'text-slate-400', bg: 'bg-slate-500/10', border: 'border-slate-500/25' }
  return (
    <span className={`inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-full border ${cfg.color} ${cfg.bg} ${cfg.border}`}>
      {cfg.label}
    </span>
  )
}

// ── Main Component ──────────────────────────────────────────────────────────

export default function OverrideHistoryPanel({ actionType, resourceType }) {
  const [overrides, setOverrides] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!actionType) { setLoading(false); return }
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchOverrides({ limit: 5, action_type: actionType })
      .then(data => { if (!cancelled) setOverrides(data.overrides ?? []) })
      .catch(err => { if (!cancelled) setError(err.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [actionType])

  if (loading) {
    return (
      <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-3">
          Override History
        </h2>
        <p className="text-sm text-slate-500 animate-pulse">Loading override history…</p>
      </div>
    )
  }

  return (
    <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">
          Override History
        </h2>
        {overrides.length > 0 && (
          <span className="text-[10px] text-slate-600 font-mono">
            {overrides.length} match{overrides.length !== 1 ? 'es' : ''} for {actionType}
          </span>
        )}
      </div>

      {error ? (
        <p className="text-sm text-red-400">{error}</p>
      ) : overrides.length === 0 ? (
        <div className="text-center py-6">
          <p className="text-sm text-slate-500">No operator overrides yet</p>
          <p className="text-xs text-slate-600 mt-1">
            Override history for <span className="font-mono text-slate-500">{actionType}</span> will appear here
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-xs text-slate-500 leading-relaxed">
            Past operator decisions for similar <span className="font-mono text-slate-400">{actionType}</span> actions.
            These inform the LLM's scoring calibration.
          </p>

          {overrides.map((ov, i) => (
            <div key={ov.override_id ?? i} className="bg-slate-900/50 border border-slate-700/60 rounded-lg p-3 space-y-2">
              {/* Header row */}
              <div className="flex items-center gap-2 flex-wrap">
                <OverrideBadge type={ov.override_type} />
                <span className="text-[10px] text-slate-600">{formatDate(ov.timestamp)}</span>
                {ov.original_verdict && (
                  <span className="ml-auto text-[10px] tabular-nums">
                    <span className="text-slate-600">was </span>
                    <span className={`font-semibold ${VERDICT_COLORS[ov.original_verdict] ?? 'text-slate-400'}`}>
                      {ov.original_verdict.toUpperCase()}
                    </span>
                    {ov.original_sri != null && (
                      <span className="text-slate-600"> · SRI {Number(ov.original_sri).toFixed(1)}</span>
                    )}
                  </span>
                )}
              </div>

              {/* Resource type (if differs from current) */}
              {ov.resource_type && ov.resource_type !== resourceType && (
                <p className="text-[10px] text-slate-600 font-mono">{ov.resource_type.split('/').pop()}</p>
              )}

              {/* Operator reason */}
              <p className="text-xs text-slate-300 leading-relaxed">
                <span className="text-slate-500">Reason: </span>
                {ov.operator_reason}
              </p>

              {/* Context flags */}
              <div className="flex gap-2 flex-wrap">
                {ov.is_production && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">prod</span>
                )}
                {ov.is_critical && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-400 border border-orange-500/20">critical</span>
                )}
                <span className="text-[9px] text-slate-700 font-mono ml-auto">{ov.operator_id}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
