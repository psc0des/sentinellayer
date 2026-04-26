/**
 * ConfirmationModal.jsx — Phase 34F: A2 Validator safety brief + execution gate.
 *
 * Opens immediately when the user clicks "Run as dry-run" or "▶ Run live".
 * In parallel, calls POST /validate to fetch the A2 Validator brief.
 * The modal shows a spinner in the brief area while the validator thinks (≤5s).
 *
 * Layout:
 *   - Header: "About to run:" + az command
 *   - Body:   "What this does:" (brief summary), "Caveats:" (list), risk badge
 *   - Footer: [Cancel]   [Run as dry-run]   [Confirm and run]
 *               always     disabled until      disabled until
 *               active     brief arrives or    brief arrives or
 *                          unavailable         unavailable
 *
 * Safety invariants:
 *   - Cancel never calls the executor — just closes the modal.
 *   - Dry-run and Confirm buttons are disabled until the brief arrives OR
 *     validator_status is "unavailable" (amber warning enables them).
 *   - The brief is passed to executePlaybook() for postmortem audit linkage.
 *
 * Props:
 *   open         — boolean: whether the modal is shown
 *   onClose      — () => void: close without executing
 *   onConfirm    — (mode, brief) => void: user confirmed; brief may be null
 *   initialMode  — 'dry_run' | 'live': which button was clicked to open
 *   playbook     — Playbook object (for az_command display and argv)
 *   decisionId   — string: action_id UUID
 */

import React, { useEffect, useRef, useState } from 'react'
import { validatePlaybook } from '../api'

// ── Helpers ────────────────────────────────────────────────────────────────

const RISK_COLORS = {
  low:    { badge: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30', dot: 'bg-emerald-400' },
  medium: { badge: 'text-amber-400   bg-amber-500/10   border-amber-500/30',   dot: 'bg-amber-400' },
  high:   { badge: 'text-rose-400    bg-rose-500/10    border-rose-500/30',    dot: 'bg-rose-400' },
}

function RiskBadge({ level }) {
  const cfg = RISK_COLORS[level] ?? RISK_COLORS.medium
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded border text-[10px] font-semibold uppercase ${cfg.badge}`}>
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cfg.dot}`} />
      {level} risk
    </span>
  )
}

function Spinner({ size = 'sm' }) {
  const cls = size === 'sm' ? 'w-3.5 h-3.5 border-2' : 'w-4 h-4 border-2'
  return (
    <span className={`inline-block rounded-full border-slate-500 border-t-blue-400 animate-spin ${cls}`} />
  )
}

// ── Modal ──────────────────────────────────────────────────────────────────

