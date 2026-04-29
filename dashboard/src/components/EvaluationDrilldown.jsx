/**
 * EvaluationDrilldown.jsx — full-page drilldown for a governance evaluation.
 *
 * Sections:
 *   1. Verdict Header (badge, SRI, resource info, agent, timestamp)
 *   2. SRI™ Breakdown (4 weighted horizontal bars, primary factor star)
 *   3. Decision Explanation (summary, primary factor, risk highlights, violations)
 *   4. Counterfactual Analysis (cards with score transitions)
 *   5. Agent Reasoning (proposing agent + governance agents)
 *   6. Execution Status (action buttons, PR link, conditions)
 *   7. Tier 3 Remediation Playbook
 *   8. Audit Trail + Override History (UUID, timestamp, collapsible JSON)
 *
 * Props:
 *   evaluation — the raw evaluation record from /api/evaluations
 *   onBack     — callback to return to the main dashboard
 */

import React, { useEffect, useRef, useState } from 'react'
import { approveExecution, createPRFromManual, dismissExecution, executeAgentFix, fetchAgentFixPreview, fetchExecutionStatus, fetchExplanation, fetchTerraformStub, rollbackAgentFix, satisfyCondition } from '../api'
import TerraformPROverlay from './TerraformPROverlay'
import PlaybookPanel from './PlaybookPanel'
import OverrideHistoryPanel from './OverrideHistoryPanel'
import InfoIcon from './glossary/InfoIcon'

// Map verdict string → glossary term id for the inline info icon
const VERDICT_TERM = {
    approved: 'approved',
    approved_if: 'approved-if',
    escalated: 'escalated',
    denied: 'denied',
}

// ── Helpers ────────────────────────────────────────────────────────────────

const VERDICT_CONFIG = {
    approved: { emoji: '✅', label: 'APPROVED', bg: 'bg-green-500/10', border: 'border-green-500/40', text: 'text-green-400', glow: 'shadow-green-500/20' },
    approved_if: { emoji: '🔒', label: 'APPROVED IF', bg: 'bg-amber-500/10', border: 'border-amber-400/40', text: 'text-amber-300', glow: 'shadow-amber-500/20' },
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

// ── RemediationConfidenceBadge ────────────────────────────────────────────────
// Shows how confidently the execution agent can automate the fix.

const CONFIDENCE_CONFIG = {
    auto_fix:      { label: 'Automated fix',          color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/25', dot: 'bg-emerald-400', tip: 'A specific, well-tested SDK tool handles this end-to-end' },
    generic_fix:   { label: 'Generic fix available',  color: 'text-blue-400',    bg: 'bg-blue-500/10',    border: 'border-blue-500/25',    dot: 'bg-blue-400',    tip: 'Generic ARM PATCH — likely works; verify the resource after execution' },
    guided_manual: { label: 'Guided manual steps',    color: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/25',   dot: 'bg-amber-400',   tip: 'We know the exact steps — see the az CLI commands and Portal guide below' },
    manual:        { label: 'Manual required',         color: 'text-slate-400',   bg: 'bg-slate-500/10',   border: 'border-slate-500/25',   dot: 'bg-slate-400',   tip: 'No automated path — investigation required before acting' },
}

function RemediationConfidenceBadge({ confidence }) {
    if (!confidence) return null
    const cfg = CONFIDENCE_CONFIG[confidence] ?? CONFIDENCE_CONFIG.manual
    return (
        <div className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[10px] font-medium ${cfg.color} ${cfg.bg} ${cfg.border}`} title={cfg.tip}>
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cfg.dot}`} />
            {cfg.label}
        </div>
    )
}

// ── GuidedManualSteps ─────────────────────────────────────────────────────────
// Renders az CLI commands (copyable) + Portal steps (numbered list) for a
// guided_manual plan step. Shown inline in the plan view when step.operation
// is "guided_manual".

function CopyButton({ text }) {
    const [copied, setCopied] = React.useState(false)
    const copy = () => {
        navigator.clipboard?.writeText(text).then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 2000)
        })
    }
    return (
        <button
            onClick={copy}
            className="ml-2 px-1.5 py-0.5 text-[9px] rounded border border-slate-600 text-slate-400 hover:border-slate-400 hover:text-slate-200 transition-colors"
        >
            {copied ? 'copied' : 'copy'}
        </button>
    )
}

function GuidedManualSteps({ params }) {
    if (!params) return null
    const cmds = params.az_cli_commands ?? []
    const portalSteps = params.portal_steps ?? []
    const docUrl = params.doc_url ?? ''
    const allCmds = cmds.join('\n')

    return (
        <div className="space-y-3 mt-2">
            {/* az CLI commands */}
            {cmds.length > 0 && (
                <div>
                    <div className="flex items-center justify-between mb-1">
                        <span className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">az CLI Commands</span>
                        <CopyButton text={allCmds} />
                    </div>
                    <pre className="text-xs text-emerald-300 bg-slate-950 rounded-lg p-3 overflow-x-auto border border-slate-700/50 whitespace-pre-wrap leading-relaxed">
                        {cmds.map((cmd, i) => (
                            <span key={i} className="block">
                                <span className={cmd.startsWith('#') ? 'text-slate-500' : 'text-emerald-300'}>
                                    {cmd.startsWith('#') ? cmd : `$ ${cmd}`}
                                </span>
                            </span>
                        ))}
                    </pre>
                </div>
            )}

            {/* Portal steps */}
            {portalSteps.length > 0 && (
                <div>
                    <span className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold block mb-1">Azure Portal Steps</span>
                    <ol className="space-y-1">
                        {portalSteps.map((step, i) => (
                            <li key={i} className="flex items-start gap-2 text-xs text-slate-300">
                                <span className="shrink-0 w-5 h-5 rounded-full bg-slate-700 text-slate-400 flex items-center justify-center text-[9px] font-bold mt-0.5">{i + 1}</span>
                                {step}
                            </li>
                        ))}
                    </ol>
                </div>
            )}

            {/* Doc link */}
            {docUrl && (
                <p className="text-[10px] text-slate-500">
                    Docs: <a href={docUrl} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 underline underline-offset-2">{docUrl}</a>
                </p>
            )}
        </div>
    )
}

