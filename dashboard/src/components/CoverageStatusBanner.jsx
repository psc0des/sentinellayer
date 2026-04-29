/**
 * CoverageStatusBanner — shown on the Agents page when one or more Microsoft
 * APIs returned a permission error during the last preflight check.
 *
 * Fetches /api/coverage/status on mount. If any API is ok=false it renders
 * an amber warning banner listing each failing API and the missing role.
 * Hides automatically when all APIs report ok=true.
 */

import React, { useEffect, useState } from 'react'
import { AlertTriangle, ShieldAlert, RefreshCw } from 'lucide-react'

const API_BASE = import.meta.env.VITE_API_URL || ''

export default function CoverageStatusBanner() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [dismissed, setDismissed] = useState(false)

  const fetchStatus = () => {
    setLoading(true)
    fetch(`${API_BASE}/api/coverage/status`, {
      headers: {
        'Content-Type': 'application/json',
        ...(localStorage.getItem('api_key') ? { 'X-API-Key': localStorage.getItem('api_key') } : {}),
      },
    })
      .then(r => r.json())
      .then(data => {
        setStatus(data)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }

  useEffect(() => { fetchStatus() }, [])

  if (loading || dismissed || !status) return null

  const apis = status.apis || {}
  const failingApis = Object.entries(apis).filter(([, v]) => v && v.ok === false)
  if (failingApis.length === 0) return null

  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/8 px-4 py-3 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-300">
              Coverage degraded — {failingApis.length} Microsoft API{failingApis.length > 1 ? 's' : ''} unavailable
            </p>
            <p className="text-xs text-slate-400 mt-0.5">
              The rules engine still runs deterministically. Only the Microsoft API
              enrichment layer is affected. Grant the missing roles to restore full coverage.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={fetchStatus}
            className="text-slate-400 hover:text-slate-200 p-1 rounded"
            title="Re-check"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => setDismissed(true)}
            className="text-slate-500 hover:text-slate-300 text-xs px-2 py-1 rounded hover:bg-slate-700"
          >
            Dismiss
          </button>
        </div>
      </div>

      <ul className="space-y-1 ml-7">
        {failingApis.map(([name, info]) => (
          <li key={name} className="flex items-start gap-2 text-xs">
            <ShieldAlert className="w-3 h-3 text-amber-500 shrink-0 mt-0.5" />
            <span>
              <span className="font-mono text-amber-300">{name}</span>
              {info.error && (
                <span className="text-slate-400 ml-2">— {info.error}</span>
              )}
            </span>
          </li>
        ))}
      </ul>

      {status.rules && (
        <p className="text-xs text-slate-500 ml-7">
          {status.rules.total} rules loaded &nbsp;·&nbsp;
          {Object.entries(status.rules.by_category || {})
            .map(([cat, count]) => `${count} ${cat}`)
            .join(', ')}
        </p>
      )}
    </div>
  )
}
