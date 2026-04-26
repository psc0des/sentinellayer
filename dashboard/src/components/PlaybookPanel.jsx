/**
 * PlaybookPanel.jsx — Tier 3 remediation playbook for a governance decision.
 *
 * Displays the generated az CLI command (with copy button), rollback command,
 * risk level, estimated duration, downtime flag, and expected outcome.
 *
 * "Run via RuriSkry" button is intentionally disabled until Phase E lands.
 *
 * Props:
 *   decisionId — action_id UUID; used to fetch /api/decisions/{id}/playbook
 */

import React, { useEffect, useState } from 'react'
import { fetchPlaybook } from '../api'

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

// ── Main Component ─────────────────────────────────────────────────────────

export default function PlaybookPanel({ decisionId }) {
  const [playbook, setPlaybook]   = useState(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [notAvailable, setNotAvailable] = useState(false)

  useEffect(() => {
    if (!decisionId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setNotAvailable(false)
    fetchPlaybook(decisionId)
      .then(data => {
        if (cancelled) return
        if (data === null) {
          setNotAvailable(true)
        } else {
          setPlaybook(data)
        }
      })
      .catch(err => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [decisionId])

  return (
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
          A Tier 1 SDK tool may handle it directly, or this combination is not
          yet covered. Use <span className="text-blue-400">Fix using Agent</span> or the
          {' '}<span className="text-blue-400">Terraform PR</span> path above.
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
            {playbook.requires_downtime && (
              <span className="text-xs text-rose-400 bg-rose-500/10 border border-rose-500/25 rounded-md px-2 py-1">
                ⚠ Requires downtime
              </span>
            )}
            {!playbook.requires_downtime && (
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

          {/* Run via RuriSkry — disabled until Phase E */}
          <div className="pt-1 border-t border-slate-700/40">
            <button
              disabled
              title="Automated execution via RuriSkry is coming in Phase E"
              className="flex items-center gap-2 px-4 py-2 bg-slate-700/40 border border-slate-600/40 text-slate-500 rounded-lg text-sm font-medium cursor-not-allowed"
            >
              ▶ Run via RuriSkry
              <span className="text-[9px] text-slate-600 font-normal">— Phase E</span>
            </button>
            <p className="text-[10px] text-slate-600 mt-1.5">
              Copy the command above and run it manually, or use the Azure Portal.
            </p>
          </div>

        </div>
      )}
    </div>
  )
}
