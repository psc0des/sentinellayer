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
import { useOutletContext } from 'react-router-dom'
import useScanManager from '../hooks/useScanManager'
import AgentCardGrid from '../components/AgentCardGrid'
import ScanHistoryTable from '../components/ScanHistoryTable'
import ScanLogViewer from '../components/ScanLogViewer'

export default function Agents() {
  const { agents, fetchAll } = useOutletContext()

  const {
    scanState,
    logViewer,
    resourceGroup,
    setResourceGroup,
    anyScanning,
    allScanning,
    startScan,
    startAllScans,
    stopScan,
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

      {/* Agent cards with inline controls */}
      <AgentCardGrid
        agents={agents}
        scanState={scanState}
        anyScanning={anyScanning}
        allScanning={allScanning}
        onStartScan={startScan}
        onStartAll={startAllScans}
        onStopScan={stopScan}
        onOpenLiveLog={openLiveLog}
        onOpenHistoricalLog={openHistoricalLog}
        resourceGroup={resourceGroup}
        onResourceGroupChange={setResourceGroup}
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
