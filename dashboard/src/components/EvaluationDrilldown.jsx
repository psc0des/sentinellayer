/**
 * EvaluationDrilldown.jsx — full-page drilldown for a governance evaluation.
 *
 * Sections:
 *   1. Verdict Header (badge, SRI, resource info, agent, timestamp)
 *   2. SRI™ Breakdown (4 weighted horizontal bars, primary factor star)
 *   3. Decision Explanation (summary, primary factor, risk highlights, violations)
 *   4. Counterfactual Analysis (cards with score transitions)
 *   5. Agent Reasoning (proposing agent + governance agents)
 *   6. Audit Trail (UUID, timestamp, collapsible JSON)
 *
 * Props:
 *   evaluation — the raw evaluation record from /api/evaluations
 *   onBack     — callback to return to the main dashboard
 */

import React, { useEffect, useState } from 'react'
import { fetchExplanation } from '../api'

// ── Helpers ────────────────────────────────────────────────────────────────

const VERDICT_CONFIG = {
    approved: { emoji: '✅', label: 'APPROVED', bg: 'bg-green-500/10', border: 'border-green-500/40', text: 'text-green-400', glow: 'shadow-green-500/20' },
    escalated: { emoji: '⚠️', label: 'ESCALATED', bg: 'bg-yellow-500/10', border: 'border-yellow-500/40', text: 'text-yellow-400', glow: 'shadow-yellow-500/20' },
    denied: { emoji: '🚫', label: 'DENIED', bg: 'bg-red-500/10', border: 'border-red-500/40', text: 'text-red-400', glow: 'shadow-red-500/20' },
}

function barColor(score) {
    if (score <= 25) return 'bg-green-500'
    if (score <= 60) return 'bg-yellow-500'
    return 'bg-red-500'
}

