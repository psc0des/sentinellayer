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
  fetchInventoryStatus,
  testSlackNotification,
  authMe,
  authStatus,
  authLogout,
  setToken,
} from './api'
import Sidebar          from './components/Sidebar'
import OnboardingModal  from './components/OnboardingModal'
import Overview  from './pages/Overview'
import Agents    from './pages/Agents'
import Decisions from './pages/Decisions'
import AuditLog  from './pages/AuditLog'
import Alerts    from './pages/Alerts'
import Admin     from './pages/Admin'
import Inventory from './pages/Inventory'
import Login     from './pages/Login'
import Setup     from './pages/Setup'
import { RefreshCw, Bell, LogOut } from 'lucide-react'

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

// ── Auth gate — shown before AppShell loads ────────────────────────────────
//
// Flow:
//   1. On mount: call authMe() with the stored token.
//      • Valid token  → show AppShell (state = "ok")
//      • Invalid/none → call authStatus() to decide next screen
//   2. authStatus() returns { setup_required: true }  → show Setup
//      authStatus() returns { setup_required: false } → show Login
//   3. After Setup/Login succeed, parent state transitions to "ok".
//
// The auth gate is outside BrowserRouter intentionally — the Login/Setup
// pages don't need React Router routing.

function AuthGate({ children }) {
  const [authState, setAuthState] = useState('checking') // checking | setup | login | ok
  const [loggedInUser, setLoggedInUser] = useState(null)
  // isNewSetup: true when login came from the Setup page (first-time admin creation)
  const [isNewSetup, setIsNewSetup] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function check() {
      try {
        const me = await authMe()
        if (!cancelled) {
          setLoggedInUser(me.username)
          setAuthState('ok')
        }
      } catch {
        // Token missing or expired — check if setup is still needed
        try {
          const status = await authStatus()
          if (!cancelled) {
            setAuthState(status.setup_required ? 'setup' : 'login')
          }
        } catch {
          // Backend unreachable — let AppShell's own error screen handle it
          if (!cancelled) setAuthState('ok')
        }
      }
    }
    check()
    return () => { cancelled = true }
  }, [])

  function handleLogin(token, username) {
    setToken(token)
    setLoggedInUser(username)
    // Capture whether this login came from the Setup page so AppShell can
    // show the onboarding modal exactly once after a fresh installation.
    setIsNewSetup(authState === 'setup')
    setAuthState('ok')
  }

  async function handleLogout() {
    await authLogout()
    setLoggedInUser(null)
    setIsNewSetup(false)
    setAuthState('login')
  }

  if (authState === 'checking') return <LoadingScreen />
  if (authState === 'setup')    return <Setup onLogin={handleLogin} />
  if (authState === 'login')    return <Login onLogin={handleLogin} />

  // Pass username + logout handler + isNewSetup into children
  return children({ loggedInUser, onLogout: handleLogout, isNewSetup })
}

// ── App Shell (layout + data fetching) ───────────────────────────────────

