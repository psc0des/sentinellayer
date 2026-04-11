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

import React from 'react'
import { AlertTriangle } from 'lucide-react'
import { useOutletContext } from 'react-router-dom'
import useScanManager from '../hooks/useScanManager'
import AgentCardGrid from '../components/AgentCardGrid'
import ScanHistoryTable from '../components/ScanHistoryTable'
import ScanLogViewer from '../components/ScanLogViewer'

export default function Agents() {
  const { agents, inventoryStatus, fetchAll } = useOutletContext()

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
  } = useScanManager({ onScanComplete: fetchAll })

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">

      {/* Page header */}
      <div>
        <h1 className="text-xl font-bold text-white">Agents</h1>
        <p className="text-sm text-slate-500 mt-1">
          Connected agents — trigger scans, view live logs, and review run history
        </p>
      </div>

      {/* Stopping banner — shown while cancellation is pending */}
      {anyStopping && (
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
        onStartScan={startScan}
        onStartAll={startAllScans}
        onStopAll={stopAllScans}
        onStopScan={stopScan}
        onOpenLiveLog={openLiveLog}
        onOpenHistoricalLog={openHistoricalLog}
        resourceGroup={resourceGroup}
        onResourceGroupChange={setResourceGroup}
        inventoryStatus={inventoryStatus}
        subscriptionId={subscriptionId}
        onSubscriptionIdChange={setSubscriptionId}
      />

      {/* Cosmos-backed scan history */}
      <ScanHistoryTable onViewLog={openHistoricalLog} />

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
