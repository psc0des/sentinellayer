/**
 * PlaybookPanel.jsx — Tier 3 remediation playbook for a governance decision.
 *
 * Displays the generated az CLI command (with copy button), rollback command,
 * risk level, estimated duration, downtime flag, and expected outcome.
 *
 * Phase E: "Run as dry-run" and "Run live" buttons are functional.
 * Phase F: Clicking either button opens ConfirmationModal, which fetches the
 *          A2 Validator brief in parallel and gates execution until it arrives
 *          (or until the 5s validator timeout fires).
 *
 * Props:
 *   decisionId — action_id UUID; used to fetch /api/decisions/{id}/playbook
 *   reviewedBy  — logged-in user identity (passed from EvaluationDrilldown)
 */

import React, { useEffect, useRef, useState } from 'react'
import { executePlaybook, fetchPlaybook } from '../api'
import ConfirmationModal from './ConfirmationModal'

// ── Helpers ────────────────────────────────────────────────────────────────

const RISK_CONFIG = {
  low:    { color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/25', dot: 'bg-emerald-400' },
  medium: { color: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/25',   dot: 'bg-amber-400' },
  high:   { color: 'text-rose-400',    bg: 'bg-rose-500/10',    border: 'border-rose-500/25',    dot: 'bg-rose-400' },
}

function RiskBadge({ level }) {
  const cfg = RISK_CONFIG[level] ?? RISK_CONFIG.medium
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[10px] font-semibold uppercase ${cfg.color} ${cfg.bg} ${cfg.border}`}>
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cfg.dot}`} />
      {level} risk
    </span>
  )
}

function formatDuration(seconds) {
  if (seconds < 60) return `~${seconds}s`
  const mins = Math.round(seconds / 60)
  return `~${mins} min`
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <button
      onClick={copy}
      className="px-2 py-1 text-[10px] rounded border border-slate-600 text-slate-400 hover:border-slate-400 hover:text-slate-200 transition-colors"
    >
      {copied ? 'copied ✓' : 'copy'}
    </button>
  )
}

// ── Execution result terminal ─────────────────────────────────────────────