function AppShell({ loggedInUser, onLogout, isNewSetup }) {
  const [evaluations,     setEvaluations]     = useState([])
  const [scans,           setScans]           = useState([])
  const [alerts,          setAlerts]          = useState([])
  const [metrics,         setMetrics]         = useState(null)
  const [agents,          setAgents]          = useState([])
  const [pendingReviews,  setPendingReviews]  = useState([])
  const [inventoryStatus, setInventoryStatus] = useState(null)
  const [loading,         setLoading]         = useState(true)
  const [dataReady,       setDataReady]       = useState(false)
  const [error,           setError]           = useState(null)
  const [slackStatus,     setSlackStatus]     = useState(null)
  const [slackBtnLabel,   setSlackBtnLabel]   = useState('Slack Connected')

  // Onboarding modal: shown once after fresh Setup → login flow.
  // Dismissed state is persisted to localStorage so it never appears again.
  const [showOnboarding, setShowOnboarding] = useState(
    () => isNewSetup && !localStorage.getItem('ruriskry-onboarding-done')
  )
  const closeOnboarding = useCallback(() => {
    localStorage.setItem('ruriskry-onboarding-done', '1')
    setShowOnboarding(false)
  }, [])

  /**
   * fetchAll — fetch all shared data in parallel.
   * Called by background poll and explicit refresh.
   * pendingReviews silently falls back to [] if the gateway is disabled.
   */
  const fetchAll = useCallback(async () => {
    const [evalsData, metricsData, agentsData, reviewsData, scansData, alertsData, invStatus] = await Promise.all([
      fetchEvaluations(200),
      fetchMetrics(),
      fetchAgents(),
      fetchPendingReviews().catch(() => ({ reviews: [] })),
      fetchScanHistory(200).catch(() => ({ scans: [] })),
      fetchAlerts(200).catch(() => ({ alerts: [] })),
      fetchInventoryStatus().catch(() => null),
    ])
    setEvaluations(evalsData.evaluations ?? [])
    setMetrics(metricsData)
    setAgents(agentsData.agents ?? [])
    setPendingReviews(reviewsData.reviews ?? [])
    setScans(scansData.scans ?? [])
    setAlerts(alertsData.alerts ?? [])
    setInventoryStatus(invStatus)
    setDataReady(true)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // Phase 1 — fast calls only; unblocks LoadingScreen immediately.
      // metrics + agents are lightweight REST endpoints (no large Cosmos scans).
      const [metricsData, agentsData] = await Promise.all([
        fetchMetrics(),
        fetchAgents(),
      ])
      setMetrics(metricsData)
      setAgents(agentsData.agents ?? [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
    // Phase 2 — everything else loads in background after page is visible.
    // fetchAll() runs the full 6-call refresh; errors are swallowed (same as
    // the background poll).
    fetchAll().catch(() => {})
  }, [fetchAll])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    fetchNotificationStatus()
      .then(setSlackStatus)
      .catch(() => setSlackStatus(null))
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

  const alertCount = alerts.filter(a => a.status === 'pending' || a.status === 'investigating').length
  const context = { evaluations, scans, alerts, metrics, agents, pendingReviews, inventoryStatus, fetchAll, loggedInUser, dataReady }

  return (
    <div className="min-h-screen text-slate-100 flex font-sans" style={{ background: 'var(--bg-base)', fontFamily: 'var(--font-ui)' }}>
      {/* Onboarding guide — shown once after first-time admin setup */}
      {showOnboarding && <OnboardingModal onClose={closeOnboarding} />}

      <Sidebar pendingCount={pendingReviews.length} alertCount={alertCount} loggedInUser={loggedInUser} />

      <div className="flex-1 flex flex-col min-w-0 bg-dots">

        {/* ── Top bar ── */}
        <header className="shrink-0 px-6 py-3 flex items-center gap-3 sticky top-0 z-20" style={{
          background: 'rgba(2,8,23,0.92)',
          borderBottom: '1px solid rgba(30,45,74,0.6)',
          backdropFilter: 'blur(12px)',
        }}>
          <div className="flex-1" />

          {/* Slack notification button */}
          {slackStatus?.slack_configured && slackStatus?.slack_enabled ? (
            <button
              onClick={async () => {
                setSlackBtnLabel('Sending…')
                try {
                  const res = await testSlackNotification()
                  setSlackBtnLabel(res.status === 'sent' ? 'Sent!' : 'Failed')
                } catch {
                  setSlackBtnLabel('Failed')
                }
                setTimeout(() => setSlackBtnLabel('Slack Connected'), 2000)
              }}
              className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border bg-emerald-500/10 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
              title="Click to send a test notification to Slack"
            >
              <Bell className="w-3 h-3" />
              {slackBtnLabel}
            </button>
          ) : slackStatus ? (
            <div className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border bg-slate-800 border-slate-700 text-slate-500"
              title="Set SLACK_WEBHOOK_URL in .env to enable"
            >
              <Bell className="w-3 h-3" />
              Slack: Off
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

          {/* User + logout */}
          {loggedInUser && (
            <div className="flex items-center gap-2 pl-1 border-l border-slate-700/50">
              <span className="text-xs text-slate-400">{loggedInUser}</span>
              <button
                onClick={onLogout}
                className="flex items-center gap-1 text-xs px-2 py-1 rounded-md text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
                title="Sign out"
              >
                <LogOut className="w-3.5 h-3.5" />
                Sign out
              </button>
            </div>
          )}

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
    <AuthGate>
      {({ loggedInUser, onLogout, isNewSetup }) => (
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<AppShell loggedInUser={loggedInUser} onLogout={onLogout} isNewSetup={isNewSetup} />}>
              <Route index element={<Navigate to="/overview" replace />} />
              <Route path="overview"    element={<Overview />} />
              <Route path="scans"       element={<Navigate to="/agents" replace />} />
              <Route path="agents"      element={<Agents />} />
              <Route path="inventory"   element={<Inventory />} />
              <Route path="decisions"   element={<Decisions />} />
              <Route path="decisions/:id" element={<Decisions />} />
              <Route path="alerts"      element={<Alerts />} />
              <Route path="audit"       element={<AuditLog />} />
              <Route path="admin"       element={<Admin />} />
              <Route path="*"           element={<Navigate to="/overview" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      )}
    </AuthGate>
  )
}
