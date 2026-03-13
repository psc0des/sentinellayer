/**
 * Alerts.jsx — Azure Monitor alert investigation dashboard.
 *
 * Shows one row per fired alert with real-time status updates.
 * Each row: resource, metric, value/threshold, severity, when it fired,
 * investigation status, and governance outcome.
 *
 * Click a row → drilldown panel with agent findings, SRI scores,
 * policy violations, governance rationale, and action buttons
 * (Create Terraform PR, Fix by Agent, Open in Portal, Decline/Ignore).
 */

import React, { useState, useMemo, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useOutletContext } from 'react-router-dom'
import {
  Search, X, ChevronRight, Clock, CheckCircle,
  AlertTriangle, XCircle, Activity, Copy, Check, Zap, Download,
} from 'lucide-react'
import VerdictBadge from '../components/magicui/VerdictBadge'
import {
  streamAlertEvents,
  approveExecution,
  createPRFromManual,
  dismissExecution,
  executeAgentFix,
  fetchAgentFixPreview,
  rollbackAgentFix,
} from '../api'

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function formatRelative(iso) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  if (isNaN(diff)) return ''
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

function shortResource(id) {
  return id?.split('/').filter(Boolean).pop() ?? id ?? '—'
}

// ── Severity helpers ─────────────────────────────────────────────────────────

function severityColor(sev) {
  const s = String(sev)
  if (s === '0' || s === '1') return 'text-rose-400'
  if (s === '2') return 'text-amber-400'
  return 'text-blue-400'
}

function severityBg(sev) {
  const s = String(sev)
  if (s === '0' || s === '1') return 'bg-rose-500/10 border-rose-500/30'
  if (s === '2') return 'bg-amber-500/10 border-amber-500/30'
  return 'bg-blue-500/10 border-blue-500/30'
}

function severityLabel(sev) {
  const map = { '0': 'Critical', '1': 'Error', '2': 'Warning', '3': 'Info', '4': 'Verbose' }
  return map[String(sev)] ?? `Sev ${sev}`
}

// ── Status helpers ───────────────────────────────────────────────────────────

function statusIcon(status) {
  if (status === 'resolved')      return <CheckCircle  className="w-3.5 h-3.5 text-emerald-400" />
  if (status === 'error')         return <XCircle      className="w-3.5 h-3.5 text-rose-400" />
  if (status === 'investigating') return <Activity     className="w-3.5 h-3.5 text-blue-400 animate-pulse" />
  return <Zap className="w-3.5 h-3.5 text-amber-400 animate-pulse" />
}

function statusLabel(status) {
  const map = { firing: 'Firing', investigating: 'Investigating', resolved: 'Investigated', error: 'Error' }
  return map[status] ?? status ?? '—'
}

function statusColor(status) {
  if (status === 'resolved')      return 'text-emerald-400'
  if (status === 'error')         return 'text-rose-400'
  if (status === 'investigating') return 'text-blue-400'
  return 'text-amber-400'
}

// ── SRI colour ───────────────────────────────────────────────────────────────

function sriColor(score) {
  if (score <= 25) return 'text-emerald-400'
  if (score <= 60) return 'text-amber-400'
  return 'text-rose-400'
}

// ── Execution status label ───────────────────────────────────────────────────

