/**
 * OnboardingModal.jsx — first-run setup guide for new installations.
 *
 * Shown once after admin account creation (Setup page → login).
 * Guides the user through two steps:
 *   1. Scan Inventory  — discover Azure resources via Resource Graph
 *   2. Run All Agents  — navigate to Agents page to trigger the first scan
 *
 * Dismissed via the X / "Skip for now" button; dismissal is stored in
 * localStorage so the modal never appears again in subsequent sessions.
 */

import React, { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { X, Server, Bot, CheckCircle, Loader2, ArrowRight, AlertTriangle } from 'lucide-react'
import { refreshInventory, fetchRefreshStatus } from '../api'

export default function OnboardingModal({ onClose }) {
  const navigate = useNavigate()

  const [step, setStep]               = useState(1)    // 1 | 2
  const [invStatus, setInvStatus]     = useState('idle') // idle | scanning | done | error
  const [resourceCount, setResourceCount] = useState(null)
  const [errorMsg, setErrorMsg]       = useState(null)
  const pollRef = useRef(null)

  // Clean up poll on unmount
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const handleScanInventory = async () => {
    setInvStatus('scanning')
    setErrorMsg(null)
    try {
      const { refresh_id } = await refreshInventory()
      pollRef.current = setInterval(async () => {
        try {
          const result = await fetchRefreshStatus(refresh_id)
          if (result.status === 'complete') {
            clearInterval(pollRef.current)
            pollRef.current = null
            setResourceCount(result.resource_count ?? null)
            setInvStatus('done')
            setStep(2)
          } else if (result.status === 'error') {
            clearInterval(pollRef.current)
            pollRef.current = null
            setInvStatus('error')
            setErrorMsg('Inventory scan encountered an error — you can still continue.')
            setTimeout(() => setStep(2), 2500)
          }
        } catch { /* keep polling on network hiccup */ }
      }, 2000)
    } catch (err) {
      setInvStatus('error')
      setErrorMsg(err.message || 'Failed to start inventory scan. Check your Azure credentials.')
    }
  }

  const handleRunAgents = () => {
    onClose()
    navigate('/agents')
  }

  return (
    <>
      {/* Backdrop — clicking closes (skip) */}
      <div
        className="fixed inset-0 z-50 bg-black/60"
        style={{ backdropFilter: 'blur(4px)' }}
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Modal */}
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
        <div
          className="relative w-full max-w-md rounded-2xl pointer-events-auto"
          style={{
            background: 'linear-gradient(145deg, #0d1b2e 0%, #080f1e 100%)',
            border: '1px solid rgba(59,130,246,0.25)',
            boxShadow: '0 30px 70px rgba(0,0,0,0.8), 0 0 50px rgba(59,130,246,0.06)',
          }}
          onClick={e => e.stopPropagation()}
        >
          {/* Close / Skip */}
          <button
            onClick={onClose}
            className="absolute top-4 right-4 w-7 h-7 flex items-center justify-center text-slate-500 hover:text-slate-200 hover:bg-slate-700/60 rounded-lg transition-colors"
            title="Skip onboarding"
          >
            <X className="w-4 h-4" />
          </button>

          {/* Step indicator */}
          <div className="flex items-center gap-2 px-6 pt-6 mb-1">
            {[1, 2].map(n => (
              <React.Fragment key={n}>
                <div
                  className="w-2 h-2 rounded-full transition-all duration-300"
                  style={{
                    background: step === n ? '#60a5fa' : step > n ? '#10b981' : 'rgba(71,85,105,0.5)',
                    transform: step === n ? 'scale(1.4)' : 'scale(1)',
                  }}
                />
                {n < 2 && (
                  <div
                    className="h-px w-10 transition-colors duration-300"
                    style={{ background: step > n ? '#10b981' : 'rgba(51,65,85,0.5)' }}
                  />
                )}
              </React.Fragment>
            ))}
            <span className="text-xs text-slate-600 ml-1 font-mono">
              {step} / 2
            </span>
          </div>

          <div className="px-6 pb-6 pt-4">
            {step === 1 ? (
              /* ── Step 1: Scan Inventory ── */
              <>
                <div className="flex items-center gap-3 mb-4">
                  <div
                    className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
                    style={{
                      background: 'rgba(59,130,246,0.1)',
                      border: '1px solid rgba(59,130,246,0.2)',
                    }}
                  >
                    <Server className="w-5 h-5 text-blue-400" />
                  </div>
                  <div>
                    <h2 className="text-base font-bold text-white leading-tight">
                      Welcome to RuriSkry
                    </h2>
                    <p className="text-xs text-slate-500 mt-0.5">
                      Let's get your workspace set up in 2 steps
                    </p>
                  </div>
                </div>

                <p className="text-sm text-slate-400 leading-relaxed mb-5">
                  First, scan your Azure inventory to discover your cloud resources.
                  The governance agents use this map to know what to audit.
                </p>

                {invStatus === 'error' && errorMsg && (
                  <div
                    className="flex items-start gap-2 mb-4 px-3 py-2.5 rounded-lg text-xs text-rose-300"
                    style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)' }}
                  >
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-rose-400" />
                    {errorMsg}
                  </div>
                )}

                <button
                  onClick={handleScanInventory}
                  disabled={invStatus === 'scanning'}
                  className="w-full py-3 rounded-xl text-sm font-semibold text-white flex items-center justify-center gap-2 transition-all"
                  style={{
                    background: invStatus === 'scanning'
                      ? 'rgba(59,130,246,0.25)'
                      : 'rgba(59,130,246,0.75)',
                    border: '1px solid rgba(59,130,246,0.4)',
                    cursor: invStatus === 'scanning' ? 'not-allowed' : 'pointer',
                    opacity: invStatus === 'scanning' ? 0.8 : 1,
                  }}
                >
                  {invStatus === 'scanning' ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      Scanning Azure resources…
                    </>
                  ) : (
                    <>
                      <Server className="w-4 h-4" />
                      Scan Inventory
                    </>
                  )}
                </button>

                <button
                  onClick={onClose}
                  className="w-full mt-2 py-2 text-xs text-slate-600 hover:text-slate-400 transition-colors"
                >
                  Skip for now
                </button>
              </>
            ) : (
              /* ── Step 2: Run All Agents ── */
              <>
                <div className="flex items-center gap-3 mb-4">
                  <div
                    className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
                    style={{
                      background: 'rgba(16,185,129,0.1)',
                      border: '1px solid rgba(16,185,129,0.2)',
                    }}
                  >
                    <CheckCircle className="w-5 h-5 text-emerald-400" />
                  </div>
                  <div>
                    <h2 className="text-base font-bold text-white leading-tight">
                      Inventory Ready!
                    </h2>
                    {resourceCount != null ? (
                      <p className="text-xs text-emerald-400 mt-0.5">
                        {resourceCount} resource{resourceCount !== 1 ? 's' : ''} discovered
                      </p>
                    ) : (
                      <p className="text-xs text-slate-500 mt-0.5">
                        Inventory scan complete
                      </p>
                    )}
                  </div>
                </div>

                <p className="text-sm text-slate-400 leading-relaxed mb-5">
                  Now run all three governance agents — Cost, Monitoring, and Deploy — to
                  get your first security and cost optimization report.
                </p>

                <button
                  onClick={handleRunAgents}
                  className="w-full py-3 rounded-xl text-sm font-semibold text-white flex items-center justify-center gap-2 transition-all"
                  style={{
                    background: 'rgba(59,130,246,0.75)',
                    border: '1px solid rgba(59,130,246,0.4)',
                    cursor: 'pointer',
                  }}
                >
                  <Bot className="w-4 h-4" />
                  Go to Agents
                  <ArrowRight className="w-4 h-4 ml-auto" />
                </button>

                <button
                  onClick={onClose}
                  className="w-full mt-2 py-2 text-xs text-slate-600 hover:text-slate-400 transition-colors"
                >
                  Skip for now
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