export default function ConfirmationModal({
  open,
  onClose,
  onConfirm,
  initialMode = 'live',
  playbook,
  decisionId,
}) {
  const [brief, setBrief]         = useState(null)   // ValidatorBrief or null while loading
  const [loading, setLoading]     = useState(false)
  const [mode, setMode]           = useState(initialMode)
  const cancelledRef              = useRef(false)

  // Reset and fetch brief each time the modal opens
  useEffect(() => {
    if (!open) return
    cancelledRef.current = false
    setBrief(null)
    setLoading(true)
    setMode(initialMode)

    const argv = playbook?.executable_args ?? []
    validatePlaybook(decisionId, argv)
      .then(b => { if (!cancelledRef.current) setBrief(b) })
      .finally(() => { if (!cancelledRef.current) setLoading(false) })

    return () => { cancelledRef.current = true }
  }, [open, decisionId, initialMode, playbook])

  if (!open) return null

  const unavailable = brief?.validator_status === 'unavailable' || brief?.validator_status === 'timeout'
  // Buttons enabled once brief arrives (ok or unavailable) — never before
  const buttonsEnabled = !loading

  function handleConfirm(chosenMode) {
    onConfirm(chosenMode, brief)
  }

  return (
    // Backdrop
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      {/* Panel */}
      <div className="relative w-full max-w-lg mx-4 bg-slate-900 border border-slate-700 rounded-xl shadow-2xl overflow-hidden">

        {/* Header */}
        <div className="px-5 py-4 border-b border-slate-700/60 bg-slate-800/60">
          <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1">
            About to run
          </p>
          <pre className="text-xs text-emerald-300 font-mono whitespace-pre-wrap break-all leading-relaxed">
            $ {playbook?.az_command ?? '—'}
          </pre>
        </div>

        {/* Body */}
        <div className="px-5 py-4 space-y-4">

          {/* What this does */}
          <div>
            <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1.5">
              What this does
            </p>
            {loading ? (
              <div className="flex items-center gap-2 text-xs text-slate-400">
                <Spinner /> <span>Validator is reviewing the command…</span>
              </div>
            ) : unavailable ? (
              <div className="flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
                <span className="shrink-0">⚠</span>
                <span>{brief?.raw_text ?? 'Validator unavailable — review the command carefully.'}</span>
              </div>
            ) : (
              <p className="text-xs text-slate-300 leading-relaxed">
                {brief?.summary || brief?.raw_text || '—'}
              </p>
            )}
          </div>

          {/* Caveats */}
          {!loading && !unavailable && brief?.caveats?.length > 0 && (
            <div>
              <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1.5">
                Caveats
              </p>
              <ul className="space-y-1">
                {brief.caveats.map((c, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-slate-400">
                    <span className="text-amber-400 shrink-0 mt-0.5">•</span>
                    <span>{c}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Risk badge row */}
          {!loading && brief?.risk_level && (
            <div className="flex items-center gap-2">
              <RiskBadge level={brief.risk_level} />
              {brief.validator_status === 'ok' && (
                <span className="text-[10px] text-slate-500">reviewed by A2 Validator</span>
              )}
            </div>
          )}

          {/* Mode selector — shows only when both modes are relevant */}
          <div className="pt-1 border-t border-slate-700/40">
            <p className="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-2">
              Execution mode
            </p>
            <div className="flex gap-2">
              <button
                onClick={() => setMode('dry_run')}
                className={`flex-1 py-1.5 px-3 rounded-lg text-xs font-medium border transition-colors ${
                  mode === 'dry_run'
                    ? 'bg-blue-600/30 border-blue-500/60 text-blue-200'
                    : 'bg-slate-800 border-slate-600/40 text-slate-400 hover:border-slate-500'
                }`}
              >
                🔍 Dry-run (safe)
              </button>
              <button
                onClick={() => setMode('live')}
                className={`flex-1 py-1.5 px-3 rounded-lg text-xs font-medium border transition-colors ${
                  mode === 'live'
                    ? 'bg-green-600/30 border-green-500/60 text-green-200'
                    : 'bg-slate-800 border-slate-600/40 text-slate-400 hover:border-slate-500'
                }`}
              >
                ▶ Live (real change)
              </button>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="px-5 py-3 bg-slate-800/50 border-t border-slate-700/60 flex items-center justify-between gap-2">
          {/* Cancel — always active */}
          <button
            onClick={onClose}
            className="px-4 py-1.5 rounded-lg text-xs text-slate-400 border border-slate-600/60 hover:border-slate-400 hover:text-slate-200 transition-colors"
          >
            Cancel
          </button>

          {/* Confirm — disabled while loading */}
          <button
            onClick={() => handleConfirm(mode)}
            disabled={!buttonsEnabled}
            className={`px-5 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${
              !buttonsEnabled
                ? 'bg-slate-700/40 border-slate-600/30 text-slate-500 cursor-not-allowed'
                : mode === 'live'
                ? 'bg-green-600/20 hover:bg-green-600/30 border-green-500/50 text-green-200 hover:text-green-100'
                : 'bg-blue-600/20 hover:bg-blue-600/30 border-blue-500/50 text-blue-200 hover:text-blue-100'
            }`}
          >
            {loading ? (
              <span className="flex items-center gap-1.5"><Spinner /> Reviewing…</span>
            ) : mode === 'live' ? (
              '▶ Confirm and run live'
            ) : (
              '🔍 Confirm dry-run'
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
