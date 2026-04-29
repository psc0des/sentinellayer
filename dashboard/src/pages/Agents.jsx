/**
 * Agents.jsx — enterprise-grade agents page.
 *
 * Single-system architecture replacing the dual ConnectedAgents + AgentControls
 * approach. All scan state flows through useScanManager. Supports live SSE logs,
 * stop controls, historical log viewing, and refresh-resilient state.
 *
 * Layout:
 *   useScanManager()     — single source of truth for all scan state
 *   <AgentCardGrid />    — agent cards with inline scan/stop/log controls
 *   <ScanHistoryTable /> — Cosmos-backed history with "View Log"
 *   <ScanLogViewer />    — portal overlay (live or historical mode)
 */

import React, { useState, useCallback, useEffect, useRef } from 'react'
import { AlertTriangle } from 'lucide-react'
import { useOutletContext } from 'react-router-dom'
import useScanManager from '../hooks/useScanManager'
import AgentCardGrid from '../components/AgentCardGrid'
import ScanHistoryTable from '../components/ScanHistoryTable'
import ScanLogViewer from '../components/ScanLogViewer'
import CoverageStatusBanner from '../components/CoverageStatusBanner'

export default function Agents() {
  const { agents, inventoryStatus, fetchAll } = useOutletContext()

  // Bumped whenever we want ScanHistoryTable to reload:
  //   • immediately when stop is clicked (shows "Cancelling" row)
  //   • again when scan reaches terminal state (shows final status)
  const [histRefreshKey, setHistRefreshKey] = useState(0)

  const bumpHistory = useCallback(() => setHistRefreshKey(k => k + 1), [])

  const {
    scanState,
    logViewer,
    resourceGroup,
    setResourceGroup,
    subscriptionId,
    setSubscriptionId,
    anyScanning,
    allScanning,
    anyStopping,
    startScan,
    startAllScans,
    stopScan,
    stopAllScans,
    openLiveLog,
    openHistoricalLog,
    closeLogs,
  } = useScanManager({
    onScanComplete: useCallback(() => {
      fetchAll()
      bumpHistory()
    }, [fetchAll, bumpHistory]),
  })

  // Banner persistence: show the amber "Cancellation requested" banner for at
  // least 8 seconds after any stop action, even if the scan resolves instantly.
  const [bannerUntil, setBannerUntil] = useState(0)
  const bannerTimerRef = useRef(null)
  const showStoppingBanner = anyStopping || Date.now() < bannerUntil

  useEffect(() => {
    if (!bannerUntil || Date.now() >= bannerUntil) return
    if (bannerTimerRef.current) clearTimeout(bannerTimerRef.current)
    bannerTimerRef.current = setTimeout(
      () => setBannerUntil(0),
      bannerUntil - Date.now(),
    )
    return () => clearTimeout(bannerTimerRef.current)
  }, [bannerUntil])

  const extendBanner = useCallback(() => setBannerUntil(Date.now() + 8000), [])

  // Wrappers that refresh history at the right moments:
  //   • On START: delay 1.5 s so the backend has time to write the running
  //     record, then show it in the table (so "Cancelling" has a row to update)
  //   • On STOP : immediately refresh so the row transitions to "Cancelling"
  //   • On COMPLETE: refresh again to show final status (via onScanComplete above)

  const handleStartScan = useCallback((agentType, mode) => {
    startScan(agentType, mode)
    setTimeout(bumpHistory, 1500)
  }, [startScan, bumpHistory])

  const handleStartAll = useCallback((mode) => {
    startAllScans(mode)
    setTimeout(bumpHistory, 1500)
  }, [startAllScans, bumpHistory])

  const handleStopScan = useCallback((agentType) => {
    stopScan(agentType)
    extendBanner()
    // Don't bump history immediately — the cross-reference logic already shows
    // "Cancelling" for the existing Running row without a reload. Delay the
    // reload so the backend has time to write the final "cancelled" status.
    setTimeout(bumpHistory, 6000)
  }, [stopScan, bumpHistory, extendBanner])

  const handleStopAll = useCallback(() => {
    stopAllScans()
    extendBanner()
    setTimeout(bumpHistory, 6000)
  }, [stopAllScans, bumpHistory, extendBanner])

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">

      {/* Page header */}
      <div>
        <h1 className="text-xl font-bold text-white">Agents</h1>
        <p className="text-sm text-slate-500 mt-1">
          Connected agents — trigger scans, view live logs, and review run history
        </p>
      </div>

      {/* Coverage degraded banner — shown when Microsoft APIs are missing permissions */}
      <CoverageStatusBanner />

      {/* Stopping banner — shown while cancellation is pending, or for 8s after stop */}
      {showStoppingBanner && (
        <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
          <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>
            <strong>Cancellation requested</strong> — the scan will stop after its current
            evaluation finishes (may take up to 30 seconds).{' '}
            <strong>Please wait before starting a new scan.</strong>
          </span>
        </div>
      )}

      {/* Agent cards with inline controls */}
      <AgentCardGrid
        agents={agents}
        scanState={scanState}
        anyScanning={anyScanning}
        allScanning={allScanning}
        onStartScan={handleStartScan}
        onStartAll={handleStartAll}
        onStopAll={handleStopAll}
        onStopScan={handleStopScan}
        onOpenLiveLog={openLiveLog}
        onOpenHistoricalLog={openHistoricalLog}
        resourceGroup={resourceGroup}
        onResourceGroupChange={setResourceGroup}
        inventoryStatus={inventoryStatus}
        subscriptionId={subscriptionId}
        onSubscriptionIdChange={setSubscriptionId}
      />

      {/* Cosmos-backed scan history — refreshKey triggers reload on stop/complete */}
      <ScanHistoryTable
        onViewLog={openHistoricalLog}
        scanState={scanState}
        refreshKey={histRefreshKey}
      />

      {/* Log viewer overlay (live or historical) */}
      <ScanLogViewer
        scanId={logViewer.scanId}
        agentType={logViewer.agentType}
        scanEntries={logViewer.scanEntries}
        mode={logViewer.mode}
        isOpen={logViewer.open}
        onClose={closeLogs}
        isComplete={
          logViewer.mode === 'live' && (
            logViewer.agentType === 'all'
              ? !anyScanning
              : scanState[logViewer.agentType]?.status !== 'running'
          )
        }
        startedAt={
          logViewer.mode === 'live' && logViewer.agentType !== 'all'
            ? scanState[logViewer.agentType]?.startedAt
            : logViewer.mode === 'live'
              ? scanState.cost?.startedAt ?? scanState.monitoring?.startedAt ?? scanState.deploy?.startedAt
              : null
        }
      />

    </div>
  )
}