function ExecutionResult({ result, mode }) {
  const success = result.exit_code === 0 || result.exit_code === null
  const isDryRun = mode === 'dry_run'

  return (
    <div className={`rounded-lg border text-xs ${
      success
        ? 'bg-emerald-500/5 border-emerald-500/25'
        : 'bg-rose-500/5 border-rose-500/30'
    }`}>
      {/* Header row */}
      <div className={`flex items-center gap-2 px-3 py-2 border-b ${
        success ? 'border-emerald-500/20' : 'border-rose-500/20'
      }`}>
        <span>{success ? '✓' : '✗'}</span>
        <span className={`font-semibold ${success ? 'text-emerald-300' : 'text-rose-300'}`}>
          {isDryRun
            ? (success ? 'Dry-run validated — no changes made' : 'Dry-run validation failed')
            : (success ? 'Executed successfully' : 'Execution failed')}
        </span>
        {result.exit_code !== null && result.exit_code !== undefined && (
          <span className="ml-auto text-slate-500 font-mono">exit {result.exit_code}</span>
        )}
        {result.duration_ms && (
          <span className="text-slate-600 font-mono">{result.duration_ms}ms</span>
        )}
      </div>

      {/* stdout */}
      {result.stdout && (
        <pre className="px-3 py-2 text-emerald-300 whitespace-pre-wrap break-all leading-5 max-h-48 overflow-y-auto">
          {result.stdout}
        </pre>
      )}

      {/* stderr */}
      {result.stderr && (
        <pre className={`px-3 py-2 whitespace-pre-wrap break-all leading-5 max-h-48 overflow-y-auto ${
          success ? 'text-amber-300/80' : 'text-rose-300'
        }`}>
          {result.stderr}
        </pre>
      )}

      {/* notes (e.g. dry_run explanation) */}
      {result.notes && !result.stdout && !result.stderr && (
        <p className="px-3 py-2 text-slate-400 italic">{result.notes}</p>
      )}

      {/* Audit ID */}
      <div className="px-3 py-1.5 border-t border-slate-700/40 text-slate-600 font-mono text-[10px]">
        Audit ID: {result.execution_id}
      </div>
    </div>
  )
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function PlaybookPanel({ decisionId, reviewedBy }) {
  const [playbook, setPlaybook]         = useState(null)
  const [loading, setLoading]           = useState(true)
  const [error, setError]               = useState(null)
  const [notAvailable, setNotAvailable] = useState(false)

  const [executing, setExecuting]   = useState(false)  // true while waiting for executor API
  const [execMode, setExecMode]     = useState(null)   // 'live' | 'dry_run'
  const [execResult, setExecResult] = useState(null)   // AzPlaybookExecution record
  const [execError, setExecError]   = useState(null)

  // Phase F — ConfirmationModal state
  const [modalOpen, setModalOpen]       = useState(false)
  const [modalInitMode, setModalInitMode] = useState('live')

  useEffect(() => {
    if (!decisionId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setNotAvailable(false)
    fetchPlaybook(decisionId)
      .then(data => {
        if (cancelled) return
        if (data === null) setNotAvailable(true)
        else setPlaybook(data)
      })
      .catch(err => { if (!cancelled) setError(err.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [decisionId])

  // Opens the ConfirmationModal pre-set to the requested mode
  function handleOpenModal(mode) {
    setModalInitMode(mode)
    setModalOpen(true)
  }

  // Called by ConfirmationModal when the user clicks "Confirm and run" / "Confirm dry-run"
  async function handleConfirm(mode, brief) {
    setModalOpen(false)
    setExecuting(true)
    setExecMode(mode)
    setExecResult(null)
    setExecError(null)
    try {
      const result = await executePlaybook(decisionId, mode, reviewedBy || 'dashboard-user', brief)
      setExecResult(result)
    } catch (err) {
      setExecError(err.message)
    } finally {
      setExecuting(false)
    }
  }

  return (
    <>
    {/* Phase F — Confirmation + Validator Modal */}
    <ConfirmationModal
      open={modalOpen}
      onClose={() => setModalOpen(false)}
      onConfirm={handleConfirm}
      initialMode={modalInitMode}
      playbook={playbook}
      decisionId={decisionId}
    />

    <div className="bg-slate-800 rounded-xl border border-slate-700 p-6">
      <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-4">
        Remediation Playbook
      </h2>

      {loading && (
        <div className="flex items-center gap-2 py-2">
          <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-slate-400">Loading playbook…</span>
        </div>
      )}

      {error && (
        <p className="text-sm text-rose-400">Could not load playbook: {error}</p>
      )}

      {notAvailable && !loading && (
        <div className="rounded-lg border border-slate-700/60 bg-slate-900/40 px-4 py-3 text-xs text-slate-500">
          <span className="font-semibold text-slate-400 block mb-1">Playbook not available</span>
          No Tier 3 template exists for this action + resource combination.
          A Tier 1 SDK tool may handle it directly, or this combination is not yet covered.
          Use <span className="text-blue-400">Fix using Agent</span> or the{' '}
          <span className="text-blue-400">Terraform PR</span> path above.
        </div>
      )}

      {playbook && !loading && (
        <div className="space-y-4">

          {/* Meta row — risk, duration, downtime */}
          <div className="flex items-center gap-3 flex-wrap">
            <RiskBadge level={playbook.risk_level} />
            <span className="text-xs text-slate-400 bg-slate-700/40 border border-slate-600/40 rounded-md px-2 py-1">
              ⏱ {formatDuration(playbook.estimated_duration_seconds)}
            </span>
            {playbook.requires_downtime ? (
              <span className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/25 rounded-md px-2 py-1">
                ⚠ Requires downtime
              </span>
            ) : (
              <span className="text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/25 rounded-md px-2 py-1">
                ✓ No downtime
              </span>
            )}
          </div>

          {/* az command */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">
                Command
              </span>
              <CopyButton text={playbook.az_command} />
            </div>
            <pre className="text-xs text-emerald-300 bg-slate-950 rounded-lg p-3 overflow-x-auto border border-slate-700/50 whitespace-pre-wrap leading-relaxed">
              $ {playbook.az_command}
            </pre>
          </div>

          {/* Expected outcome */}
          <div>
            <span className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold block mb-1">
              Expected Outcome
            </span>
            <p className="text-xs text-slate-300 leading-relaxed">{playbook.expected_outcome}</p>
          </div>

          {/* Rollback command */}
          {playbook.rollback_command ? (
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">
                  Rollback Command
                </span>
                <CopyButton text={playbook.rollback_command} />
              </div>
              <pre className="text-xs text-amber-300 bg-slate-950 rounded-lg p-3 overflow-x-auto border border-slate-700/50 whitespace-pre-wrap leading-relaxed">
                $ {playbook.rollback_command}
              </pre>
            </div>
          ) : (
            <div className="text-xs text-slate-500 italic">
              ↩ No rollback — operation is idempotent or irreversible.
            </div>
          )}

          {/* ── Execution buttons ─────────────────────────────────────── */}
          <div className="pt-2 border-t border-slate-700/40 space-y-3">
            <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold">
              Run via RuriSkry
            </p>

            <div className="flex flex-wrap gap-2">
              {/* Dry-run — opens ConfirmationModal pre-set to dry_run */}
              <button
                onClick={() => handleOpenModal('dry_run')}
                disabled={executing}
                className="flex items-center gap-1.5 px-4 py-2 bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/40 text-blue-300 hover:text-blue-200 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
              >
                {executing && execMode === 'dry_run' ? (
                  <><span className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" /> Validating…</>
                ) : (
                  <>🔍 Run as dry-run</>
                )}
              </button>

              {/* Live — opens ConfirmationModal pre-set to live */}
              <button
                onClick={() => handleOpenModal('live')}
                disabled={executing}
                className="flex items-center gap-1.5 px-4 py-2 bg-green-600/20 hover:bg-green-600/30 border border-green-500/40 text-green-300 hover:text-green-200 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
              >
                {executing && execMode === 'live' ? (
                  <><span className="w-3 h-3 border-2 border-green-400 border-t-transparent rounded-full animate-spin" /> Running…</>
                ) : (
                  <>▶ Run live</>
                )}
              </button>
            </div>

            <p className="text-[10px] text-slate-600">
              Both buttons open the safety review modal. The A2 Validator will review the command
              (≤5s) before you confirm. Dry-run validates without making changes.
            </p>
          </div>

          {/* ── Execution error ───────────────────────────────────────── */}
          {execError && (
            <div className="text-xs rounded-lg px-3 py-2 border bg-rose-500/10 border-rose-500/30 text-rose-300">
              ✗ {execError}
            </div>
          )}

          {/* ── Execution result ──────────────────────────────────────── */}
          {execResult && !execError && (
            <ExecutionResult result={execResult} mode={execResult.mode} />
          )}

        </div>
      )}
    </div>
    </>
  )
}