function verdictPill(verdict) {
    const v = (verdict ?? '').toLowerCase()
    const c = VERDICT_CONFIG[v] ?? VERDICT_CONFIG.approved
    return (
        <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${c.text} ${c.bg} ${c.border}`}>
            {c.label}
        </span>
    )
}

function shortResource(id) {
    if (!id) return '—'
    return id.split('/').filter(Boolean).pop() ?? id
}

function formatTime(iso) {
    if (!iso) return ''
    try {
        return new Date(iso).toLocaleString()
    } catch {
        return iso
    }
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function EvaluationDrilldown({ evaluation, onBack }) {
    const [explanation, setExplanation] = useState(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [jsonExpanded, setJsonExpanded] = useState(false)

    const ev = evaluation
    const decision = (ev.decision ?? 'approved').toLowerCase()
    const vc = VERDICT_CONFIG[decision] ?? VERDICT_CONFIG.approved
    // /api/evaluations returns flat tracker records: { sri_composite, sri_breakdown: { infrastructure, ... } }
    // Map to the expected shape so sri.sri_composite and sri.sri_infrastructure etc. resolve correctly.
    const sri = ev.skry_risk_index ?? {
        sri_composite: ev.sri_composite ?? 0,
        sri_infrastructure: ev.sri_breakdown?.infrastructure ?? 0,
        sri_policy: ev.sri_breakdown?.policy ?? 0,
        sri_historical: ev.sri_breakdown?.historical ?? 0,
        sri_cost: ev.sri_breakdown?.cost ?? 0,
    }

    useEffect(() => {
        let cancelled = false
        setLoading(true)
        setError(null)
        fetchExplanation(ev.action_id)
            .then(data => { if (!cancelled) setExplanation(data) })
            .catch(err => { if (!cancelled) setError(err.message) })
            .finally(() => { if (!cancelled) setLoading(false) })
        return () => { cancelled = true }
    }, [ev.action_id])

    // SRI dimension display config
    const dimensions = [
        { key: 'sri_infrastructure', label: 'Infrastructure (Blast Radius)', weight: 0.30, maxW: 30 },
        { key: 'sri_policy', label: 'Policy Compliance', weight: 0.25, maxW: 25 },
        { key: 'sri_historical', label: 'Historical Patterns', weight: 0.25, maxW: 25 },
        { key: 'sri_cost', label: 'Financial Impact', weight: 0.20, maxW: 20 },
    ]

    return (
        <div className="max-w-5xl mx-auto px-4 py-6 space-y-6">

            {/* ── Back Button ── */}
            <button
                onClick={onBack}
                className="flex items-center gap-2 text-sm text-slate-400 hover:text-slate-200 transition-colors mb-2"
            >
                <span>←</span> Back to Dashboard
            </button>

            {/* ═══════════════════════════════════════════════════════════════
          Section 1 — Verdict Header
          ═══════════════════════════════════════════════════════════════ */}
            <div className={`rounded-xl border ${vc.border} ${vc.bg} p-6 shadow-lg ${vc.glow}`}>
                <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                    {/* Large verdict badge */}
                    <div className="flex items-center gap-3">
                        <span className="text-4xl">{vc.emoji}</span>
                        <div>
                            <h1 className={`text-2xl font-black ${vc.text}`}>{vc.label}</h1>
                            <p className="text-xs text-slate-500 mt-0.5">Governance Verdict</p>
                        </div>
                    </div>

                    {/* SRI composite — large number */}
                    <div className="sm:ml-auto text-right">
                        <div className={`text-4xl font-black tabular-nums ${vc.text}`}>
                            {(sri.sri_composite ?? 0).toFixed(1)}
                        </div>
                        <p className="text-xs text-slate-500">SRI™ Composite</p>
                    </div>
                </div>

                {/* Resource + Agent info */}
                <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                    <div>
                        <span className="text-slate-500 block">Resource</span>
                        <span className="text-slate-200 font-mono truncate block" title={ev.resource_id}>
                            {shortResource(ev.resource_id)}
                        </span>
                    </div>
                    <div>
                        <span className="text-slate-500 block">Type</span>
                        <span className="text-slate-300">{ev.resource_type ?? ev.action_type?.replace(/_/g, ' ') ?? '—'}</span>
                    </div>
                    <div>
                        <span className="text-slate-500 block">Proposing Agent</span>
                        <span className="text-blue-400 font-mono">{ev.agent_id ?? '—'}</span>
                    </div>
                    <div>
                        <span className="text-slate-500 block">Timestamp</span>
                        <span className="text-slate-300">{formatTime(ev.timestamp)}</span>
                    </div>
                </div>
            </div>

            {/* ═══════════════════════════════════════════════════════════════
          Section 2 — SRI™ Breakdown (4 horizontal bars)
          ═══════════════════════════════════════════════════════════════ */}
            <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
                    SRI™ Dimensional Breakdown
                </h2>
                <div className="space-y-4">
                    {dimensions.map((dim, i) => {
                        const score = sri[dim.key] ?? 0
                        const weighted = (score * dim.weight).toFixed(1)
                        const pct = Math.min((score / 100) * 100, 100)
                        const isPrimary = explanation?.contributing_factors?.[0]?.dimension?.includes(dim.label.split(' ')[0])
                        return (
                            <div key={dim.key}>
                                <div className="flex items-center justify-between mb-1">
                                    <span className="text-xs text-slate-300 flex items-center gap-1.5">
                                        {isPrimary && <span className="text-yellow-400" title="Primary factor">⭐</span>}
                                        {dim.label}
                                    </span>
                                    <span className="text-xs tabular-nums text-slate-400">
                                        {weighted}/{dim.maxW}
                                        <span className="text-slate-600 ml-1">
                                            (score {score.toFixed(0)} × {dim.weight})
                                        </span>
                                    </span>
                                </div>
                                <div className="w-full h-2.5 bg-slate-700 rounded-full overflow-hidden">
                                    <div
                                        className={`h-full rounded-full transition-all duration-700 ${barColor(score)} ${isPrimary ? 'ring-1 ring-yellow-400/50' : ''}`}
                                        style={{ width: `${pct}%` }}
                                    />
                                </div>
                            </div>
                        )
                    })}
                </div>
            </div>

            {/* ═══════════════════════════════════════════════════════════════
          Section 3 — Decision Explanation
          ═══════════════════════════════════════════════════════════════ */}
            <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
                    Decision Explanation
                </h2>

                {loading ? (
                    <div className="flex items-center gap-2 py-4">
                        <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
                        <span className="text-sm text-slate-400">Generating explanation…</span>
                    </div>
                ) : error ? (
                    <p className="text-sm text-red-400">{error}</p>
                ) : explanation ? (
                    <div className="space-y-4">
                        {/* Summary */}
                        <p className="text-sm text-slate-200 leading-relaxed">{explanation.summary}</p>

                        {/* Primary Factor callout */}
                        <div className={`rounded-lg border ${vc.border} ${vc.bg} p-4`}>
                            <span className="text-xs font-semibold text-slate-400 uppercase">Primary Factor</span>
                            <p className={`text-sm ${vc.text} mt-1 font-medium`}>{explanation.primary_factor}</p>
                        </div>

                        {/* Risk Highlights */}
                        {explanation.risk_highlights?.length > 0 && (
                            <div>
                                <h3 className="text-xs font-semibold text-slate-400 uppercase mb-2">Risk Highlights</h3>
                                <ul className="space-y-1">
                                    {explanation.risk_highlights.map((h, i) => (
                                        <li key={i} className="text-xs text-slate-300 flex items-start gap-2">
                                            <span className="text-orange-400 shrink-0 mt-0.5">▸</span>
                                            {h}
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        )}

                        {/* Policy Violations */}
                        {explanation.policy_violations?.length > 0 && (
                            <div>
                                <h3 className="text-xs font-semibold text-slate-400 uppercase mb-2">Policy Violations</h3>
                                <div className="space-y-1.5">
                                    {explanation.policy_violations.map((v, i) => (
                                        <div key={i} className="text-xs bg-red-500/5 border border-red-500/20 rounded-lg px-3 py-2 text-red-300">
                                            {v}
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                ) : null}
            </div>

            {/* ═══════════════════════════════════════════════════════════════
          Section 4 — Counterfactual Analysis
          ═══════════════════════════════════════════════════════════════ */}
            {explanation?.counterfactuals?.length > 0 && (
                <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                    <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-1">
                        {decision === 'approved' ? 'What Would Have Triggered Escalation?' : 'What Would Change This Outcome?'}
                    </h2>
                    <p className="text-xs text-slate-500 mb-4">Counterfactual analysis — hypothetical scenarios</p>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {explanation.counterfactuals.map((cf, i) => (
                            <div key={i} className="bg-slate-900/60 border border-slate-700/60 rounded-lg p-4 hover:border-slate-600 transition-colors">
                                {/* Change description */}
                                <p className="text-sm text-slate-200 font-medium mb-3">{cf.change_description}</p>

                                {/* Score transition */}
                                <div className="flex items-center gap-3 mb-3">
                                    <span className={`text-lg font-bold tabular-nums ${vc.text}`}>
                                        {(sri.sri_composite ?? 0).toFixed(1)}
                                    </span>
                                    <span className="text-slate-600">→</span>
                                    <span className={`text-lg font-bold tabular-nums ${cf.predicted_new_verdict === 'APPROVED' ? 'text-green-400' :
                                        cf.predicted_new_verdict === 'ESCALATED' ? 'text-yellow-400' :
                                            'text-red-400'
                                        }`}>
                                        {cf.predicted_new_score.toFixed(1)}
                                    </span>
                                    <span className="ml-auto">
                                        {verdictPill(cf.predicted_new_verdict)}
                                    </span>
                                </div>

                                {/* Explanation */}
                                <p className="text-xs text-slate-400 leading-relaxed">{cf.explanation}</p>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* ═══════════════════════════════════════════════════════════════
          Section 5 — Agent Reasoning
          ═══════════════════════════════════════════════════════════════ */}
            <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
                    Agent Reasoning
                </h2>

                {/* Proposing agent's reason */}
                <div className="mb-4">
                    <h3 className="text-xs text-blue-400 font-mono mb-1">
                        Proposing Agent — {ev.agent_id ?? 'unknown'}
                    </h3>
                    <p className="text-sm text-slate-300 bg-slate-900/40 rounded-lg p-3 border border-slate-700/40">
                        {ev.proposed_action?.reason ?? ev.reason ?? 'No reasoning provided.'}
                    </p>
                </div>

                {/* Governance agent reasoning */}
                {explanation?.contributing_factors?.length > 0 && (
                    <div className="space-y-3">
                        <h3 className="text-xs text-slate-400 font-semibold uppercase">Governance Agent Assessments</h3>
                        {explanation.contributing_factors.map((f, i) => (
                            <div key={i} className="bg-slate-900/40 rounded-lg p-3 border border-slate-700/40">
                                <div className="flex items-center justify-between mb-1">
                                    <span className="text-xs font-medium text-slate-300">{f.dimension}</span>
                                    <span className={`text-xs tabular-nums font-bold ${f.score <= 25 ? 'text-green-400' : f.score <= 60 ? 'text-yellow-400' : 'text-red-400'
                                        }`}>
                                        {f.score.toFixed(0)}/100
                                    </span>
                                </div>
                                {f.reasoning && (
                                    <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                                        {f.reasoning.length > 500 ? f.reasoning.slice(0, 500) + '…' : f.reasoning}
                                    </p>
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* ═══════════════════════════════════════════════════════════════
          Section 6 — Audit Trail
          ═══════════════════════════════════════════════════════════════ */}
            <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
                    Audit Trail
                </h2>

                <div className="grid grid-cols-2 gap-4 text-xs mb-4">
                    <div>
                        <span className="text-slate-500 block">Decision ID</span>
                        <span className="text-slate-300 font-mono break-all">{ev.action_id ?? '—'}</span>
                    </div>
                    <div>
                        <span className="text-slate-500 block">Timestamp</span>
                        <span className="text-slate-300">{formatTime(ev.timestamp)}</span>
                    </div>
                </div>

                {/* Collapsible JSON */}
                <button
                    onClick={() => setJsonExpanded(!jsonExpanded)}
                    className="text-xs text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1"
                >
                    <span className={`transition-transform ${jsonExpanded ? 'rotate-90' : ''}`}>▶</span>
                    {jsonExpanded ? 'Hide' : 'Show'} Full Verdict JSON
                </button>

                {jsonExpanded && (
                    <pre className="mt-3 text-xs text-slate-400 bg-slate-900 rounded-lg p-4 overflow-x-auto max-h-80 border border-slate-700">
                        {JSON.stringify(ev, null, 2)}
                    </pre>
                )}
            </div>
        </div>
    )
}
