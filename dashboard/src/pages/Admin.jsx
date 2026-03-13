/**
 * Admin.jsx — system configuration + admin actions.
 *
 * Two sections:
 *   1. System Configuration — mode, timeouts, feature flags (read from GET /api/config)
 *   2. Danger Zone — Reset button (moved here from the top header)
 */

import React, { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { Settings, AlertTriangle, Trash2, RefreshCw } from 'lucide-react'
import { fetchConfig, adminReset } from '../api'
import GlowCard from '../components/magicui/GlowCard'

// ── Helper components ──────────────────────────────────────────────────────

function ConfigRow({ label, value, mono = false }) {
  return (
    <div className="flex items-center justify-between py-2.5 border-b border-slate-800/60 last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      <span className={`text-xs font-medium text-slate-200 ${mono ? 'font-mono' : ''}`}>{value}</span>
    </div>
  )
}

function StatusBadge({ enabled }) {
  return enabled ? (
    <span className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
      <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
      Enabled
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-slate-700/50 text-slate-500 border border-slate-700/60">
      <span className="w-1.5 h-1.5 rounded-full bg-slate-600" />
      Disabled
    </span>
  )
}

function ModeBadge({ mode }) {
  const isLive = mode === 'live'
  return (
    <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-0.5 rounded-full border ${
      isLive
        ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
        : 'bg-amber-500/10 text-amber-400 border-amber-500/25'
    }`}>
      <span className={`w-1.5 h-1.5 rounded-full ${isLive ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`} />
      {isLive ? 'Live (Azure)' : 'Mock'}
    </span>
  )
}

// ── Main component ─────────────────────────────────────────────────────────

export default function Admin() {
  const { fetchAll } = useOutletContext()
  const [config, setConfig] = useState(null)
  const [configError, setConfigError] = useState(null)
  const [resetting, setResetting] = useState(false)

  useEffect(() => {
    fetchConfig()
      .then(setConfig)
      .catch(e => setConfigError(e.message))
  }, [])

  async function handleReset() {
    if (!window.confirm('Delete ALL local evaluation, scan, alert and execution data? This cannot be undone.')) return
    setResetting(true)
    try {
      const result = await adminReset()
      await fetchAll()
      alert(`Reset complete — deleted ${result.total} records.`)
    } catch (e) {
      alert(`Reset failed: ${e.message}`)
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-3xl mx-auto">

      {/* ── Page header ── */}
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg flex items-center justify-center bg-slate-800 border border-slate-700">
          <Settings className="w-4.5 h-4.5 text-slate-400" />
        </div>
        <div>
          <h1 className="text-lg font-semibold text-slate-100 leading-none">Admin</h1>
          <p className="text-xs text-slate-500 mt-0.5">System configuration and management</p>
        </div>
      </div>

      {/* ── System Configuration ── */}
      <GlowCard color="slate" intensity="low" className="p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
            <Settings className="w-4 h-4 text-slate-500" />
            System Configuration
          </h2>
          <button
            onClick={() => fetchConfig().then(setConfig).catch(e => setConfigError(e.message))}
            className="text-slate-600 hover:text-slate-300 transition-colors"
            title="Refresh config"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>

        {configError ? (
          <p className="text-xs text-rose-400">{configError}</p>
        ) : !config ? (
          <div className="space-y-2.5">
            {[...Array(6)].map((_, i) => (
              <div key={i} className="h-8 bg-slate-800/60 rounded animate-pulse" />
            ))}
          </div>
        ) : (
          <div>
            <ConfigRow label="Mode" value={<ModeBadge mode={config.mode} />} />
            <ConfigRow label="LLM Timeout" value={`${config.llm_timeout}s`} mono />
            <ConfigRow label="LLM Concurrency" value={`${config.llm_concurrency_limit} parallel`} mono />
            <ConfigRow label="Execution Gateway" value={<StatusBadge enabled={config.execution_gateway_enabled} />} />
            <ConfigRow label="Live Topology" value={<StatusBadge enabled={config.use_live_topology} />} />
            <ConfigRow label="Version" value={`v${config.version}`} mono />
          </div>
        )}
      </GlowCard>

      {/* ── Danger Zone ── */}
      <GlowCard color="red" intensity="low" className="p-5" style={{ borderColor: 'rgba(239,68,68,0.2)' }}>
        <div className="flex items-center gap-2 mb-4">
          <AlertTriangle className="w-4 h-4 text-rose-500" />
          <h2 className="text-sm font-semibold text-rose-400">Danger Zone</h2>
        </div>

        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-slate-200">Reset all local data</p>
            <p className="text-xs text-slate-500 mt-0.5 max-w-sm">
              Permanently deletes all local evaluation, execution, scan, and alert records.
              Cosmos DB data is never touched. The server stays running.
            </p>
          </div>
          <button
            onClick={handleReset}
            disabled={resetting}
            className="shrink-0 flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium bg-rose-500/10 border border-rose-500/30 text-rose-400 hover:bg-rose-500/20 hover:border-rose-500/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Trash2 className="w-4 h-4" />
            {resetting ? 'Resetting…' : 'Reset'}
          </button>
        </div>
      </GlowCard>

    </div>
  )
}