const EXEC_STATUS_CONFIG = {
  pending:         { label: 'Pending',          color: 'text-slate-400',   bg: 'bg-slate-500/10',   border: 'border-slate-500/30' },
  blocked:         { label: 'Blocked',           color: 'text-rose-400',    bg: 'bg-rose-500/10',    border: 'border-rose-500/30' },
  awaiting_review: { label: 'Awaiting Review',   color: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/30' },
  manual_required: { label: 'Action Required',   color: 'text-orange-400',  bg: 'bg-orange-500/10',  border: 'border-orange-500/30' },
  pr_created:      { label: 'PR Created',        color: 'text-blue-400',    bg: 'bg-blue-500/10',    border: 'border-blue-500/30' },
  pr_merged:       { label: 'PR Merged',         color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/30' },
  applied:         { label: 'Applied',           color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/30' },
  dismissed:       { label: 'Dismissed',         color: 'text-slate-400',   bg: 'bg-slate-500/10',   border: 'border-slate-500/30' },
  failed:          { label: 'Failed',            color: 'text-rose-400',    bg: 'bg-rose-500/10',    border: 'border-rose-500/30' },
  rolled_back:     { label: 'Rolled Back',       color: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/30' },
}

function ExecStatusBadge({ status }) {
  const cfg = EXEC_STATUS_CONFIG[status] ?? EXEC_STATUS_CONFIG.pending
  return (
    <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border ${cfg.color} ${cfg.bg} ${cfg.border}`}>
      {cfg.label}
    </span>
  )
}

// ── CopyButton ───────────────────────────────────────────────────────────────

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={() => navigator.clipboard.writeText(text).then(() => {
        setCopied(true); setTimeout(() => setCopied(false), 1500)
      })}
      className="ml-1 text-slate-500 hover:text-slate-300 transition-colors flex-shrink-0"
    >
      {copied
        ? <Check className="w-3.5 h-3.5 text-emerald-400" />
        : <Copy  className="w-3.5 h-3.5" />}
    </button>
  )
}

// ── Export helpers ────────────────────────────────────────────────────────────

function exportCSV(rows) {
  const cols = ['alert_id', 'status', 'resource_name', 'metric', 'value', 'threshold', 'severity', 'fired_at', 'resolved_at']
  const header = cols.join(',')
  const lines  = rows.map(r => cols.map(c => JSON.stringify(r[c] ?? '')).join(','))
  const blob   = new Blob([header + '\n' + lines.join('\n')], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a'); a.href = url; a.download = 'ruriskry-alerts.csv'; a.click()
  URL.revokeObjectURL(url)
}

// ── AgentFixPlanView ─────────────────────────────────────────────────────────
// Renders the structured execution plan returned by the LLM-driven agent.

function AgentFixPlanView({ plan }) {
  if (!plan) return null
  const hasSteps = Array.isArray(plan.steps) && plan.steps.length > 0
  return (
    <div className="space-y-2">
      {plan.summary && (
        <p className="text-xs text-slate-300 font-medium">{plan.summary}</p>
      )}
      {hasSteps ? (
        <div className="overflow-x-auto rounded border border-slate-700">
          <table className="w-full text-xs text-slate-300">
            <thead>
              <tr className="bg-slate-800/80 text-slate-400 uppercase text-[10px] tracking-wider">
                <th className="px-2 py-1.5 text-left w-6">#</th>
                <th className="px-2 py-1.5 text-left">Operation</th>
                <th className="px-2 py-1.5 text-left">Target</th>
                <th className="px-2 py-1.5 text-left">Reason</th>
              </tr>
            </thead>
            <tbody>
              {plan.steps.map((step, i) => (
                <tr key={i} className="border-t border-slate-700/60 hover:bg-slate-800/30">
                  <td className="px-2 py-1.5 text-slate-500">{i + 1}</td>
                  <td className="px-2 py-1.5 font-mono text-purple-300">{step.operation}</td>
                  <td className="px-2 py-1.5 text-slate-400 max-w-[160px] truncate" title={step.target}>{step.target}</td>
                  <td className="px-2 py-1.5 text-slate-400">{step.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-xs text-slate-500 italic">No steps — resource may already be in the desired state.</p>
      )}
      {plan.estimated_impact && (
        <p className="text-xs text-amber-400/80">⚡ Impact: {plan.estimated_impact}</p>
      )}
      {plan.rollback_hint && (
        <p className="text-xs text-slate-500">↩ Rollback: <code className="text-slate-400">{plan.rollback_hint}</code></p>
      )}
      {Array.isArray(plan.commands) && plan.commands.length > 0 && (
        <details>
          <summary className="text-[10px] text-slate-500 cursor-pointer hover:text-slate-400 select-none">Equivalent CLI commands</summary>
          <pre className="mt-1 text-xs text-slate-400 bg-slate-900 rounded p-2 overflow-x-auto border border-slate-700/50 whitespace-pre-wrap">
            {plan.commands.map(cmd => `$ ${cmd}`).join('\n')}
          </pre>
        </details>
      )}
    </div>
  )
}

// ── ExecutionLogView ─────────────────────────────────────────────────────────

function ExecutionLogView({ steps, verification, label = 'Execution Log' }) {
  if (!steps?.length && !verification) return null
  return (
    <div className="space-y-2 mt-2">
      {steps?.length > 0 && (
        <details open>
          <summary className="text-[10px] text-slate-500 cursor-pointer hover:text-slate-400 select-none uppercase tracking-wide font-semibold">
            {label} ({steps.length} step{steps.length !== 1 ? 's' : ''})
          </summary>
          <div className="mt-1.5 rounded border border-slate-700/60 overflow-hidden">
            {steps.map((step, i) => (
              <div key={i} className={`flex items-start gap-2 px-3 py-2 text-xs border-b border-slate-800/60 last:border-0 ${step.success ? 'text-slate-300' : 'text-rose-300 bg-rose-500/5'}`}>
                <span className={`shrink-0 mt-0.5 ${step.success ? 'text-emerald-400' : 'text-rose-400'}`}>{step.success ? '✓' : '✗'}</span>
                <span className="font-mono text-violet-300 shrink-0">{step.operation ?? `step ${i + 1}`}</span>
                <span className="text-slate-400 min-w-0">{step.message}</span>
              </div>
            ))}
          </div>
        </details>
      )}
      {verification && (
        <div className={`flex items-start gap-2 text-xs rounded px-2 py-1.5 border ${verification.confirmed ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-300' : 'bg-amber-500/10 border-amber-500/25 text-amber-300'}`}>
          <span className="shrink-0">{verification.confirmed ? '✓' : '⚠'}</span>
          <div><span className="font-semibold mr-1">{verification.confirmed ? 'Verified:' : 'Unconfirmed:'}</span>{verification.message}</div>
        </div>
      )}
    </div>
  )
}

// ── AgentTerminal ─────────────────────────────────────────────────────────────

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
      <div className="flex items-center gap-1.5 px-3 py-2 bg-[#111611] border-b border-slate-700/30">
        <span className="w-2.5 h-2.5 rounded-full bg-rose-500/60" />
        <span className="w-2.5 h-2.5 rounded-full bg-amber-500/60" />
        <span className="w-2.5 h-2.5 rounded-full bg-emerald-500/60" />
        <span className="mx-auto text-slate-600 text-[10px] tracking-[0.2em] uppercase select-none">execution terminal</span>
        {running && (
          <span className="flex items-center gap-1.5 text-[10px] text-emerald-500">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            running
          </span>
        )}
      </div>
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

// ── AlertFindingActions ───────────────────────────────────────────────────────
// Action buttons for a single governance finding within an alert.
// Mirrors the execution panel in EvaluationDrilldown.jsx.

function AlertFindingActions({ execId, execStatusInitial, resourceId }) {
  const [execStatus, setExecStatus] = useState(execStatusInitial)
  const [createPrLoading, setCreatePrLoading] = useState(false)
  const [agentFixLoading, setAgentFixLoading] = useState(false)
  const [agentFixPreview, setAgentFixPreview] = useState(null)
  const [agentFixExpanded, setAgentFixExpanded] = useState(false)
  const [agentFixExecuting, setAgentFixExecuting] = useState(false)
  const [agentFixResult, setAgentFixResult] = useState(null)
  const [terminalLines, setTerminalLines] = useState([])
  const [prUrl, setPrUrl] = useState(null)
  const [error, setError] = useState(null)
  const [rollbackExecuting, setRollbackExecuting] = useState(false)
  const [rollbackResult, setRollbackResult] = useState(null)

  // Only render when there's an actionable execution record
  const actionable = ['manual_required', 'awaiting_review'].includes(execStatus)
  const prCreated  = execStatus === 'pr_created'
  const terminal   = ['applied', 'dismissed', 'pr_merged', 'rolled_back'].includes(execStatus)

  if (!execId) return null

  async function handleCreatePR() {
    setCreatePrLoading(true)
    setError(null)
    try {
      const updated = await createPRFromManual(execId)
      setExecStatus(updated.status)
      if (updated.pr_url) setPrUrl(updated.pr_url)
    } catch (err) {
      setError(`PR failed: ${err.message}`)
    } finally {
      setCreatePrLoading(false)
    }
  }

  async function handleAgentFixPreview() {
    if (agentFixPreview) { setAgentFixExpanded(e => !e); setTerminalLines([]); return }
    setAgentFixLoading(true)
    setAgentFixExpanded(true)
    setTerminalLines([])
    try {
      const data = await fetchAgentFixPreview(execId)
      setAgentFixPreview(data)
    } catch (err) {
      setAgentFixPreview({ commands: [`# Error: ${err.message}`], warning: '' })
    } finally {
      setAgentFixLoading(false)
    }
  }

  async function handleAgentFixExecute() {
    if (!window.confirm('This will run az CLI commands against your Azure environment. Continue?')) return
    const ts = () => new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    setAgentFixExecuting(true)
    setAgentFixResult(null)
    setTerminalLines([
      { type: 'init', text: `[${ts()}] ▶  Execution started` },
      { type: 'init', text: `[${ts()}] ▶  Connecting to Azure environment...` },
    ])

    let updated
    try {
      updated = await executeAgentFix(execId)
      setExecStatus(updated.status)
      setAgentFixResult(updated)
    } catch (err) {
      setTerminalLines(prev => [...prev,
        { type: 'error', text: `[${ts()}] ✗  Error: ${err.message}` },
      ])
      setAgentFixResult({ status: 'failed', notes: err.message })
      setAgentFixExecuting(false)
      return
    }

    const steps = updated.execution_log ?? []
    const animLines = []
    for (let i = 0; i < steps.length; i++) {
      const s = steps[i]
      animLines.push({ type: 'step',    text: `[${ts()}] ▶  [${i + 1}/${steps.length}] ${s.operation}` })
      animLines.push({ type: s.success ? 'success' : 'error',
                       text: `[${ts()}]    ${s.success ? '✓' : '✗'}  ${s.message}` })
    }
    if (updated.verification) {
      const v = updated.verification
      animLines.push({ type: 'info',     text: `[${ts()}] ▶  Running post-execution verification...` })
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

  async function handleRollback() {
    const hint = agentFixResult?.execution_plan?.rollback_hint
    const msg = hint
      ? `Roll back this fix?\n\nRollback operation:\n${hint}`
      : 'Roll back this fix? This will attempt to reverse the applied change.'
    if (!window.confirm(msg)) return
    setRollbackExecuting(true)
    setRollbackResult(null)
    try {
      const updated = await rollbackAgentFix(execId)
      setExecStatus(updated.status)
      setRollbackResult(updated)
    } catch (err) {
      setRollbackResult({ status: 'failed', notes: err.message, rollback_log: [] })
    } finally {
      setRollbackExecuting(false)
    }
  }

  async function handleDismiss() {
    const reason = window.prompt('Reason for dismissal (optional):', 'Alert finding dismissed') ?? ''
    try {
      const updated = await dismissExecution(execId, 'dashboard-user', reason)
      setExecStatus(updated.status)
    } catch (err) {
      setError(`Dismiss failed: ${err.message}`)
    }
  }

  const portalHref = resourceId
    ? `https://portal.azure.com/#resource${resourceId.startsWith('/') ? resourceId : '/' + resourceId}`
    : null

  return (
    <div className="mt-2 pt-2 border-t border-slate-700/60 space-y-2">
      {/* Execution status + PR link + Rollback */}
      <div className="flex items-center gap-2 flex-wrap">
        <ExecStatusBadge status={execStatus} />
        {prUrl && (
          <a href={prUrl} target="_blank" rel="noopener noreferrer"
            className="text-xs text-blue-400 hover:text-blue-300 underline">
            View PR →
          </a>
        )}
        {/* Rollback button — only for agent-applied fixes */}
        {execStatus === 'applied' && (
          <button
            onClick={handleRollback}
            disabled={rollbackExecuting}
            className="flex items-center gap-1 px-2.5 py-0.5 bg-amber-600/10 hover:bg-amber-600/20 border border-amber-500/30 text-amber-400 hover:text-amber-300 rounded text-[10px] font-medium transition-colors disabled:opacity-50"
          >
            {rollbackExecuting
              ? <><span className="w-2.5 h-2.5 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" /> Rolling back…</>
              : <>↩ Rollback</>}
          </button>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <p className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded px-2 py-1">{error}</p>
      )}

      {/* Terminal state — no buttons needed */}
      {terminal && execStatus !== 'rolled_back' && (
        <p className="text-xs text-slate-500 italic">
          {execStatus === 'dismissed' ? 'Finding dismissed.' : 'Fix has been applied.'}
        </p>
      )}

      {/* Rolled back state */}
      {execStatus === 'rolled_back' && (
        <div className="space-y-1">
          <p className="text-xs text-amber-400/80 bg-amber-500/5 border border-amber-500/20 rounded px-2 py-1">
            ↩ Fix rolled back. Resource returned to pre-fix state.
          </p>
          {rollbackResult?.rollback_log && (
            <ExecutionLogView steps={rollbackResult.rollback_log} verification={null} label="Rollback Steps" />
          )}
        </div>
      )}

      {/* Action buttons — manual_required or awaiting_review */}
      {actionable && (
        <div className="space-y-2">
          <p className="text-xs text-orange-300/80 bg-orange-500/5 border border-orange-500/20 rounded px-2 py-1.5">
            {execStatus === 'awaiting_review'
              ? 'Escalated for review — choose a remediation action:'
              : 'Governance approved — choose how to remediate:'}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {/* Create Terraform PR */}
            <button
              onClick={handleCreatePR}
              disabled={createPrLoading}
              className="flex items-center gap-1 px-3 py-1.5 bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/40 text-blue-300 hover:text-blue-200 rounded text-xs font-medium transition-colors disabled:opacity-50"
            >
              {createPrLoading
                ? <><span className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" /> Creating…</>
                : <>📝 Terraform PR</>}
            </button>

            {/* Fix by Agent */}
            <button
              onClick={handleAgentFixPreview}
              className="flex items-center gap-1 px-3 py-1.5 bg-purple-600/20 hover:bg-purple-600/30 border border-purple-500/40 text-purple-300 hover:text-purple-200 rounded text-xs font-medium transition-colors"
            >
              🤖 {agentFixExpanded ? 'Hide Fix' : 'Fix by Agent'}
            </button>

            {/* Open in Azure Portal */}
            {portalHref && (
              <a
                href={portalHref}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 px-3 py-1.5 bg-slate-700/50 hover:bg-slate-700 border border-slate-500/40 text-slate-300 hover:text-slate-100 rounded text-xs font-medium transition-colors no-underline"
              >
                🌐 Azure Portal
              </a>
            )}

            {/* Decline / Ignore */}
            <button
              onClick={handleDismiss}
              className="flex items-center gap-1 px-3 py-1.5 bg-rose-600/10 hover:bg-rose-600/20 border border-rose-500/30 text-rose-400 hover:text-rose-300 rounded text-xs font-medium transition-colors"
            >
              ✕ Ignore
            </button>
          </div>

          {/* Agent fix preview panel */}
          {agentFixExpanded && (
            <div className="bg-slate-900/60 border border-purple-500/20 rounded p-3 space-y-2">
              <p className="text-xs text-orange-300/80">
                ⚠ These commands will modify your Azure environment. Review before running.
              </p>
              {agentFixLoading ? (
                <p className="text-xs text-slate-500 animate-pulse">Generating execution plan…</p>
              ) : (
                <>
                  <AgentFixPlanView plan={agentFixPreview} />
                  <div className="flex gap-2">
                    <button
                      onClick={handleAgentFixExecute}
                      disabled={agentFixExecuting}
                      className="flex items-center gap-1 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-medium transition-colors disabled:opacity-50"
                    >
                      {agentFixExecuting ? <>Running…</> : <>▶ Run</>}
                    </button>
                    {!agentFixExecuting && !terminalLines.length && (
                      <button
                        onClick={() => setAgentFixExpanded(false)}
                        className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded text-xs font-medium transition-colors"
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

      {/* PR created state — alternative actions */}
      {prCreated && (
        <div className="space-y-2">
          <p className="text-xs text-blue-300/80 bg-blue-500/5 border border-blue-500/20 rounded px-2 py-1.5">
            A Terraform PR is open. Review and merge it, or use an alternative fix:
          </p>
          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={handleAgentFixPreview}
              className="flex items-center gap-1 px-3 py-1.5 bg-purple-600/20 hover:bg-purple-600/30 border border-purple-500/40 text-purple-300 rounded text-xs font-medium transition-colors"
            >
              🤖 {agentFixExpanded ? 'Hide Fix' : 'Fix by Agent instead'}
            </button>
            {portalHref && (
              <a href={portalHref} target="_blank" rel="noopener noreferrer"
                className="flex items-center gap-1 px-3 py-1.5 bg-slate-700/50 hover:bg-slate-700 border border-slate-500/40 text-slate-300 rounded text-xs font-medium no-underline transition-colors">
                🌐 Azure Portal
              </a>
            )}
            <button
              onClick={handleDismiss}
              className="flex items-center gap-1 px-3 py-1.5 bg-rose-600/10 hover:bg-rose-600/20 border border-rose-500/30 text-rose-400 rounded text-xs font-medium transition-colors"
            >
              ✕ Close PR / Ignore
            </button>
          </div>
          {/* Agent fix panel reused here too */}
          {agentFixExpanded && (
            <div className="bg-slate-900/60 border border-purple-500/20 rounded p-3 space-y-2">
              <p className="text-xs text-orange-300/80">⚠ Close or ignore the PR after running to avoid drift.</p>
              {agentFixLoading ? (
                <p className="text-xs text-slate-500 animate-pulse">Generating execution plan…</p>
              ) : (
                <>
                  <AgentFixPlanView plan={agentFixPreview} />
                  <div className="flex gap-2">
                    <button onClick={handleAgentFixExecute} disabled={agentFixExecuting}
                      className="flex items-center gap-1 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-medium disabled:opacity-50">
                      {agentFixExecuting ? <>Running…</> : <>▶ Run</>}
                    </button>
                    {!agentFixExecuting && !terminalLines.length && (
                      <button onClick={() => setAgentFixExpanded(false)}
                        className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded text-xs font-medium">Cancel</button>
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
    </div>
  )
}

// ── Alert drilldown panel ────────────────────────────────────────────────────

function AlertPanel({ alert, onClose }) {
  if (!alert) return null

  const proposals  = alert.proposals  ?? []
  const verdicts   = alert.verdicts   ?? []
  const totals     = alert.totals ?? {}

  return createPortal(
    <>
      {/* Backdrop */}
      <div
        style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 9998 }}
        onClick={onClose}
      />

      {/* Slide-in panel */}
      <div
        style={{ position: 'fixed', right: 0, top: 0, bottom: 0, width: '100%', maxWidth: '620px', zIndex: 9999 }}
        className="bg-slate-950 border-l border-slate-800 overflow-y-auto shadow-2xl flex flex-col"
      >
        {/* Header */}
        <div className="sticky top-0 bg-slate-950 border-b border-slate-800 px-5 py-4 flex items-start justify-between gap-3 z-10">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              {statusIcon(alert.status)}
              <p className={`text-xs font-semibold uppercase tracking-wide ${statusColor(alert.status)}`}>
                {statusLabel(alert.status)}
              </p>
              <span className={`text-xs font-bold px-1.5 py-0.5 rounded border ${severityBg(alert.severity)} ${severityColor(alert.severity)}`}>
                {severityLabel(alert.severity)}
              </span>
            </div>
            <p className="text-sm font-medium text-slate-200 font-mono truncate" title={alert.resource_id}>
              {alert.resource_name || shortResource(alert.resource_id)}
            </p>
            <p className="text-xs text-slate-500 mt-0.5">
              {alert.metric} — {alert.value != null ? `${alert.value}` : '?'} / {alert.threshold ?? '?'}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200 transition-colors flex-shrink-0 mt-0.5">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 px-5 py-4 space-y-5">

          {/* Timeline */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Timeline</h3>
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-3 space-y-2">
              <TimelineRow label="Fired"         time={alert.fired_at}          icon={<Zap         className="w-3 h-3 text-amber-400" />} />
              <TimelineRow label="Received"      time={alert.received_at}       icon={<Activity    className="w-3 h-3 text-slate-400" />} />
              <TimelineRow label="Investigating" time={alert.investigating_at}  icon={<Activity    className="w-3 h-3 text-blue-400" />} />
              <TimelineRow label="Investigated"   time={alert.resolved_at}       icon={<CheckCircle className="w-3 h-3 text-emerald-400" />} />
            </div>
          </section>

          {/* Outcome summary */}
          {alert.status === 'resolved' && (
            <section>
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Outcome Summary</h3>
              <div className="grid grid-cols-4 gap-2">
                <div className="bg-slate-800/60 border border-slate-700 rounded-lg p-2.5 text-center">
                  <p className="text-lg font-bold text-slate-200">{proposals.length}</p>
                  <p className="text-xs text-slate-500 mt-0.5">Findings</p>
                </div>
                <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-2.5 text-center">
                  <p className="text-lg font-bold text-emerald-400">{totals.approved ?? 0}</p>
                  <p className="text-xs text-slate-500 mt-0.5">Approved</p>
                </div>
                <div className="bg-amber-500/10 border border-amber-500/20 rounded-lg p-2.5 text-center">
                  <p className="text-lg font-bold text-amber-400">{totals.escalated ?? 0}</p>
                  <p className="text-xs text-slate-500 mt-0.5">Escalated</p>
                </div>
                <div className="bg-rose-500/10 border border-rose-500/20 rounded-lg p-2.5 text-center">
                  <p className="text-lg font-bold text-rose-400">{totals.denied ?? 0}</p>
                  <p className="text-xs text-slate-500 mt-0.5">Denied</p>
                </div>
              </div>
            </section>
          )}

          {/* Error message */}
          {alert.error && (
            <section>
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Error</h3>
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-lg p-3">
                <p className="text-xs text-rose-300 leading-relaxed">{alert.error}</p>
              </div>
            </section>
          )}

          {/* Agent findings + verdicts + action buttons */}
          {verdicts.length > 0 && (
            <section>
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">
                Agent Findings ({verdicts.length})
              </h3>
              <div className="space-y-3">
                {verdicts.map((v, idx) => {
                  const proposal    = proposals[idx] ?? {}
                  const resourceId  = v.proposed_action?.target?.resource_id
                                   ?? proposal.target?.resource_id
                                   ?? alert.resource_id
                                   ?? ''
                  const resourceName = shortResource(resourceId)
                  const decision    = v.decision ?? null
                  const sri         = v.skry_risk_index?.sri_composite ?? v.sri_composite ?? null
                  const reason      = proposal.reason ?? proposal.action_reason ?? v.reason ?? ''
                  const actionType  = proposal.action_type ?? v.action_type ?? ''
                  const violations  = v.violations ?? v.policy_violations ?? []
                  const execId      = v.execution_id ?? null
                  const execStatus  = v.execution_status ?? null

                  return (
                    <div key={idx} className="bg-slate-900 border border-slate-700 rounded-lg p-3 space-y-2">
                      {/* Finding header */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <AlertTriangle className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />
                          <span className="text-xs font-mono text-slate-200 truncate">{resourceName}</span>
                        </div>
                        <div className="flex items-center gap-2 flex-shrink-0">
                          {sri != null && (
                            <span className={`text-xs font-bold tabular-nums ${sriColor(sri)}`}>
                              {Number(sri).toFixed(1)}
                            </span>
                          )}
                          {decision && <VerdictBadge verdict={decision} />}
                        </div>
                      </div>

                      {/* Action type */}
                      {actionType && (
                        <p className="text-xs text-slate-500 capitalize">{String(actionType).replace(/_/g, ' ')}</p>
                      )}

                      {/* Agent reasoning */}
                      {reason && (
                        <p className="text-xs text-slate-400 leading-relaxed">{reason}</p>
                      )}

                      {/* Policy violations */}
                      {violations.length > 0 && (
                        <div className="flex flex-wrap gap-1 pt-0.5">
                          {violations.map((pv, vi) => (
                            <span key={vi} className="text-xs font-mono px-1.5 py-0.5 rounded bg-rose-500/10 border border-rose-500/30 text-rose-400">
                              {typeof pv === 'string' ? pv : pv.policy_id ?? pv.id ?? JSON.stringify(pv)}
                            </span>
                          ))}
                        </div>
                      )}

                      {/* ── Action buttons (the new addition) ── */}
                      <AlertFindingActions
                        execId={execId}
                        execStatusInitial={execStatus}
                        resourceId={resourceId}
                      />
                    </div>
                  )
                })}
              </div>
            </section>
          )}

          {/* No findings message */}
          {alert.status === 'resolved' && verdicts.length === 0 && (
            <section>
              <div className="bg-emerald-500/5 border border-emerald-500/20 rounded-lg p-4 text-center">
                <CheckCircle className="w-5 h-5 text-emerald-500 mx-auto mb-1" />
                <p className="text-xs text-emerald-400 font-medium">No actionable governance findings.</p>
                <p className="text-xs text-slate-500 mt-1">
                  The agent investigated the resource and determined no governance action is required.
                  This may indicate the alert was informational, the resource is intentionally in its
                  current state, or no policy violations were detected.
                </p>
              </div>
            </section>
          )}

          {/* Alert reference */}
          <section>
            <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Alert Reference</h3>
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-3 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500 flex-shrink-0">Alert ID</span>
                <div className="flex items-center min-w-0">
                  <span className="text-xs font-mono text-slate-400 truncate">{alert.alert_id}</span>
                  <CopyButton text={alert.alert_id} />
                </div>
              </div>
              {alert.resource_id && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-slate-500 flex-shrink-0">Resource</span>
                  <div className="flex items-center min-w-0">
                    <span className="text-xs font-mono text-slate-400 truncate" title={alert.resource_id}>
                      {alert.resource_id}
                    </span>
                    <CopyButton text={alert.resource_id} />
                  </div>
                </div>
              )}
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500">Metric</span>
                <span className="text-xs text-slate-400">{alert.metric || '—'}</span>
              </div>
              {(alert.value != null || alert.threshold != null) && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-slate-500">Value / Threshold</span>
                  <span className="text-xs text-slate-400 tabular-nums">
                    {alert.value ?? '?'} / {alert.threshold ?? '?'}
                  </span>
                </div>
              )}
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500">Severity</span>
                <span className={`text-xs font-bold ${severityColor(alert.severity)}`}>
                  {severityLabel(alert.severity)}
                </span>
              </div>
              {(alert.description || alert.alert_payload?.description) && (
                <div className="pt-1">
                  <span className="text-xs text-slate-500 block mb-1">Description</span>
                  <p className="text-xs text-slate-400 leading-relaxed">
                    {alert.description || alert.alert_payload?.description}
                  </p>
                </div>
              )}
            </div>
          </section>

        </div>
      </div>
    </>,
    document.body
  )
}

function TimelineRow({ label, time, icon }) {
  if (!time) return null
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        {icon}
        <span className="text-xs text-slate-500">{label}</span>
      </div>
      <span className="text-xs text-slate-400 tabular-nums">{formatTime(time)}</span>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function Alerts() {
  const { alerts = [], fetchAll } = useOutletContext()

  const [searchText,    setSearchText]    = useState('')
  const [filterStatus,  setFilterStatus]  = useState('all')
  const [filterSeverity,setFilterSeverity]= useState('all')
  const [selectedAlert, setSelectedAlert] = useState(null)

  // Subscribe to SSE for active alerts
  useEffect(() => {
    const active = alerts.filter(a => a.status === 'firing' || a.status === 'investigating')
    if (active.length === 0) return

    const sources = active.map(a => {
      const es = streamAlertEvents(a.alert_id)
      es.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data)
          if (event.event === 'alert_resolved' || event.event === 'alert_error') {
            fetchAll?.()
            es.close()
          }
        } catch { /* ignore parse errors */ }
      }
      es.onerror = () => es.close()
      return es
    })
    return () => sources.forEach(es => es.close())
  }, [alerts, fetchAll])

  // Keep selected alert in sync when data refreshes (so action buttons reflect new status)
  useEffect(() => {
    if (!selectedAlert) return
    const updated = alerts.find(a => a.alert_id === selectedAlert.alert_id)
    if (updated) setSelectedAlert(updated)
  }, [alerts]) // eslint-disable-line react-hooks/exhaustive-deps

  // Filter
  const filtered = useMemo(() => {
    const q = searchText.toLowerCase()
    return alerts.filter(a => {
      if (filterStatus !== 'all' && a.status !== filterStatus) return false
      if (filterSeverity !== 'all' && String(a.severity) !== filterSeverity) return false
      if (q) {
        const inResource = (a.resource_name ?? '').toLowerCase().includes(q)
          || (a.resource_id ?? '').toLowerCase().includes(q)
        const inMetric = (a.metric ?? '').toLowerCase().includes(q)
        const inId = (a.alert_id ?? '').toLowerCase().includes(q)
        if (!inResource && !inMetric && !inId) return false
      }
      return true
    })
  }, [alerts, filterStatus, filterSeverity, searchText])

  const activeCount = alerts.filter(a => a.status === 'firing' || a.status === 'investigating').length

  return (
    <div className="p-6 space-y-5">

      {/* Header */}
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Alerts</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {alerts.length} total
            {activeCount > 0 && (
              <span className="text-amber-400 ml-2">{activeCount} active</span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => exportCSV(filtered)} className="text-xs text-slate-500 hover:text-slate-300 flex items-center gap-1 px-2 py-1 rounded border border-slate-800 hover:border-slate-700 transition-colors">
            <Download className="w-3 h-3" /> CSV
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-500 pointer-events-none" />
          <input
            type="text"
            placeholder="Search resource, metric, ID…"
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
            className="w-full bg-slate-900 border border-slate-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:ring-1 focus:ring-blue-500/50"
          />
        </div>

        <select
          value={filterStatus}
          onChange={e => setFilterStatus(e.target.value)}
          className="bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-300 focus:outline-none"
        >
          <option value="all">All statuses</option>
          <option value="firing">Firing</option>
          <option value="investigating">Investigating</option>
          <option value="resolved">Investigated</option>
          <option value="error">Error</option>
        </select>

        <select
          value={filterSeverity}
          onChange={e => setFilterSeverity(e.target.value)}
          className="bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-300 focus:outline-none"
        >
          <option value="all">All severities</option>
          <option value="0">Sev 0 — Critical</option>
          <option value="1">Sev 1 — Error</option>
          <option value="2">Sev 2 — Warning</option>
          <option value="3">Sev 3 — Info</option>
          <option value="4">Sev 4 — Verbose</option>
        </select>

        {filtered.length !== alerts.length && (
          <span className="text-xs text-slate-500">{filtered.length} of {alerts.length}</span>
        )}
      </div>

      {/* Table */}
      {filtered.length === 0 ? (
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-10 text-center">
          <Zap className="w-8 h-8 text-slate-700 mx-auto mb-2" />
          <p className="text-sm text-slate-500">No alerts recorded yet.</p>
          <p className="text-xs text-slate-600 mt-1">
            Configure an Azure Monitor Action Group to POST to <code className="text-slate-400">/api/alert-trigger</code>
          </p>
        </div>
      ) : (
        <div className="bg-slate-900/40 border border-slate-800 rounded-xl overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-slate-800 text-slate-500 text-left">
                <th className="px-4 py-2.5 font-semibold">Status</th>
                <th className="px-4 py-2.5 font-semibold">Resource</th>
                <th className="px-4 py-2.5 font-semibold">Metric</th>
                <th className="px-4 py-2.5 font-semibold text-center">Value</th>
                <th className="px-4 py-2.5 font-semibold text-center">Severity</th>
                <th className="px-4 py-2.5 font-semibold">Fired</th>
                <th className="px-4 py-2.5 font-semibold text-center">Outcome</th>
                <th className="w-8" />
              </tr>
            </thead>
            <tbody>
              {filtered.map(alert => {
                const isActive   = alert.status === 'firing' || alert.status === 'investigating'
                const isSelected = selectedAlert?.alert_id === alert.alert_id
                const totals     = alert.totals ?? {}
                // Show "Action Required" indicator if any finding needs remediation
                const hasAction  = (alert.verdicts ?? []).some(v =>
                  v.execution_status === 'manual_required' || v.execution_status === 'awaiting_review'
                )

                return (
                  <tr
                    key={alert.alert_id}
                    onClick={() => setSelectedAlert(alert)}
                    className={`border-b border-slate-800/60 cursor-pointer transition-colors ${
                      isSelected
                        ? 'bg-blue-500/8 ring-1 ring-inset ring-blue-500/20'
                        : 'hover:bg-slate-800/30'
                    } ${isActive ? 'bg-amber-500/[0.03]' : ''}`}
                  >
                    {/* Status */}
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        {statusIcon(alert.status)}
                        <span className={`font-medium ${statusColor(alert.status)}`}>
                          {statusLabel(alert.status)}
                        </span>
                        {hasAction && (
                          <span className="text-[10px] font-semibold text-orange-400 bg-orange-500/10 border border-orange-500/30 px-1.5 py-0.5 rounded-full ml-1">
                            Action
                          </span>
                        )}
                      </div>
                    </td>

                    {/* Resource */}
                    <td className="px-4 py-2.5">
                      <span className="font-mono text-slate-200 truncate block max-w-[200px]" title={alert.resource_id}>
                        {alert.resource_name || shortResource(alert.resource_id)}
                      </span>
                    </td>

                    {/* Metric */}
                    <td className="px-4 py-2.5 text-slate-400">
                      {alert.metric || '—'}
                    </td>

                    {/* Value / Threshold */}
                    <td className="px-4 py-2.5 text-center tabular-nums">
                      {alert.value != null ? (
                        <span className="text-slate-200">
                          {Number(alert.value).toFixed(1)}
                          <span className="text-slate-600"> / {alert.threshold ?? '?'}</span>
                        </span>
                      ) : '—'}
                    </td>

                    {/* Severity */}
                    <td className="px-4 py-2.5 text-center">
                      <span className={`font-bold ${severityColor(alert.severity)}`}>
                        {severityLabel(alert.severity)}
                      </span>
                    </td>

                    {/* Fired */}
                    <td className="px-4 py-2.5 text-slate-400 whitespace-nowrap">
                      <div>{formatTime(alert.fired_at)}</div>
                      <div className="text-slate-600 text-[10px]">{formatRelative(alert.fired_at)}</div>
                    </td>

                    {/* Outcome */}
                    <td className="px-4 py-2.5 text-center">
                      {alert.status === 'resolved' ? (
                        <div className="flex items-center justify-center gap-1.5">
                          {(totals.approved ?? 0) > 0 && <span className="text-emerald-400">✓{totals.approved}</span>}
                          {(totals.escalated ?? 0) > 0 && <span className="text-amber-400">⚠{totals.escalated}</span>}
                          {(totals.denied ?? 0) > 0 && <span className="text-rose-400">✗{totals.denied}</span>}
                          {(totals.approved ?? 0) === 0 && (totals.escalated ?? 0) === 0 && (totals.denied ?? 0) === 0 && (
                            <span className="text-emerald-500">Clean</span>
                          )}
                        </div>
                      ) : alert.status === 'error' ? (
                        <span className="text-rose-400">Failed</span>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>

                    {/* Chevron */}
                    <td className="pr-3 text-slate-600">
                      <ChevronRight className="w-3.5 h-3.5" />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Drilldown panel */}
      {selectedAlert && (
        <AlertPanel alert={selectedAlert} onClose={() => setSelectedAlert(null)} />
      )}
    </div>
  )
}