// ── AgentFixPlanView ────────────────────────────────────────────────────────
// Renders the structured execution plan returned by the LLM-driven agent.
// Falls back to raw commands if plan has no steps (backward compat).

function AgentFixPlanView({ plan }) {
    if (!plan) return null

    const hasSteps = Array.isArray(plan.steps) && plan.steps.length > 0
    // Collect all guided_manual steps to render their detailed sections below the table
    const guidedSteps = hasSteps ? plan.steps.filter(s => s.operation === 'guided_manual') : []

    return (
        <div className="space-y-3">
            {/* Summary + confidence badge on same line */}
            <div className="flex items-center gap-3 flex-wrap">
                {plan.summary && (
                    <p className="text-xs text-slate-300 font-medium flex-1">{plan.summary}</p>
                )}
                <RemediationConfidenceBadge confidence={plan.remediation_confidence} />
            </div>

            {/* Steps table */}
            {hasSteps ? (
                <div className="overflow-x-auto rounded-lg border border-slate-700">
                    <table className="w-full text-xs text-slate-300">
                        <thead>
                            <tr className="bg-slate-800/80 text-slate-400 uppercase text-[10px] tracking-wider">
                                <th className="px-3 py-2 text-left w-6">#</th>
                                <th className="px-3 py-2 text-left">Operation</th>
                                <th className="px-3 py-2 text-left">Target</th>
                                <th className="px-3 py-2 text-left">Reason</th>
                            </tr>
                        </thead>
                        <tbody>
                            {plan.steps.map((step, i) => (
                                <tr key={i} className="border-t border-slate-700/60 hover:bg-slate-800/30">
                                    <td className="px-3 py-2 text-slate-500">{i + 1}</td>
                                    <td className="px-3 py-2 font-mono text-purple-300">{step.operation}</td>
                                    <td className="px-3 py-2 text-slate-400 max-w-[180px] truncate" title={step.target}>{step.target}</td>
                                    <td className="px-3 py-2 text-slate-400">{step.reason}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            ) : (
                <p className="text-xs text-slate-500 italic">No steps — resource may already be in the desired state.</p>
            )}

            {/* Guided manual detail sections */}
            {guidedSteps.map((step, i) => (
                <div key={i} className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
                    <p className="text-[10px] text-amber-400 uppercase tracking-wider font-semibold mb-2">
                        Step {plan.steps.indexOf(step) + 1} — Guided Manual Steps
                    </p>
                    <GuidedManualSteps params={step.params} />
                </div>
            ))}

            {/* Impact + rollback */}
            {plan.estimated_impact && (
                <p className="text-xs text-amber-400/80">⚡ Impact: {plan.estimated_impact}</p>
            )}
            {plan.rollback_hint && (
                <p className="text-xs text-slate-500">↩ Rollback: <code className="text-slate-400">{plan.rollback_hint}</code></p>
            )}

            {/* Equivalent CLI (backward compat — non-guided steps) */}
            {Array.isArray(plan.commands) && plan.commands.length > 0 && (
                <details className="group">
                    <summary className="text-[10px] text-slate-500 cursor-pointer hover:text-slate-400 select-none">Equivalent CLI commands</summary>
                    <pre className="mt-1 text-xs text-slate-400 bg-slate-900 rounded-lg p-3 overflow-x-auto border border-slate-700/50 whitespace-pre-wrap">
                        {plan.commands.map(cmd => `$ ${cmd}`).join('\n')}
                    </pre>
                </details>
            )}
        </div>
    )
}

// ── ExecutionLogView ────────────────────────────────────────────────────────
// Renders the step-by-step execution log + verification badge.

function ExecutionLogView({ steps, verification, label = 'Execution Log' }) {
    if (!steps?.length && !verification) return null
    return (
        <div className="space-y-2 mt-2">
            {steps?.length > 0 && (
                <details open>
                    <summary className="text-[10px] text-slate-500 cursor-pointer hover:text-slate-400 select-none uppercase tracking-wide font-semibold">
                        {label} ({steps.length} step{steps.length !== 1 ? 's' : ''})
                    </summary>
                    <div className="mt-1.5 rounded-lg border border-slate-700/60 overflow-hidden">
                        {steps.map((step, i) => (
                            <div
                                key={i}
                                className={`flex items-start gap-2 px-3 py-2 text-xs border-b border-slate-800/60 last:border-0 ${
                                    step.success ? 'text-slate-300' : 'text-rose-300 bg-rose-500/5'
                                }`}
                            >
                                <span className={`shrink-0 mt-0.5 ${step.success ? 'text-emerald-400' : 'text-rose-400'}`}>
                                    {step.success ? '✓' : '✗'}
                                </span>
                                <span className="font-mono text-violet-300 shrink-0">{step.operation ?? `step ${i + 1}`}</span>
                                <span className="text-slate-400 min-w-0">{step.message}</span>
                            </div>
                        ))}
                    </div>
                </details>
            )}
            {verification && (
                <div className={`flex items-start gap-2 text-xs rounded-lg px-3 py-2 border ${
                    verification.confirmed
                        ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-300'
                        : 'bg-amber-500/10 border-amber-500/25 text-amber-300'
                }`}>
                    <span className="shrink-0">{verification.confirmed ? '✓' : '⚠'}</span>
                    <div>
                        <span className="font-semibold mr-1">{verification.confirmed ? 'Verified:' : 'Unconfirmed:'}</span>
                        {verification.message}
                    </div>
                </div>
            )}
        </div>
    )
}

// ── AgentTerminal ────────────────────────────────────────────────────────────
// Black terminal window that streams execution steps line-by-line.

const TERM_COLORS = {
    init:        'text-slate-600',
    info:        'text-slate-500',
    step:        'text-violet-400',
    success:     'text-emerald-400',
    error:       'text-rose-400',
    verify_ok:   'text-emerald-300',
    verify_warn: 'text-amber-300',
    complete:    'text-emerald-400 font-bold',
    failed:      'text-rose-400 font-bold',
}

function AgentTerminal({ lines, running }) {
    const bottomRef = useRef(null)
    useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [lines])

    return (
        <div className="rounded-lg overflow-hidden border border-slate-700/40 bg-[#080c08] font-mono text-xs leading-relaxed mt-3">
            {/* macOS-style title bar */}
            <div className="flex items-center gap-1.5 px-3 py-2 bg-[#111611] border-b border-slate-700/30">
                <span className="w-2.5 h-2.5 rounded-full bg-rose-500/60" />
                <span className="w-2.5 h-2.5 rounded-full bg-amber-500/60" />
                <span className="w-2.5 h-2.5 rounded-full bg-emerald-500/60" />
                <span className="mx-auto text-slate-600 text-[10px] tracking-[0.2em] uppercase select-none">
                    execution terminal
                </span>
                {running && (
                    <span className="flex items-center gap-1.5 text-[10px] text-emerald-500">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                        running
                    </span>
                )}
            </div>
            {/* Terminal body */}
            <div className="p-3 min-h-[60px] max-h-60 overflow-y-auto space-y-0.5">
                {lines.map((line, i) => (
                    <div key={i} className={`${TERM_COLORS[line.type] ?? 'text-slate-400'} whitespace-pre-wrap break-all leading-5`}>
                        {line.text}
                    </div>
                ))}
                {running && <span className="text-emerald-400 animate-pulse">█</span>}
                <div ref={bottomRef} />
            </div>
        </div>
    )
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function EvaluationDrilldown({ evaluation, onBack, reviewedBy }) {
    const [explanation, setExplanation] = useState(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)
    const [jsonExpanded, setJsonExpanded] = useState(false)
    const [executionStatus, setExecutionStatus] = useState(null)
    const [execLoading, setExecLoading] = useState(true)
    const [tfStub, setTfStub] = useState(null)
    const [tfLoading, setTfLoading] = useState(false)
    const [tfExpanded, setTfExpanded] = useState(false)
    const [agentFixPreview, setAgentFixPreview] = useState(null)
    const [agentFixLoading, setAgentFixLoading] = useState(false)
    const [agentFixExpanded, setAgentFixExpanded] = useState(false)
    const [agentFixExecuting, setAgentFixExecuting] = useState(false)
    const [agentFixResult, setAgentFixResult] = useState(null)
    const [terminalLines, setTerminalLines] = useState([])
    const [createPrLoading, setCreatePrLoading] = useState(false)
    const [showDismissInput, setShowDismissInput] = useState(false)
    const [dismissReasonDraft, setDismissReasonDraft] = useState('')
    const [showSatisfyInput, setShowSatisfyInput] = useState(null) // condition index | null
    const [satisfyWhoDraft, setSatisfyWhoDraft] = useState('')
    const [createPrError, setCreatePrError] = useState(null)
    const [showPROverlay, setShowPROverlay] = useState(false)
    const [rollbackExecuting, setRollbackExecuting] = useState(false)
    const [rollbackResult, setRollbackResult] = useState(null)

    // Normalize: evaluation may be a flat tracker record OR a GovernanceVerdict
    // snapshot (from ?exec= URL). Flatten proposed_action fields so the rest of
    // the component can read resource_id / action_type / agent_id uniformly.
    const ev = {
        ...evaluation,
        resource_id:   evaluation.resource_id   ?? evaluation.proposed_action?.target?.resource_id,
        resource_type: evaluation.resource_type ?? evaluation.proposed_action?.target?.resource_type,
        action_type:   evaluation.action_type   ?? evaluation.proposed_action?.action_type,
        agent_id:      evaluation.agent_id      ?? evaluation.proposed_action?.agent_id,
    }
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

    useEffect(() => {
        let cancelled = false
        setExecLoading(true)
        fetchExecutionStatus(ev.action_id)
            .then(data => {
                if (!cancelled) {
                    // Pick the most recent execution record if multiple exist
                    const execs = data.executions
                    setExecutionStatus(execs && execs.length > 0 ? execs[execs.length - 1] : null)
                }
            })
            .catch(() => { if (!cancelled) setExecutionStatus(null) })
            .finally(() => { if (!cancelled) setExecLoading(false) })
        return () => { cancelled = true }
    }, [ev.action_id])

    async function handleApprove(executionId) {
        try {
            const updated = await approveExecution(executionId, reviewedBy || 'dashboard-user')
            setExecutionStatus(updated)
        } catch (err) {
            alert(`Approve failed: ${err.message}`)
        }
    }

    function handleDismiss(executionId, prefillReason = '') {
        setDismissReasonDraft(prefillReason)
        setShowDismissInput(executionId)
    }

    async function confirmDismiss() {
        const executionId = showDismissInput
        setShowDismissInput(false)
        try {
            const updated = await dismissExecution(executionId, reviewedBy || 'dashboard-user', dismissReasonDraft)
            setExecutionStatus(updated)
        } catch (err) {
            alert(`Dismiss failed: ${err.message}`)
        }
    }

    async function confirmSatisfy() {
        const idx = showSatisfyInput
        const who = satisfyWhoDraft.trim() || reviewedBy || 'dashboard-user'
        setShowSatisfyInput(null)
        setSatisfyWhoDraft('')
        try {
            await satisfyCondition(executionStatus.execution_id, idx, who)
            const fresh = await fetchExecutionStatus(executionStatus.execution_id)
            setExecutionStatus(fresh)
        } catch (err) {
            alert(`Failed to satisfy condition: ${err.message}`)
        }
    }

    async function handleShowTerraform(executionId) {
        if (tfStub) { setTfExpanded(e => !e); return }
        setTfLoading(true)
        setTfExpanded(true)
        try {
            const data = await fetchTerraformStub(executionId)
            setTfStub(data.hcl)
        } catch (err) {
            setTfStub(`# Error fetching stub: ${err.message}`)
        } finally {
            setTfLoading(false)
        }
    }

    async function handleCreatePR(executionId, iacRepo = '', iacPath = '', confirmedChange = null) {
        setCreatePrLoading(true)
        setCreatePrError(null)
        try {
            const updated = await createPRFromManual(executionId, reviewedBy || 'dashboard-user', iacRepo, iacPath, confirmedChange)
            setExecutionStatus(updated)
            setShowPROverlay(false)
        } catch (err) {
            setCreatePrError(err.message)
            // Keep overlay open so user can correct the repo/path
        } finally {
            setCreatePrLoading(false)
        }
    }

    function handleOpenPROverlay() {
        setCreatePrError(null)
        setShowPROverlay(true)
    }

    async function handleAgentFixPreview(executionId) {
        if (agentFixPreview) { setAgentFixExpanded(e => !e); setTerminalLines([]); return }
        setAgentFixLoading(true)
        setAgentFixExpanded(true)
        setTerminalLines([])
        try {
            const data = await fetchAgentFixPreview(executionId)
            setAgentFixPreview(data)
        } catch (err) {
            setAgentFixPreview({ commands: [`# Error: ${err.message}`], warning: '' })
        } finally {
            setAgentFixLoading(false)
        }
    }

    async function handleAgentFixExecute(executionId) {
        if (!window.confirm('This will run az CLI commands against your Azure environment. Continue?')) return
        const ts = () => new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        setAgentFixExecuting(true)
        setAgentFixResult(null)
        setTerminalLines([
            { type: 'init', text: `[${ts()}] ▶  Execution started` },
            { type: 'init', text: `[${ts()}] ▶  Connecting to Azure environment...` },
        ])

        // Show live progress while the API call is pending (LLM + Azure SDK can take 20-60s)
        const progressMsgs = [
            'Requesting execution plan from AI...',
            'Validating approved governance decision...',
            'Preparing Azure SDK tool calls...',
            'Waiting for Azure control plane response...',
        ]
        let progIdx = 0
        const progressInterval = setInterval(() => {
            if (progIdx < progressMsgs.length) {
                setTerminalLines(prev => [...prev, { type: 'info', text: `[${ts()}] ⟳  ${progressMsgs[progIdx++]}` }])
            }
        }, 2000)

        let updated
        try {
            updated = await executeAgentFix(executionId, reviewedBy || 'dashboard-user')
            clearInterval(progressInterval)
            setExecutionStatus(updated)
            setAgentFixResult(updated)
        } catch (err) {
            clearInterval(progressInterval)
            setTerminalLines(prev => [...prev,
                { type: 'error', text: `[${ts()}] ✗  Error: ${err.message}` },
            ])
            setAgentFixResult({ status: 'failed', notes: err.message })
            setAgentFixExecuting(false)
            return
        }

        // Animate each step line in with a short delay so it feels live
        const steps = updated.execution_log ?? []
        const animLines = [{ type: 'info', text: `[${ts()}] ▶  Agent returned ${steps.length} step(s) — animating results...` }]
        for (let i = 0; i < steps.length; i++) {
            const s = steps[i]
            animLines.push({ type: 'step',    text: `[${ts()}] ▶  [${i + 1}/${steps.length}] ${s.operation}` })
            animLines.push({ type: s.success ? 'success' : 'error',
                             text: `[${ts()}]    ${s.success ? '✓' : '✗'}  ${s.message}` })
        }
        if (updated.verification) {
            const v = updated.verification
            animLines.push({ type: 'info',       text: `[${ts()}] ▶  Running post-execution verification...` })
            animLines.push({ type: v.confirmed ? 'verify_ok' : 'verify_warn',
                             text: `[${ts()}]    ${v.confirmed ? '✓' : '⚠'}  ${v.message}` })
        }
        const ok = steps.filter(s => s.success).length
        animLines.push({
            type: updated.status === 'applied' ? 'complete' : 'failed',
            text: `[${ts()}] ${'─'.repeat(4)} ${updated.status === 'applied' ? 'EXECUTION COMPLETE' : 'EXECUTION FAILED'} — ${ok}/${steps.length} steps ${'─'.repeat(4)}`,
        })

        for (const line of animLines) {
            await new Promise(r => setTimeout(r, 140))
            setTerminalLines(prev => [...prev, line])
        }
        setAgentFixExecuting(false)
    }

    async function handleRollback(executionId) {
        const hint = executionStatus?.execution_plan?.rollback_hint
        const msg = hint
            ? `Roll back this fix?\n\nRollback operation:\n${hint}`
            : 'Roll back this fix? This will attempt to reverse the applied change.'
        if (!window.confirm(msg)) return
        setRollbackExecuting(true)
        setRollbackResult(null)
        try {
            const updated = await rollbackAgentFix(executionId, reviewedBy || 'dashboard-user')
            setExecutionStatus(updated)
            setRollbackResult(updated)
        } catch (err) {
            setRollbackResult({ status: 'failed', notes: err.message, rollback_log: [] })
        } finally {
            setRollbackExecuting(false)
        }
    }

    // SRI dimension display config
    const dimensions = [
        { key: 'sri_infrastructure', label: 'Infrastructure (Blast Radius)', weight: 0.30, maxW: 30 },
        { key: 'sri_policy', label: 'Policy Compliance', weight: 0.25, maxW: 25 },
        { key: 'sri_historical', label: 'Historical Patterns', weight: 0.25, maxW: 25 },
        { key: 'sri_cost', label: 'Financial Impact', weight: 0.20, maxW: 20 },
    ]

    return (
        <div className="max-w-7xl mx-auto px-4 py-6 space-y-6">

            {/* ═══════════════════════════════════════════════════════════════
          Section 1 — Verdict Header
          ═══════════════════════════════════════════════════════════════ */}
            <div className={`rounded-xl border ${vc.border} ${vc.bg} p-6 shadow-lg ${vc.glow}`}>
                <div className="lg:grid lg:grid-cols-3 gap-6">

                    {/* Left col (spans 2): verdict badge + metadata */}
                    <div className="lg:col-span-2">
                        <div className="flex items-center gap-3 mb-4">
                            <span className="text-4xl">{vc.emoji}</span>
                            <div>
                                <h1 className={`text-2xl font-black ${vc.text} flex items-center gap-2`}>
                                    {vc.label}
                                    {VERDICT_TERM[(ev.verdict ?? '').toLowerCase()] && (
                                        <InfoIcon termId={VERDICT_TERM[(ev.verdict ?? '').toLowerCase()]} size={16} />
                                    )}
                                </h1>
                                <p className="text-xs text-slate-500 mt-0.5 flex items-center gap-1">
                                    Governance Verdict
                                    <InfoIcon termId="governance-verdict" size={11} />
                                </p>
                            </div>
                        </div>

                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                            <div>
                                <span className="text-slate-500 block">Resource</span>
                                <span className="text-slate-200 font-mono truncate block" title={ev.resource_id}>
                                    {shortResource(ev.resource_id)}
                                </span>
                                {ev.resource_type && (
                                    <span className="text-slate-600 block truncate" title={ev.resource_type}>
                                        {ev.resource_type.split('/').pop()}
                                    </span>
                                )}
                            </div>
                            <div>
                                <span className="text-slate-500 block">Action Proposed</span>
                                <span className="text-slate-200 font-medium capitalize">
                                    {ev.action_type?.replace(/_/g, ' ') ?? '—'}
                                </span>
                            </div>
                            <div>
                                <span className="text-slate-500 block">Proposed By</span>
                                <span className="text-blue-400 font-mono">{ev.agent_id ?? '—'}</span>
                            </div>
                            <div>
                                <span className="text-slate-500 block">Timestamp</span>
                                <span className="text-slate-300">{formatTime(ev.timestamp)}</span>
                            </div>
                        </div>
                    </div>

                    {/* Right col: SRI hero + why proposed */}
                    <div className="lg:col-span-1 mt-5 lg:mt-0 lg:border-l lg:border-slate-700/60 lg:pl-6 flex flex-col justify-between">
                        <div className="text-right">
                            <div className={`text-5xl font-black tabular-nums ${vc.text}`}>
                                {(Math.round((sri.sri_composite ?? 0) * 10) / 10).toFixed(1)}
                            </div>
                            <p className="text-xs text-slate-500 flex items-center gap-1 justify-end mt-1">
                                SRI™ Composite
                                <InfoIcon termId="sri-score" size={11} />
                            </p>
                        </div>

                        {ev.action_reason && (
                            <div className="mt-4 text-xs">
                                <span className="text-slate-500 block mb-1">Why proposed</span>
                                <p className={`${vc.bg} ${vc.border} border rounded-lg px-3 py-2 text-slate-300 leading-relaxed`}>
                                    {ev.action_reason}
                                </p>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* ═══════════════════════════════════════════════════════════════
          Section 2 — SRI™ Breakdown (4 horizontal bars)
          ═══════════════════════════════════════════════════════════════ */}
            <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4 flex items-center gap-1.5">
                    SRI™ Dimensional Breakdown
                    <InfoIcon termId="sri-score" />
                </h2>
                <div className="space-y-3">
                    {dimensions.map((dim, i) => {
                        const score = sri[dim.key] ?? 0
                        const weighted = (score * dim.weight).toFixed(1)
                        const pct = Math.min((score / 100) * 100, 100)
                        const isPrimary = explanation?.contributing_factors?.[0]?.dimension?.includes(dim.label.split(' ')[0])
                        return (
                            <div key={dim.key} className="flex items-center gap-3">
                                <span className="w-48 shrink-0 text-xs text-slate-300 flex items-center gap-1.5">
                                    {isPrimary && <span className="text-yellow-400" title="Primary factor">⭐</span>}
                                    {dim.label}
                                </span>
                                <div className="flex-1 h-2.5 bg-slate-700 rounded-full overflow-hidden">
                                    <div
                                        className={`h-full rounded-full transition-all duration-700 ${barColor(score)} ${isPrimary ? 'ring-1 ring-yellow-400/50' : ''}`}
                                        style={{ width: `${pct}%` }}
                                    />
                                </div>
                                <span className="w-36 shrink-0 text-right text-xs tabular-nums text-slate-400">
                                    {weighted}/{dim.maxW}
                                    <span className="text-slate-600 ml-1">
                                        ({score.toFixed(0)} × {dim.weight})
                                    </span>
                                </span>
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
                                        {(Math.round((sri.sri_composite ?? 0) * 10) / 10).toFixed(1)}
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
                        {ev.proposed_action?.reason ?? ev.action_reason ?? ev.verdict_reason ?? ev.reason ?? 'No reasoning provided.'}
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
          Section 6 — Execution Status (Phase 21)
          ═══════════════════════════════════════════════════════════════ */}
            <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
                <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
                    Execution Status
                </h2>

                {execLoading ? (
                    <p className="text-sm text-slate-500 animate-pulse">Loading execution status…</p>
                ) : executionStatus ? (
                    <div className="space-y-4">
                        {/* Status badge row */}
                        <div className="flex items-center gap-3 flex-wrap">
                            <ExecutionStatusBadge status={executionStatus.status} />
                            {executionStatus.iac_managed && (
                                <span className="text-xs text-blue-400 bg-blue-500/10 border border-blue-500/30 px-2 py-0.5 rounded-full">
                                    IaC: {executionStatus.iac_tool || 'terraform'}
                                </span>
                            )}
                            {executionStatus.reviewed_by && (
                                <span className="text-xs text-slate-400">
                                    by {executionStatus.reviewed_by}
                                </span>
                            )}
                            {/* Rollback button — only for agent-applied fixes */}
                            {executionStatus.status === 'applied' && (
                                <button
                                    onClick={() => handleRollback(executionStatus.execution_id)}
                                    disabled={rollbackExecuting}
                                    className="flex items-center gap-1.5 px-3 py-1 bg-amber-600/10 hover:bg-amber-600/20 border border-amber-500/30 text-amber-400 hover:text-amber-300 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
                                >
                                    {rollbackExecuting ? (
                                        <><span className="w-3 h-3 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" /> Rolling back…</>
                                    ) : (
                                        <>↩ Rollback</>
                                    )}
                                </button>
                            )}
                        </div>

                        {/* PR link */}
                        {executionStatus.pr_url && (
                            <a
                                href={executionStatus.pr_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 underline"
                            >
                                View PR #{executionStatus.pr_number} on GitHub →
                            </a>
                        )}

                        {/* Action buttons for pr_created — PR was created via "Create Terraform PR" button */}
                        {executionStatus.status === 'pr_created' && (
                            <div className="space-y-3 pt-1">
                                <p className="text-xs text-blue-300/80 bg-blue-500/5 border border-blue-500/20 rounded-lg px-3 py-2">
                                    A Terraform PR has been created. Review and merge it, or use an alternative fix below.
                                </p>
                                <div className="flex flex-wrap gap-2">
                                    <button
                                        onClick={() => handleAgentFixPreview(executionStatus.execution_id)}
                                        className="flex items-center gap-1.5 px-4 py-2 bg-purple-600/20 hover:bg-purple-600/30 border border-purple-500/40 text-purple-300 hover:text-purple-200 rounded-lg text-sm font-medium transition-colors"
                                    >
                                        🤖 {agentFixExpanded ? 'Hide' : 'Fix using'} Agent
                                    </button>
                                    {ev.resource_id && (
                                        <a
                                            href={`https://portal.azure.com/#resource${ev.resource_id.startsWith('/') ? ev.resource_id : '/' + ev.resource_id}`}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="flex items-center gap-1.5 px-4 py-2 bg-slate-700/50 hover:bg-slate-700 border border-slate-500/40 text-slate-300 hover:text-slate-100 rounded-lg text-sm font-medium transition-colors no-underline"
                                        >
                                            🌐 Fix in Azure Portal instead
                                        </a>
                                    )}
                                    <button
                                        onClick={() => handleDismiss(executionStatus.execution_id, 'Closing PR — fixed via alternative method')}
                                        className="flex items-center gap-1.5 px-4 py-2 bg-red-600/10 hover:bg-red-600/20 border border-red-500/30 text-red-400 hover:text-red-300 rounded-lg text-sm font-medium transition-colors"
                                    >
                                        ✕ Close PR / Ignore
                                    </button>
                                </div>
                                {agentFixExpanded && (
                                    <div className="mt-2 bg-slate-900/60 border border-purple-500/20 rounded-lg p-4 space-y-3">
                                        <p className="text-xs text-orange-300/80">
                                            ⚠ This will fix Azure directly. Close or ignore the open PR afterwards to avoid drift.
                                        </p>
                                        {agentFixLoading ? (
                                            <p className="text-xs text-slate-500 animate-pulse">Generating execution plan…</p>
                                        ) : (
                                            <>
                                                <AgentFixPlanView plan={agentFixPreview} />
                                                <div className="flex gap-2">
                                                    <button
                                                        onClick={() => handleAgentFixExecute(executionStatus.execution_id)}
                                                        disabled={agentFixExecuting}
                                                        className="flex items-center gap-1.5 px-4 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                                                    >
                                                        {agentFixExecuting ? <>Running…</> : <>▶ Run</>}
                                                    </button>
                                                    {!agentFixExecuting && !terminalLines.length && (
                                                        <button
                                                            onClick={() => setAgentFixExpanded(false)}
                                                            className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded-lg text-sm font-medium transition-colors"
                                                        >
                                                            Cancel
                                                        </button>
                                                    )}
                                                </div>
                                                {terminalLines.length > 0 && (
                                                    <AgentTerminal lines={terminalLines} running={agentFixExecuting} />
                                                )}
                                            </>
                                        )}
                                    </div>
                                )}
                            </div>
                        )}

                        {/* Notes / error message */}
                        {executionStatus.notes && (
                            <p className="text-xs text-slate-400 italic">{executionStatus.notes}</p>
                        )}

                        {/* Execution log + verification for applied/failed records */}
                        {(executionStatus.status === 'applied' || executionStatus.status === 'failed') && (
                            <ExecutionLogView
                                steps={executionStatus.execution_log}
                                verification={executionStatus.verification}
                            />
                        )}

                        {/* Rollback failed — covers: backend steps failed, LLM no-op, and network/API errors */}
                        {rollbackResult && rollbackResult.status !== 'rolled_back' && executionStatus.status === 'applied' && (
                            <div className="space-y-2">
                                <div className="text-xs rounded-lg px-3 py-2 border bg-rose-500/10 border-rose-500/30 text-rose-300">
                                    ↩ Rollback attempted but failed — fix is still applied.
                                    {(executionStatus.rollback_log?.length > 0 || rollbackResult.rollback_log?.length > 0)
                                        ? ' Review the steps below and retry manually.'
                                        : rollbackResult.notes
                                            ? ` Error: ${rollbackResult.notes}`
                                            : ' The rollback agent did not complete — retry or roll back manually.'}
                                </div>
                                {(executionStatus.rollback_log?.length > 0 || rollbackResult.rollback_log?.length > 0) && (
                                    <ExecutionLogView
                                        steps={executionStatus.rollback_log ?? rollbackResult.rollback_log}
                                        verification={null}
                                        label="Rollback Steps"
                                    />
                                )}
                            </div>
                        )}

                        {/* Rollback result log — success */}
                        {executionStatus.status === 'rolled_back' && (
                            <div className="space-y-2">
                                <div className="text-xs rounded-lg px-3 py-2 border bg-amber-500/10 border-amber-500/30 text-amber-300">
                                    ↩ Fix rolled back successfully. Resource returned to pre-fix state.
                                </div>
                                <ExecutionLogView
                                    steps={executionStatus.rollback_log}
                                    verification={null}
                                    label="Rollback Steps"
                                />
                            </div>
                        )}

                        {/* Conditions panel — conditional (APPROVED_IF) */}
                        {executionStatus.status === 'conditional' && executionStatus.conditions && executionStatus.conditions.length > 0 && (
                            <div className="space-y-3 pt-1">
                                <p className="text-xs text-amber-300/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
                                    This action is approved but execution is gated on the following conditions.
                                    Auto-checkable conditions are evaluated every 60 seconds by the condition watcher.
                                </p>
                                <div className="space-y-2">
                                    {executionStatus.conditions.map((cond, idx) => (
                                        <div
                                            key={idx}
                                            className={`flex items-start gap-3 rounded-lg border px-3 py-2.5 text-xs ${
                                                cond.satisfied
                                                    ? 'bg-emerald-500/5 border-emerald-500/30'
                                                    : 'bg-slate-800/60 border-slate-600/40'
                                            }`}
                                        >
                                            <span className="mt-0.5 text-base shrink-0">
                                                {cond.satisfied ? '✅' : cond.auto_checkable ? '🔄' : '👤'}
                                            </span>
                                            <div className="flex-1 min-w-0">
                                                <div className={`font-semibold ${cond.satisfied ? 'text-emerald-300' : 'text-slate-200'}`}>
                                                    {cond.description}
                                                </div>
                                                <div className="text-slate-400 mt-0.5">
                                                    {cond.auto_checkable ? 'Auto-checkable' : 'Requires human confirmation'}
                                                    {cond.satisfied && cond.satisfied_by && (
                                                        <span className="ml-2 text-emerald-400">
                                                            — satisfied by {cond.satisfied_by}
                                                        </span>
                                                    )}
                                                </div>
                                            </div>
                                            {!cond.satisfied && !cond.auto_checkable && (
                                                <button
                                                    onClick={() => { setSatisfyWhoDraft(''); setShowSatisfyInput(idx) }}
                                                    className="shrink-0 text-xs px-2.5 py-1 rounded bg-amber-500/20 border border-amber-400/30 text-amber-300 hover:bg-amber-500/30 transition-colors"
                                                >
                                                    Confirm
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        {/* Action panel — manual_required (APPROVED) + awaiting_review (ESCALATED) */}
                        {(executionStatus.status === 'manual_required' || executionStatus.status === 'awaiting_review') && (
                            <div className="space-y-3 pt-1">
                                <p className="text-xs text-orange-300/80 bg-orange-500/5 border border-orange-500/20 rounded-lg px-3 py-2">
                                    {executionStatus.status === 'awaiting_review'
                                        ? 'This action was escalated for review. Choose how to remediate (your choice acts as approval):'
                                        : 'This verdict was approved. Choose how to remediate:'}
                                </p>
                                {createPrError && (
                                    <p className="text-xs text-rose-300 bg-rose-500/10 border border-rose-500/30 rounded-lg px-3 py-2">
                                        ⚠ {createPrError}
                                    </p>
                                )}

                                {/* Four action buttons */}
                                <div className="flex flex-wrap gap-2">
                                    <button
                                        onClick={handleOpenPROverlay}
                                        disabled={createPrLoading}
                                        className="flex items-center gap-1.5 px-4 py-2 bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/40 text-blue-300 hover:text-blue-200 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                                    >
                                        {createPrLoading ? (
                                            <><span className="w-3.5 h-3.5 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" /> Creating PR…</>
                                        ) : (
                                            <>📝 Create Terraform PR</>
                                        )}
                                    </button>

                                    {ev.resource_id && (
                                        <a
                                            href={`https://portal.azure.com/#resource${ev.resource_id.startsWith('/') ? ev.resource_id : '/' + ev.resource_id}`}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="flex items-center gap-1.5 px-4 py-2 bg-slate-700/50 hover:bg-slate-700 border border-slate-500/40 text-slate-300 hover:text-slate-100 rounded-lg text-sm font-medium transition-colors no-underline"
                                        >
                                            🌐 Open in Azure Portal
                                        </a>
                                    )}

                                    <button
                                        onClick={() => handleAgentFixPreview(executionStatus.execution_id)}
                                        className="flex items-center gap-1.5 px-4 py-2 bg-purple-600/20 hover:bg-purple-600/30 border border-purple-500/40 text-purple-300 hover:text-purple-200 rounded-lg text-sm font-medium transition-colors"
                                    >
                                        🤖 {agentFixExpanded ? 'Hide' : 'Fix using'} Agent
                                    </button>

                                    <button
                                        onClick={() => handleDismiss(executionStatus.execution_id, 'Declined — not applicable')}
                                        className="flex items-center gap-1.5 px-4 py-2 bg-red-600/10 hover:bg-red-600/20 border border-red-500/30 text-red-400 hover:text-red-300 rounded-lg text-sm font-medium transition-colors"
                                    >
                                        ✕ Decline / Ignore
                                    </button>
                                </div>

                                {/* Agent Fix — expandable preview + execute panel */}
                                {agentFixExpanded && (
                                    <div className="mt-2 bg-slate-900/60 border border-purple-500/20 rounded-lg p-4 space-y-3">
                                        <p className="text-xs text-orange-300/80 flex items-center gap-1.5">
                                            ⚠ These commands will modify your Azure environment. Review carefully before running.
                                        </p>

                                        {agentFixLoading ? (
                                            <p className="text-xs text-slate-500 animate-pulse">Generating execution plan…</p>
                                        ) : (
                                            <>
                                                <AgentFixPlanView plan={agentFixPreview} />

                                                <div className="flex gap-2">
                                                    <button
                                                        onClick={() => handleAgentFixExecute(executionStatus.execution_id)}
                                                        disabled={agentFixExecuting}
                                                        className="flex items-center gap-1.5 px-4 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                                                    >
                                                        {agentFixExecuting ? (
                                                            <><span className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" /> Running…</>
                                                        ) : (
                                                            <>▶ Run</>
                                                        )}
                                                    </button>
                                                    <button
                                                        onClick={() => setAgentFixExpanded(false)}
                                                        className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded-lg text-sm font-medium transition-colors"
                                                    >
                                                        Cancel
                                                    </button>
                                                </div>

                                                {agentFixResult && (
                                                    <>
                                                        <div className={`text-xs rounded-lg px-3 py-2 border ${
                                                            agentFixResult.status === 'applied'
                                                                ? 'bg-green-500/10 border-green-500/30 text-green-300'
                                                                : 'bg-red-500/10 border-red-500/30 text-red-300'
                                                        }`}>
                                                            {agentFixResult.status === 'applied' ? 'Fix applied successfully.' : 'Fix failed.'} {agentFixResult.notes}
                                                        </div>
                                                        <ExecutionLogView steps={agentFixResult.execution_log} verification={agentFixResult.verification} />
                                                    </>
                                                )}
                                            </>
                                        )}
                                    </div>
                                )}
                            </div>
                        )}

                        {/* Execution ID for audit */}
                        <p className="text-xs text-slate-600 font-mono">
                            ID: {executionStatus.execution_id}
                        </p>
                    </div>
                ) : (
                    <div className="space-y-1">
                        <p className="text-sm text-slate-500">
                            No execution record.
                        </p>
                        <p className="text-xs text-slate-600">
                            {executionStatus?.gateway_enabled === false
                                ? <>Set <code className="text-slate-400">EXECUTION_GATEWAY_ENABLED=true</code> to enable Terraform PR generation for APPROVED verdicts.</>
                                : 'This verdict was processed before the gateway was enabled. Run a new scan to generate an execution record.'
                            }
                        </p>
                    </div>
                )}
            </div>

            {/* ═══════════════════════════════════════════════════════════════
          Section 7 — Tier 3 Remediation Playbook (Phase 34D)
          ═══════════════════════════════════════════════════════════════ */}
            <PlaybookPanel decisionId={ev.action_id} reviewedBy={reviewedBy} />

            {/* ═══════════════════════════════════════════════════════════════
          Section 8 — Audit Trail + Override History (side-by-side on lg+)
          ═══════════════════════════════════════════════════════════════ */}
            <div className="grid lg:grid-cols-2 gap-6">
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

                {/* Override History sits alongside Audit Trail */}
                <OverrideHistoryPanel
                    actionType={ev.action_type ?? ev.proposed_action?.action_type}
                    resourceType={ev.resource_type ?? ev.proposed_action?.target?.resource_type}
                />
            </div>

            {/* Satisfy condition inline modal */}
            {showSatisfyInput !== null && (
                <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick={() => setShowSatisfyInput(null)}>
                    <div className="bg-slate-800 border border-slate-600 rounded-xl p-5 w-full max-w-md space-y-4 shadow-2xl" onClick={e => e.stopPropagation()}>
                        <h3 className="text-sm font-semibold text-slate-200">Confirm condition — enter your name or email</h3>
                        <textarea
                            autoFocus
                            rows={2}
                            value={satisfyWhoDraft}
                            onChange={e => setSatisfyWhoDraft(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) confirmSatisfy() }}
                            placeholder="Your name or email (for audit trail)"
                            className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-slate-500 resize-none"
                        />
                        <div className="flex gap-2 justify-end">
                            <button onClick={() => setShowSatisfyInput(null)} className="px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200 transition-colors">Cancel</button>
                            <button onClick={confirmSatisfy} className="px-4 py-1.5 text-sm bg-amber-600/20 hover:bg-amber-600/30 border border-amber-500/40 text-amber-300 hover:text-amber-200 rounded-lg font-medium transition-colors">
                                Confirm Condition
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Dismiss reason inline modal */}
            {showDismissInput && (
                <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick={() => setShowDismissInput(false)}>
                    <div className="bg-slate-800 border border-slate-600 rounded-xl p-5 w-full max-w-md space-y-4 shadow-2xl" onClick={e => e.stopPropagation()}>
                        <h3 className="text-sm font-semibold text-slate-200">Dismiss — add reason (optional)</h3>
                        <textarea
                            autoFocus
                            rows={3}
                            value={dismissReasonDraft}
                            onChange={e => setDismissReasonDraft(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) confirmDismiss() }}
                            placeholder="Why is this being dismissed?"
                            className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-slate-500 resize-none"
                        />
                        <div className="flex gap-2 justify-end">
                            <button onClick={() => setShowDismissInput(false)} className="px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200 transition-colors">Cancel</button>
                            <button onClick={confirmDismiss} className="px-4 py-1.5 text-sm bg-red-600/20 hover:bg-red-600/30 border border-red-500/40 text-red-300 hover:text-red-200 rounded-lg font-medium transition-colors">
                                Confirm Dismiss
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Terraform PR overlay — rendered at fixed position, outside scroll flow */}
            {showPROverlay && executionStatus && (
                <TerraformPROverlay
                    executionId={executionStatus.execution_id ?? ''}
                    actionType={ev?.proposed_action?.action_type ?? ''}
                    detectedRepo={executionStatus.iac_repo ?? ''}
                    detectedPath={executionStatus.iac_path ?? ''}
                    loading={createPrLoading}
                    onConfirm={(repo, path, confirmedChange) => handleCreatePR(executionStatus.execution_id, repo, path, confirmedChange)}
                    onCancel={() => { setShowPROverlay(false); setCreatePrError(null) }}
                />
            )}
        </div>
    )
}

// ── Execution status badge ──────────────────────────────────────────────────

const EXEC_STATUS_CONFIG = {
    pending:         { label: 'Pending',          color: 'text-slate-400',  bg: 'bg-slate-500/10',  border: 'border-slate-500/30' },
    blocked:         { label: 'Blocked',           color: 'text-red-400',    bg: 'bg-red-500/10',    border: 'border-red-500/30' },
    awaiting_review: { label: 'Awaiting Review',   color: 'text-yellow-400', bg: 'bg-yellow-500/10', border: 'border-yellow-500/30' },
    conditional:     { label: 'Conditional',       color: 'text-amber-300',  bg: 'bg-amber-500/10',  border: 'border-amber-400/30' },
    pr_created:      { label: 'PR Created',        color: 'text-blue-400',   bg: 'bg-blue-500/10',   border: 'border-blue-500/30' },
    pr_merged:       { label: 'PR Merged',         color: 'text-green-400',  bg: 'bg-green-500/10',  border: 'border-green-500/30' },
    applied:         { label: 'Applied',           color: 'text-green-400',  bg: 'bg-green-500/10',  border: 'border-green-500/30' },
    manual_required: { label: 'Manual Required',   color: 'text-orange-400', bg: 'bg-orange-500/10', border: 'border-orange-500/30' },
    dismissed:       { label: 'Dismissed',         color: 'text-slate-400',  bg: 'bg-slate-500/10',  border: 'border-slate-500/30' },
    failed:          { label: 'Failed',            color: 'text-red-400',    bg: 'bg-red-500/10',    border: 'border-red-500/30' },
    rolled_back:     { label: 'Rolled Back',       color: 'text-amber-400',  bg: 'bg-amber-500/10',  border: 'border-amber-500/30' },
}

function ExecutionStatusBadge({ status }) {
    const cfg = EXEC_STATUS_CONFIG[status] ?? EXEC_STATUS_CONFIG.pending
    return (
        <span className={`text-xs font-semibold px-3 py-1 rounded-full border ${cfg.color} ${cfg.bg} ${cfg.border}`}>
            {cfg.label}
        </span>
    )
}
