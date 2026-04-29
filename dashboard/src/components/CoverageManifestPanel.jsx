/**
 * CoverageManifestPanel — rendered inside the scan log viewer next to proposal/verdict counts.
 *
 * Shows per-category rule match counts and a collapsible list of resource types
 * that had no rules applied. Renders nothing gracefully when coverage_manifest is absent
 * (old scan records from before Phase 40).
 */

import React, { useState } from 'react'
import { ChevronDown, ChevronRight, ShieldCheck, Activity } from 'lucide-react'

const CATEGORY_COLORS = {
  security: 'text-red-400 bg-red-400/10 border-red-400/20',
  cost: 'text-emerald-400 bg-emerald-400/10 border-emerald-400/20',
  reliability: 'text-blue-400 bg-blue-400/10 border-blue-400/20',
  hygiene: 'text-purple-400 bg-purple-400/10 border-purple-400/20',
}

function CategoryBadge({ name, applied, matched }) {
  const colors = CATEGORY_COLORS[name] || 'text-slate-400 bg-slate-400/10 border-slate-400/20'
  return (
    <div className={`rounded-lg border px-3 py-2 ${colors}`}>
      <p className="text-xs font-semibold capitalize">{name}</p>
      <p className="text-lg font-bold mt-0.5">{matched}</p>
      <p className="text-xs opacity-60">of {applied} rules matched</p>
    </div>
  )
}

export default function CoverageManifestPanel({ manifest }) {
  const [uncoveredOpen, setUncoveredOpen] = useState(false)

  if (!manifest || !manifest.rules_applied) return null

  const {
    rules_applied = 0,
    rules_matched = 0,
    categories = {},
    types_in_inventory = 0,
    types_with_at_least_one_rule = 0,
    types_uncovered = [],
  } = manifest

  return (
    <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Activity className="w-4 h-4 text-slate-400" />
        <h3 className="text-sm font-semibold text-slate-200">Coverage Manifest</h3>
      </div>

      {/* Summary row */}
      <div className="flex items-center gap-4 text-xs text-slate-400">
        <span>
          <span className="font-semibold text-slate-200">{rules_matched}</span> / {rules_applied} rules matched
        </span>
        <span className="text-slate-600">·</span>
        <span>
          <span className="font-semibold text-slate-200">{types_with_at_least_one_rule}</span> / {types_in_inventory} resource types covered
        </span>
      </div>

      {/* Per-category grid */}
      {Object.keys(categories).length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {Object.entries(categories).map(([cat, counts]) => (
            <CategoryBadge
              key={cat}
              name={cat}
              applied={counts.applied}
              matched={counts.matched}
            />
          ))}
        </div>
      )}

      {/* Uncovered types */}
      {types_uncovered.length > 0 && (
        <div>
          <button
            onClick={() => setUncoveredOpen(o => !o)}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200"
          >
            {uncoveredOpen
              ? <ChevronDown className="w-3.5 h-3.5" />
              : <ChevronRight className="w-3.5 h-3.5" />}
            {types_uncovered.length} resource type{types_uncovered.length > 1 ? 's' : ''} without rules
          </button>
          {uncoveredOpen && (
            <ul className="mt-2 space-y-0.5 ml-5 max-h-40 overflow-y-auto">
              {types_uncovered.map(t => (
                <li key={t} className="text-xs font-mono text-slate-500">{t}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {types_uncovered.length === 0 && types_with_at_least_one_rule >= types_in_inventory && (
        <div className="flex items-center gap-2 text-xs text-emerald-400">
          <ShieldCheck className="w-3.5 h-3.5" />
          All resource types in this inventory have at least one rule
        </div>
      )}
    </div>
  )
}
