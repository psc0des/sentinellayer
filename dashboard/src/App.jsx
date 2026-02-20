/**
 * App.jsx — root component of the SentinelLayer Governance Dashboard.
 *
 * Layout:
 *   Header
 *   MetricsBar   (4 stat cards)
 *   SRIGauge  |  DecisionTable   (side by side)
 *   EvaluationDetail              (appears when a row is selected)
 *
 * Data is fetched from the FastAPI backend at http://localhost:8000/api/.
 */

import React, { useEffect, useState, useCallback } from 'react'
import { fetchEvaluations, fetchMetrics } from './api'
import MetricsBar from './components/MetricsBar'
import SRIGauge from './components/SRIGauge'
import DecisionTable from './components/DecisionTable'
import EvaluationDetail from './components/EvaluationDetail'

// ── Loading / Error screens ────────────────────────────────────────────────

function LoadingScreen() {
  return (
    <div className="min-h-screen bg-slate-900 flex items-center justify-center">
      <div className="text-center">
        <div className="w-12 h-12 border-4 border-blue-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
        <p className="text-slate-400 text-sm">Loading governance data…</p>
      </div>
    </div>
  )
}

function ErrorScreen({ message, onRetry }) {
  return (
    <div className="min-h-screen bg-slate-900 flex items-center justify-center p-6">
      <div className="text-center bg-slate-800 rounded-2xl p-8 border border-red-500/30 max-w-md w-full shadow-xl">
        <div className="text-5xl mb-4">⚠️</div>
        <h2 className="text-xl font-bold text-red-400 mb-2">Connection Error</h2>
        <p className="text-slate-400 text-sm mb-4">{message}</p>
        <p className="text-slate-500 text-xs mb-6">
          Start the FastAPI server first:
          <code className="block mt-2 bg-slate-900 rounded px-3 py-2 text-slate-300 text-left">
            python -m src.api.dashboard_api
          </code>
        </p>
        <button
          onClick={onRetry}
          className="px-6 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm font-medium transition-colors"
        >
          Retry
        </button>
      </div>
    </div>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────

export default function App() {
  const [evaluations, setEvaluations] = useState([])
  const [metrics, setMetrics]         = useState(null)
  const [selected, setSelected]       = useState(null)
  const [loading, setLoading]         = useState(true)
  const [error, setError]             = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // Fetch evaluations and metrics in parallel — Promise.all waits for both
      const [evalsData, metricsData] = await Promise.all([
        fetchEvaluations(20),
        fetchMetrics(),
      ])
      setEvaluations(evalsData.evaluations ?? [])
      setMetrics(metricsData)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  // Fetch on first render
  useEffect(() => { load() }, [load])

  if (loading) return <LoadingScreen />
  if (error)   return <ErrorScreen message={error} onRetry={load} />

  const latestEval  = evaluations[0] ?? null
  const latestScore = latestEval?.sri_composite ?? 0

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100 font-sans">

      {/* ── Header ── */}
      <header className="border-b border-slate-700/60 px-6 py-4 backdrop-blur-sm sticky top-0 z-20 bg-slate-900/95">
        <div className="max-w-7xl mx-auto flex items-center gap-3">
          {/* Logo mark */}
          <div className="w-9 h-9 bg-gradient-to-br from-blue-500 to-blue-700 rounded-lg flex items-center justify-center font-black text-white text-sm shadow">
            SL
          </div>
          <div>
            <h1 className="text-lg font-bold leading-none">SentinelLayer</h1>
            <p className="text-xs text-slate-500 leading-none mt-0.5">AI Action Governance Dashboard</p>
          </div>

          {/* Spacer */}
          <div className="ml-auto flex items-center gap-4">
            {/* Live indicator */}
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              Live
            </div>
            {/* Refresh button */}
            <button
              onClick={load}
              className="text-slate-400 hover:text-slate-200 transition-colors text-sm"
              title="Refresh data"
            >
              ↺ Refresh
            </button>
          </div>
        </div>
      </header>

      {/* ── Main content ── */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-6">

        {/* Metrics summary bar */}
        {metrics && <MetricsBar metrics={metrics} />}

        {/* Gauge + Decision table */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

          {/* SRI Gauge panel */}
          <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 flex flex-col items-center justify-center gap-4">
            <h2 className="self-start text-xs font-semibold text-slate-400 uppercase tracking-widest">
              Latest Evaluation
            </h2>

            <SRIGauge score={latestScore} />

            {latestEval && (
              <div className="text-center">
                <p className="text-sm font-mono font-medium text-slate-300">
                  {latestEval.resource_id?.split('/').filter(Boolean).pop()}
                </p>
                <p className="text-xs text-slate-500 mt-0.5">
                  {latestEval.action_type?.replace(/_/g, ' ')}
                </p>
              </div>
            )}

            {!latestEval && (
              <p className="text-xs text-slate-500 text-center">
                No evaluations yet — run the demo to populate data.
              </p>
            )}
          </div>

          {/* Decision history table */}
          <div className="lg:col-span-2 bg-slate-800 rounded-xl border border-slate-700">
            <DecisionTable
              evaluations={evaluations}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
        </div>

        {/* Evaluation detail (shown when a row is selected) */}
        {selected && (
          <EvaluationDetail
            evaluation={selected}
            onClose={() => setSelected(null)}
          />
        )}

        {/* Footer */}
        <footer className="text-center text-xs text-slate-600 pb-4">
          SentinelLayer · SRI™ Governance Engine · {new Date().getFullYear()}
        </footer>
      </main>
    </div>
  )
}
