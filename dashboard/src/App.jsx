/**
 * App.jsx — router shell for the RuriSkry Governance Dashboard.
 *
 * Responsibilities:
 *   - Set up React Router with five named pages
 *   - Fetch evaluations, metrics, agents, and pending reviews on mount
 *   - Run a silent 5-second background refresh
 *   - Pass shared data to every page via Outlet context
 *   - Render the left Sidebar and top header bar
 */

import React, { useEffect, useState, useCallback } from 'react'
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  Outlet,
} from 'react-router-dom'
import {
  fetchEvaluations,
  fetchMetrics,
  fetchAgents,
  fetchPendingReviews,
  fetchNotificationStatus,
  fetchScanHistory,
  fetchAlerts,
  testTeamsNotification,
} from './api'
import Sidebar from './components/Sidebar'
import Overview  from './pages/Overview'
import Scans     from './pages/Scans'
import Agents    from './pages/Agents'
import Decisions from './pages/Decisions'
import AuditLog  from './pages/AuditLog'
import Alerts    from './pages/Alerts'
import Admin     from './pages/Admin'
import { RefreshCw, Bell } from 'lucide-react'

// ── Loading / Error screens ────────────────────────────────────────────────

function LoadingScreen() {
  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center">
      <div className="text-center">
        <div className="w-12 h-12 border-4 border-blue-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
        <p className="text-slate-400 text-sm">Loading governance data…</p>
      </div>
    </div>
  )
}

function ErrorScreen({ message, onRetry }) {
  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center p-6">
      <div className="bg-slate-800 rounded-2xl p-8 border border-red-500/30 max-w-md w-full shadow-xl text-center">
        <div className="text-5xl mb-4">⚠️</div>
        <h2 className="text-xl font-bold text-red-400 mb-2">Connection Error</h2>
        <p className="text-slate-400 text-sm mb-4">{message}</p>
        <p className="text-slate-500 text-xs mb-6">
          Start the FastAPI server first:
          <code className="block mt-2 bg-slate-900 rounded px-3 py-2 text-slate-300 text-left">
            uvicorn src.api.dashboard_api:app --reload
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

// ── App Shell (layout + data fetching) ───────────────────────────────────

function AppShell() {
  const [evaluations,    setEvaluations]    = useState([])
  const [scans,          setScans]          = useState([])
  const [alerts,         setAlerts]         = useState([])
  const [metrics,        setMetrics]        = useState(null)
  const [agents,         setAgents]         = useState([])
  const [pendingReviews, setPendingReviews] = useState([])
  const [loading,        setLoading]        = useState(true)
  const [error,          setError]          = useState(null)
  const [teamsStatus,    setTeamsStatus]    = useState(null)
  const [teamsBtnLabel,  setTeamsBtnLabel]  = useState('Teams Connected')

  /**
   * fetchAll — fetch all shared data in parallel.
   * Called by background poll and explicit refresh.
   * pendingReviews silently falls back to [] if the gateway is disabled.
   */
  const fetchAll = useCallback(async () => {
    const [evalsData, metricsData, agentsData, reviewsData, scansData, alertsData] = await Promise.all([
      fetchEvaluations(200),
      fetchMetrics(),
      fetchAgents(),
      fetchPendingReviews().catch(() => ({ pending_reviews: [] })),
      fetchScanHistory(200).catch(() => ({ scans: [] })),
      fetchAlerts(200).catch(() => ({ alerts: [] })),
    ])
    setEvaluations(evalsData.evaluations ?? [])
    setMetrics(metricsData)
    setAgents(agentsData.agents ?? [])
    setPendingReviews(reviewsData.pending_reviews ?? [])
    setScans(scansData.scans ?? [])
    setAlerts(alertsData.alerts ?? [])
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      await fetchAll()
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [fetchAll])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    fetchNotificationStatus()
      .then(setTeamsStatus)
      .catch(() => setTeamsStatus(null))
  }, [])

  // Silent background refresh — errors are swallowed
  useEffect(() => {
    const id = setInterval(async () => {
      try { await fetchAll() } catch { /* ignore */ }
    }, 5_000)
    return () => clearInterval(id)
  }, [fetchAll])

  if (loading) return <LoadingScreen />
  if (error)   return <ErrorScreen message={error} onRetry={load} />

  const alertCount = alerts.filter(a => a.status === 'firing' || a.status === 'investigating').length
  const context = { evaluations, scans, alerts, metrics, agents, pendingReviews, fetchAll }

  return (
    <div className="min-h-screen text-slate-100 flex font-sans" style={{ background: 'var(--bg-base)', fontFamily: 'var(--font-ui)' }}>
      <Sidebar pendingCount={pendingReviews.length} alertCount={alertCount} />

      <div className="flex-1 flex flex-col min-w-0 bg-dots">

        {/* ── Top bar ── */}
        <header className="shrink-0 px-6 py-3 flex items-center gap-3 sticky top-0 z-20" style={{
          background: 'rgba(2,8,23,0.92)',
          borderBottom: '1px solid rgba(30,45,74,0.6)',
          backdropFilter: 'blur(12px)',
        }}>
          <div className="flex-1" />

          {/* Teams notification button */}
          {teamsStatus?.teams_configured && teamsStatus?.teams_enabled ? (
            <button
              onClick={async () => {
                setTeamsBtnLabel('Sending…')
                try {
                  const res = await testTeamsNotification()
                  setTeamsBtnLabel(res.status === 'sent' ? 'Sent!' : 'Failed')
                } catch {
                  setTeamsBtnLabel('Failed')
                }
                setTimeout(() => setTeamsBtnLabel('Teams Connected'), 2000)
              }}
              className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border bg-emerald-500/10 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
              title="Click to send a test notification to Teams"
            >
              <Bell className="w-3 h-3" />
              {teamsBtnLabel}
            </button>
          ) : teamsStatus ? (
            <div className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border bg-slate-800 border-slate-700 text-slate-500"
              title="Set TEAMS_WEBHOOK_URL in .env to enable"
            >
              <Bell className="w-3 h-3" />
              Teams: Off
            </div>
          ) : null}

          {/* Live indicator */}
          <div className="flex items-center gap-1.5 text-xs text-slate-500">
            <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
            Live
          </div>

          {/* Refresh */}
          <button
            onClick={load}
            className="text-slate-400 hover:text-slate-200 transition-colors p-1 rounded"
            title="Refresh all data"
          >
            <RefreshCw className="w-4 h-4" />
          </button>

        </header>

        {/* ── Page content ── */}
        <main className="flex-1 overflow-auto">
          <Outlet context={context} />
        </main>
      </div>
    </div>
  )
}

// ── Root component — sets up BrowserRouter + routes ──────────────────────

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<AppShell />}>
          <Route index element={<Navigate to="/overview" replace />} />
          <Route path="overview"    element={<Overview />} />
          <Route path="scans"       element={<Scans />} />
          <Route path="agents"      element={<Agents />} />
          <Route path="decisions"   element={<Decisions />} />
          <Route path="decisions/:id" element={<Decisions />} />
          <Route path="alerts"      element={<Alerts />} />
          <Route path="audit"       element={<AuditLog />} />
          <Route path="admin"       element={<Admin />} />
          <Route path="*"           element={<Navigate to="/overview" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
